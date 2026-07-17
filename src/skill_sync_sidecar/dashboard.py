from __future__ import annotations

import copy
import json
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Tuple

from .hub_import import build_hub_import_diagnosis, build_hub_import_preview_package, execute_hub_import_apply
from .ops_status import build_ops_status
from .projection import ProjectionError, build_tool_projection
from .remote import Remote, RemoteError, download_snapshot
from .tool_status import build_device_tool_status


@dataclass(frozen=True)
class DashboardConfig:
    local_root: Path
    remote_snapshot: Path
    base_record: Optional[Path] = None
    state_file: Optional[Path] = None
    blocked_report: Optional[Path] = None
    openclaw_reconcile_report: Optional[Path] = None
    openclaw_reconcile_root: Optional[Path] = None
    allow_new: bool = False
    allow_delete: bool = False
    writer_policy: str = "push-pull"
    peer_status_files: Optional[Dict[str, Path]] = None
    hub_import_work_dir: Optional[Path] = None


@dataclass(frozen=True)
class GatewayConfig:
    remote: Remote
    remote_prefix: str
    cache_dir: Path
    refresh_interval_seconds: float = 60.0
    peer_status_files: Optional[Dict[str, Path]] = None
    remote_peer_status_paths: Optional[Dict[str, str]] = None
    hub_import_work_dir: Optional[Path] = None


class RemoteSnapshotCache:
    def __init__(self, remote: Remote, prefix: str, cache_dir: Path, refresh_interval_seconds: float):
        self.remote = remote
        self.prefix = prefix
        self.cache_dir = cache_dir.expanduser()
        self.refresh_interval_seconds = max(0.0, refresh_interval_seconds)
        self._last_refresh = 0.0

    def snapshot_dir(self) -> Path:
        index_path = self.cache_dir / "index.json"
        now = time.monotonic()
        stale = now - self._last_refresh >= self.refresh_interval_seconds
        if stale or not index_path.exists():
            download_snapshot(self.remote, self.cache_dir, self.prefix)
            self._last_refresh = now
        return self.cache_dir


class DashboardSummaryCache:
    """Fast, stale-safe cache for the browser summary endpoint."""

    def __init__(
        self,
        status_provider: Callable[[], dict],
        *,
        timeout_seconds: float = 2.0,
        stale_after_seconds: float = 120.0,
    ):
        self.status_provider = status_provider
        self.timeout_seconds = max(0.05, timeout_seconds)
        self.stale_after_seconds = max(0.0, stale_after_seconds)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="skill-sync-summary")
        self._lock = threading.Lock()
        self._payload: Optional[dict] = None
        self._updated_monotonic: Optional[float] = None
        self._updated_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._last_attempt_at: Optional[str] = None
        self._inflight: Optional[Future] = None

    def get_summary(self) -> tuple[int, dict]:
        now = time.monotonic()
        with self._lock:
            self._consume_finished_locked()
            if self._payload is not None:
                age = self._age_seconds(now)
                state = "fresh" if age is not None and age < self.stale_after_seconds else "stale"
                if state == "stale":
                    self._ensure_refresh_locked()
                return 200, self._payload_with_metadata(state, age)
            future = self._ensure_refresh_locked()

        try:
            payload = future.result(timeout=self.timeout_seconds)
        except TimeoutError:
            with self._lock:
                self._last_error = f"summary refresh timed out after {self.timeout_seconds:g}s"
            return 503, self._miss_payload("summary refresh timed out")
        except Exception as exc:  # pragma: no cover - defensive cache boundary
            with self._lock:
                self._last_error = str(exc)
                if self._inflight is future:
                    self._inflight = None
            return 500, self._miss_payload(str(exc))

        with self._lock:
            self._store_locked(payload)
            if self._inflight is future:
                self._inflight = None
            return 200, self._payload_with_metadata("fresh", 0.0)

    def healthz(self) -> dict:
        now = time.monotonic()
        with self._lock:
            self._consume_finished_locked()
            age = self._age_seconds(now)
            state = "empty" if self._payload is None else "fresh" if age is not None and age < self.stale_after_seconds else "stale"
            return {
                "state": state,
                "generated_at": self._updated_at,
                "age_seconds": age,
                "stale_after_seconds": self.stale_after_seconds,
                "timeout_seconds": self.timeout_seconds,
                "refresh_in_flight": self._inflight is not None,
                "last_attempt_at": self._last_attempt_at,
                "last_error": self._last_error,
            }

    def _refresh(self) -> dict:
        return build_dashboard_summary(self.status_provider())

    def _ensure_refresh_locked(self) -> Future:
        if self._inflight is None or self._inflight.done():
            self._last_attempt_at = datetime.now(timezone.utc).isoformat()
            self._inflight = self._executor.submit(self._refresh)
        return self._inflight

    def _consume_finished_locked(self) -> None:
        if self._inflight is None or not self._inflight.done():
            return
        future = self._inflight
        self._inflight = None
        try:
            self._store_locked(future.result())
        except Exception as exc:  # pragma: no cover - defensive cache boundary
            self._last_error = str(exc)

    def _store_locked(self, payload: dict) -> None:
        self._payload = copy.deepcopy(payload)
        self._updated_monotonic = time.monotonic()
        self._updated_at = datetime.now(timezone.utc).isoformat()
        self._last_error = None

    def _age_seconds(self, now: float) -> Optional[float]:
        if self._updated_monotonic is None:
            return None
        return round(max(0.0, now - self._updated_monotonic), 3)

    def _payload_with_metadata(self, state: str, age_seconds: Optional[float]) -> dict:
        payload = copy.deepcopy(self._payload or {})
        metadata = {
            "state": state,
            "generated_at": self._updated_at,
            "age_seconds": age_seconds,
            "stale_after_seconds": self.stale_after_seconds,
            "timeout_seconds": self.timeout_seconds,
            "refresh_in_flight": self._inflight is not None,
            "last_attempt_at": self._last_attempt_at,
            "last_error": self._last_error,
        }
        payload["summary_cache"] = metadata
        dashboard = payload.get("dashboard")
        if isinstance(dashboard, dict):
            dashboard["summary_cache"] = metadata
        return payload

    def _miss_payload(self, error: str) -> dict:
        metadata = self.healthz()
        metadata["state"] = "miss"
        metadata["last_error"] = error
        return {
            "ok": False,
            "health": "red",
            "error": error,
            "summary_cache": metadata,
            "dashboard": {
                "health": "red",
                "blocked": 0,
                "operator": {
                    "headline": "状态聚合超时",
                    "next_action": "检查 WebDAV、peer-status 或 gateway 日志；没有可用缓存时 summary 会返回 503。",
                    "sync_path": "Gateway summary cache",
                    "snapshot_id": None,
                    "devices": {},
                    "blocked_count": 0,
                },
                "blocked_items": [],
                "devices": [],
                "tools": [],
                "device_tools": [],
                "summary_cache": metadata,
            },
        }


def build_dashboard_status(config: DashboardConfig) -> dict:
    status = build_ops_status(
        config.local_root,
        config.remote_snapshot,
        base_record=config.base_record,
        state_file=config.state_file,
        blocked_report=config.blocked_report,
        openclaw_reconcile_report=config.openclaw_reconcile_report,
        openclaw_reconcile_root=config.openclaw_reconcile_root,
        allow_new=config.allow_new,
        allow_delete=config.allow_delete,
        writer_policy=config.writer_policy,
    )
    peers = _load_peer_status_files(config.peer_status_files or {})
    devices = _device_overview(status, peers)
    blocked_items = _blocked_items(status, peers)
    operator = _operator_summary(status, devices, blocked_items)
    projection = _safe_tool_projection(config.remote_snapshot)
    hub_import = _safe_hub_import_diagnosis()
    local_tools = build_device_tool_status()
    planned_devices = _planned_device_overview()
    device_tools = _device_tool_overview(devices, {"mac": {"tools": local_tools, "published_at": _status_last_seen_at(status)}, **peers})
    tool_projection = _merge_tool_projection(local_tools, projection)
    status["dashboard"] = {
        "health": _aggregate_health([status.get("health")] + [device.get("health") for device in devices]),
        "blocked": len(blocked_items),
        "operator": operator,
        "blocked_items": blocked_items,
        "devices": devices,
        "planned_devices": planned_devices,
        "tools": tool_projection,
        "device_tools": device_tools,
        "tool_projection": projection,
        "hub_import": hub_import,
        "local_workspace": _local_workspace_model(devices, device_tools, blocked_items),
        "central_repository": _central_repository_model(status, snapshot=status.get("remote_snapshot"), tools=tool_projection, blocked_items=blocked_items),
        "device_map": _device_map_model(devices, planned_devices, device_tools, blocked_items),
    }
    return status


def build_gateway_status(
    cache: RemoteSnapshotCache,
    peer_status_files: Optional[Dict[str, Path]] = None,
    remote_peer_status: Optional[Dict[str, dict]] = None,
) -> dict:
    snapshot_dir = cache.snapshot_dir()
    index_path = snapshot_dir / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    snapshot = {
        "ok": True,
        "path": str(snapshot_dir),
        "snapshot_id": index.get("snapshot_id"),
        "created_at": index.get("created_at"),
        "total": index.get("total", len(index.get("skills", []))),
        "protocol_version": index.get("protocol_version"),
    }
    peers = dict(remote_peer_status or {})
    peers.update(_load_peer_status_files(peer_status_files or {}))
    status = {
        "ok": True,
        "health": "green",
        "mode": "gateway",
        "local_root": None,
        "remote_snapshot": snapshot,
        "base_record": None,
        "daemon_state": {
            "ok": True,
            "status": "gateway",
            "daemon_status": "running",
            "target": "webdav-observer",
            "writer_policy": "read-only",
            "interval_seconds": cache.refresh_interval_seconds,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        "blocked_report": None,
        "sync_plan": {
            "ok": True,
            "writer_policy": "read-only",
            "total": snapshot["total"],
            "summary": {"observed": snapshot["total"]},
            "allowed": 0,
            "blocked": 0,
            "safe_to_apply": False,
            "status_summary": {"observed": snapshot["total"]},
            "local_overrides": {"total": 0, "skills": []},
            "has_conflicts": False,
        },
        "openclaw_reconcile": None,
        "openclaw_gate": {"ok": True, "available": False, "reason": "gateway mode does not run OpenClaw reconcile"},
        "allow_new": False,
        "allow_delete": False,
        "writer_policy": "read-only",
        "error_count": 0,
    }
    devices = _gateway_device_overview(snapshot, peers)
    blocked_items = _blocked_items(status, peers)
    operator = _operator_summary(status, devices, blocked_items)
    projection = _safe_tool_projection(snapshot_dir)
    hub_import = _safe_hub_import_diagnosis()
    planned_devices = _planned_device_overview()
    tools = _gateway_tool_overview(projection)
    device_tools = _device_tool_overview(devices, peers)
    status["dashboard"] = {
        "health": _aggregate_health([status.get("health")] + [device.get("health") for device in devices]),
        "blocked": len(blocked_items),
        "operator": operator,
        "blocked_items": blocked_items,
        "devices": devices,
        "planned_devices": planned_devices,
        "tools": tools,
        "device_tools": device_tools,
        "tool_projection": projection,
        "hub_import": hub_import,
        "local_workspace": _local_workspace_model(devices, device_tools, blocked_items),
        "central_repository": _central_repository_model(status, snapshot=snapshot, tools=tools, blocked_items=blocked_items),
        "device_map": _device_map_model(devices, planned_devices, device_tools, blocked_items),
    }
    return status


def build_dashboard_summary(status: dict) -> dict:
    """Return the compact status shape used by the browser dashboard."""
    dashboard = status.get("dashboard") if isinstance(status.get("dashboard"), dict) else {}
    sync_plan = status.get("sync_plan") if isinstance(status.get("sync_plan"), dict) else {}
    remote_snapshot = status.get("remote_snapshot") if isinstance(status.get("remote_snapshot"), dict) else {}
    daemon_state = status.get("daemon_state") if isinstance(status.get("daemon_state"), dict) else {}
    blocked_report = status.get("blocked_report") if isinstance(status.get("blocked_report"), dict) else {}
    base_record = status.get("base_record") if isinstance(status.get("base_record"), dict) else {}
    return {
        "ok": status.get("ok"),
        "health": status.get("health"),
        "mode": status.get("mode"),
        "local_root": status.get("local_root"),
        "writer_policy": status.get("writer_policy"),
        "allow_new": status.get("allow_new"),
        "allow_delete": status.get("allow_delete"),
        "error_count": status.get("error_count"),
        "error": status.get("error"),
        "remote_snapshot": {
            "ok": remote_snapshot.get("ok"),
            "path": remote_snapshot.get("path"),
            "snapshot_id": remote_snapshot.get("snapshot_id"),
            "created_at": remote_snapshot.get("created_at"),
            "total": remote_snapshot.get("total"),
            "protocol_version": remote_snapshot.get("protocol_version"),
        },
        "base_record": {
            "ok": base_record.get("ok"),
            "path": base_record.get("path"),
            "snapshot_id": base_record.get("snapshot_id"),
            "applied_count": base_record.get("applied_count"),
        } if base_record else None,
        "daemon_state": {
            "ok": daemon_state.get("ok"),
            "status": daemon_state.get("status"),
            "daemon_status": daemon_state.get("daemon_status"),
            "target": daemon_state.get("target"),
            "writer_policy": daemon_state.get("writer_policy"),
            "interval_seconds": daemon_state.get("interval_seconds"),
            "stop_on_blocked": daemon_state.get("stop_on_blocked"),
            "updated_at": daemon_state.get("updated_at"),
            "cycles_run": daemon_state.get("cycles_run"),
            "last_cycle": daemon_state.get("last_cycle"),
            "path": daemon_state.get("path"),
        },
        "blocked_report": {
            "ok": blocked_report.get("ok"),
            "path": blocked_report.get("path"),
            "total": blocked_report.get("total"),
            "summary": blocked_report.get("summary"),
        } if blocked_report else None,
        "sync_plan": {
            "ok": sync_plan.get("ok"),
            "writer_policy": sync_plan.get("writer_policy"),
            "total": sync_plan.get("total"),
            "summary": sync_plan.get("summary"),
            "allowed": sync_plan.get("allowed"),
            "blocked": sync_plan.get("blocked"),
            "blocked_items": sync_plan.get("blocked_items", []),
            "safe_to_apply": sync_plan.get("safe_to_apply"),
            "status_summary": sync_plan.get("status_summary"),
            "local_overrides": sync_plan.get("local_overrides"),
            "has_conflicts": sync_plan.get("has_conflicts"),
        },
        "dashboard": {
            "health": dashboard.get("health"),
            "blocked": dashboard.get("blocked"),
            "operator": dashboard.get("operator"),
            "blocked_items": dashboard.get("blocked_items", []),
            "devices": dashboard.get("devices", []),
            "planned_devices": dashboard.get("planned_devices", []),
            "tools": dashboard.get("tools", []),
            "device_tools": dashboard.get("device_tools", []),
            "local_workspace": dashboard.get("local_workspace", {}),
            "central_repository": dashboard.get("central_repository", {}),
            "device_map": dashboard.get("device_map", {}),
            "hub_import": _compact_hub_import(dashboard.get("hub_import")),
        },
    }


def _compact_hub_import(hub_import: object) -> dict:
    if not isinstance(hub_import, dict):
        return {}
    action_plan = hub_import.get("action_plan") if isinstance(hub_import.get("action_plan"), dict) else {}
    return {
        "ok": hub_import.get("ok"),
        "error": hub_import.get("error"),
        "hub_total": hub_import.get("hub_total"),
        "source_total": hub_import.get("source_total"),
        "summary": hub_import.get("summary", {}),
        "action_plan": {
            "mode": action_plan.get("mode"),
            "safe_to_apply_automatically": action_plan.get("safe_to_apply_automatically"),
            "summary": action_plan.get("summary", {}),
            "review_required": action_plan.get("review_required"),
        },
        "items": _compact_hub_import_items(hub_import.get("items")),
    }


def _compact_hub_import_items(items: object, *, per_status_limit: int = 12) -> list[dict]:
    if not isinstance(items, list):
        return []
    statuses = ["importable", "update_available", "already_in_hub", "not_compatible"]
    selected: list[dict] = []
    seen: set[int] = set()
    for status in statuses:
        count = 0
        for index, item in enumerate(items):
            if count >= per_status_limit:
                break
            if index in seen or not isinstance(item, dict) or item.get("status") != status:
                continue
            selected.append(item)
            seen.add(index)
            count += 1
    if not selected:
        selected = [item for item in items[:per_status_limit] if isinstance(item, dict)]
    return selected


def build_hub_import_preview_response(
    work_dir: Optional[Path] = None,
    *,
    hub_root: Optional[Path] = None,
    source_roots: Optional[Sequence[Tuple[str, Path]]] = None,
) -> dict:
    root = Path(work_dir or _default_hub_import_work_dir()).expanduser()
    preview_dir = root / _timestamp_id()
    package = build_hub_import_preview_package(hub_root or Path.home() / ".skillshub", source_roots=source_roots, out_dir=preview_dir)
    apply_plan = execute_hub_import_apply(Path(str(package["preview_json"])))
    return {
        "ok": True,
        "record_type": "skill-sync-dashboard-hub-import-preview",
        "mode": "dry_run",
        "writes_files": False,
        "preview": {
            "out_dir": package.get("out_dir"),
            "preview_json": package.get("preview_json"),
            "preview_md": package.get("preview_md"),
            "action_summary": package.get("action_summary"),
            "review_required": package.get("review_required"),
            "actions": len(package.get("actions", [])) if isinstance(package.get("actions"), list) else 0,
        },
        "apply_plan": {
            "dry_run": apply_plan.get("dry_run"),
            "allowed": apply_plan.get("allowed"),
            "blocked": apply_plan.get("blocked"),
            "total": apply_plan.get("total"),
            "items": apply_plan.get("items", [])[:40],
        },
    }


def _load_peer_status_files(peer_status_files: Dict[str, Path]) -> Dict[str, dict]:
    peers = {}
    for peer_id, path in peer_status_files.items():
        try:
            data = json.loads(path.expanduser().read_text(encoding="utf-8"))
        except Exception as exc:
            peers[peer_id] = {
                "id": peer_id,
                "health": "red",
                "error": str(exc),
            }
            continue
        if isinstance(data, dict):
            peers[peer_id] = data
    return peers


def _load_remote_peer_status(remote: Remote, remote_peer_status_paths: Dict[str, str]) -> Dict[str, dict]:
    peers = {}
    for peer_id, path in remote_peer_status_paths.items():
        try:
            data = json.loads(remote.get_bytes(path).decode("utf-8"))
        except Exception as exc:
            peers[peer_id] = {
                "id": peer_id,
                "peer_id": peer_id,
                "health": "red",
                "error": str(exc),
                "status_source": "webdav",
                "status_path": path,
            }
            continue
        if isinstance(data, dict):
            copied = dict(data)
            copied.setdefault("id", peer_id)
            copied.setdefault("peer_id", peer_id)
            copied["status_source"] = "webdav"
            copied["status_path"] = path
            peers[peer_id] = copied
    return peers


def _safe_tool_projection(snapshot_dir: Path) -> dict:
    try:
        return build_tool_projection(snapshot_dir)
    except ProjectionError as exc:
        return {"ok": False, "error": str(exc), "tools": []}


def _safe_hub_import_diagnosis() -> dict:
    try:
        data = build_hub_import_diagnosis()
        data["ok"] = True
        return data
    except Exception as exc:  # pragma: no cover - diagnosis should not break dashboard
        return {"ok": False, "error": str(exc), "summary": {}, "items": []}


def _default_hub_import_work_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "skill-sync-sidecar" / "work" / "hub-import-preview"


def _timestamp_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _freshness_info(last_seen_at: Optional[str]) -> dict:
    if not last_seen_at:
        return {"state": "unknown", "label": "未知", "age_seconds": None}
    parsed = _parse_datetime(last_seen_at)
    if not parsed:
        return {"state": "unknown", "label": "时间不可解析", "age_seconds": None}
    age_seconds = max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))
    if age_seconds < 10 * 60:
        state = "fresh"
    elif age_seconds < 30 * 60:
        state = "aging"
    else:
        state = "stale"
    return {"state": state, "label": _age_label(age_seconds), "age_seconds": age_seconds}


def _parse_datetime(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_label(age_seconds: int) -> str:
    if age_seconds < 60:
        return "刚刚"
    if age_seconds < 3600:
        return f"{age_seconds // 60} 分钟前"
    if age_seconds < 86400:
        return f"{age_seconds // 3600} 小时前"
    return f"{age_seconds // 86400} 天前"


def _status_last_seen_at(status: dict) -> Optional[str]:
    daemon = status.get("daemon_state") if isinstance(status.get("daemon_state"), dict) else {}
    snapshot = status.get("remote_snapshot") if isinstance(status.get("remote_snapshot"), dict) else {}
    for value in (
        status.get("published_at"),
        daemon.get("updated_at"),
        snapshot.get("created_at"),
        status.get("updated_at"),
    ):
        if value:
            return str(value)
    return None


def _merge_tool_projection(tools: list[dict], projection: dict) -> list[dict]:
    projection_by_id = {
        str(tool.get("id")): tool
        for tool in projection.get("tools", [])
        if isinstance(tool, dict)
    }
    merged = []
    for tool in tools:
        copied = dict(tool)
        projected = projection_by_id.get(str(tool.get("id")))
        if projected:
            summary = projected.get("summary", {}) if isinstance(projected.get("summary"), dict) else {}
            copied["projection"] = {
                "canonical_targeted": projected.get("canonical_targeted"),
                "missing": summary.get("missing", 0),
                "drift": summary.get("drift", 0),
                "unsupported_scope": summary.get("unsupported_scope", 0),
                "not_targeted": summary.get("not_targeted", 0),
                "blocked_error": summary.get("blocked_error", 0),
                "extra_local": len(projected.get("extra_local", [])) if isinstance(projected.get("extra_local"), list) else 0,
            }
        merged.append(copied)
    return merged


def _gateway_tool_overview(projection: dict) -> list[dict]:
    tools = []
    for projected in projection.get("tools", []):
        if not isinstance(projected, dict):
            continue
        summary = projected.get("summary", {}) if isinstance(projected.get("summary"), dict) else {}
        tools.append(
            {
                "id": projected.get("id"),
                "name": projected.get("name") or projected.get("id"),
                "path": ", ".join(str(root) for root in projected.get("roots", []) if root),
                "role": "远端投影",
                "installed": None,
                "state": "observer",
                "skills": projected.get("canonical_targeted"),
                "risk": {},
                "note": "Gateway 只观察 WebDAV canonical snapshot；不扫描 NAS 容器内的工具目录。",
                "projection": {
                    "canonical_targeted": projected.get("canonical_targeted"),
                    "missing": None,
                    "drift": None,
                    "unsupported_scope": summary.get("unsupported_scope", 0),
                    "not_targeted": summary.get("not_targeted", 0),
                    "blocked_error": summary.get("blocked_error", 0),
                    "extra_local": None,
                },
            }
        )
    return tools


def _device_overview(status: dict, peers: Optional[Dict[str, dict]] = None) -> list[dict]:
    peers = peers or {}
    sync_plan = status.get("sync_plan") if isinstance(status.get("sync_plan"), dict) else {}
    daemon = status.get("daemon_state") if isinstance(status.get("daemon_state"), dict) else {}
    blocked = sync_plan.get("blocked")
    local_overrides = sync_plan.get("local_overrides") if isinstance(sync_plan.get("local_overrides"), dict) else {}
    current_note = "同步正常，无待处理项" if status.get("health") == "green" else "需要查看待处理队列"
    last_seen_at = _status_last_seen_at(status)
    openclaw = peers.get("oc-vps") or peers.get("openclaw")
    openclaw_device = _peer_device(
        "oc-vps",
        "oc-vps / OpenClaw",
        "已接入设备",
        openclaw,
        fallback_policy="pull-only + local-only",
        fallback_note="OpenClaw 已部署 sidecar；本机 dashboard 尚未读取到 peer status 文件",
        fallback_local_policy=["disk-cleanup", "lark-cli-adapter"],
    )
    return [
        {
            "id": "mac",
            "name": "Mac 本机",
            "kind": "当前设备",
            "health": status.get("health", "unknown"),
            "skills": status.get("remote_snapshot", {}).get("total"),
            "snapshot_id": status.get("remote_snapshot", {}).get("snapshot_id"),
            "blocked": blocked,
            "policy": _policy_label(status.get("writer_policy"), daemon.get("writer_policy")),
            "note": current_note,
            "local_policy": local_overrides.get("skills", []),
            "last_seen_at": last_seen_at,
            "freshness": _freshness_info(last_seen_at),
        },
        openclaw_device,
    ]


def _gateway_device_overview(snapshot: dict, peers: Dict[str, dict]) -> list[dict]:
    mac = peers.get("mac")
    openclaw = peers.get("oc-vps") or peers.get("openclaw")
    gateway_last_seen_at = datetime.now(timezone.utc).isoformat()
    return [
        {
            "id": "gateway",
            "name": "Gateway / NAS",
            "kind": "观察台",
            "health": "green",
            "skills": snapshot.get("total"),
            "snapshot_id": snapshot.get("snapshot_id"),
            "blocked": 0,
            "policy": "read-only",
            "note": "直接读取 WebDAV canonical snapshot，不依赖 Mac 静态导出",
            "local_policy": [],
            "last_seen_at": gateway_last_seen_at,
            "freshness": _freshness_info(gateway_last_seen_at),
        },
        _peer_device(
            "mac",
            "Mac 本机",
            "已接入设备",
            mac,
            fallback_policy="push-pull",
            fallback_note="gateway 尚未读取到 Mac peer status；canonical snapshot 仍可观察",
            fallback_local_policy=[],
        ),
        _peer_device(
            "oc-vps",
            "oc-vps / OpenClaw",
            "已接入设备",
            openclaw,
            fallback_policy="pull-only + local-only",
            fallback_note="gateway 尚未读取到 OpenClaw peer status；canonical snapshot 仍可观察",
            fallback_local_policy=["disk-cleanup", "lark-cli-adapter"],
        ),
    ]


def _planned_device_overview() -> list[dict]:
    return [
        {
            "id": "win",
            "name": "Windows",
            "kind": "后续接入",
            "health": "not_configured",
            "skills": None,
            "snapshot_id": None,
            "blocked": None,
            "policy": "本阶段跳过",
            "note": "已从当前验收范围移出；后续需要三端同步时再安装 Agent。",
            "local_policy": [],
            "last_seen_at": None,
            "freshness": _freshness_info(None),
        },
    ]


def _peer_device(
    peer_id: str,
    name: str,
    kind: str,
    status: Optional[dict],
    fallback_policy: str,
    fallback_note: str,
    fallback_local_policy: list[str],
) -> dict:
    if not status:
        return {
            "id": peer_id,
            "name": name,
            "kind": kind,
            "health": "not_connected",
            "skills": None,
            "snapshot_id": None,
            "blocked": None,
            "policy": fallback_policy,
            "note": fallback_note,
            "local_policy": fallback_local_policy,
            "last_seen_at": None,
            "freshness": _freshness_info(None),
        }
    sync_plan = status.get("sync_plan") if isinstance(status.get("sync_plan"), dict) else {}
    remote_snapshot = status.get("remote_snapshot") if isinstance(status.get("remote_snapshot"), dict) else {}
    local_overrides = sync_plan.get("local_overrides") if isinstance(sync_plan.get("local_overrides"), dict) else {}
    health = status.get("health", "unknown")
    blocked = sync_plan.get("blocked")
    last_seen_at = _status_last_seen_at(status)
    if status.get("error"):
        note = f"读取 peer status 失败：{status.get('error')}"
    elif health == "green":
        note = "远端同步正常，无待处理项"
    elif health == "yellow":
        note = "远端有待审批或待处理项"
    elif health == "red":
        note = "远端状态异常，需要检查 sidecar"
    else:
        note = "远端状态未知"
    return {
        "id": peer_id,
        "name": name,
        "kind": kind,
        "health": health,
        "skills": remote_snapshot.get("total"),
        "snapshot_id": remote_snapshot.get("snapshot_id"),
        "blocked": blocked,
        "policy": sync_plan.get("writer_policy") or status.get("writer_policy") or fallback_policy,
        "note": note,
        "local_policy": local_overrides.get("skills", []),
        "last_seen_at": last_seen_at,
        "freshness": _freshness_info(last_seen_at),
    }


def _policy_label(preflight_policy: Optional[str], daemon_policy: Optional[str]) -> str:
    if daemon_policy and preflight_policy and daemon_policy != preflight_policy:
        return f"preflight {preflight_policy} / daemon {daemon_policy}"
    return daemon_policy or preflight_policy or "-"


def _aggregate_health(values: list[Optional[str]]) -> str:
    ranked = {"red": 3, "yellow": 2, "green": 1}
    worst = "green"
    for value in values:
        if value in {"not_configured", "not_connected", None}:
            continue
        if ranked.get(str(value), 0) > ranked.get(worst, 0):
            worst = str(value)
    return worst


def _operator_summary(status: dict, devices: list[dict], blocked_items: list[dict]) -> dict:
    health = _aggregate_health([status.get("health")] + [device.get("health") for device in devices])
    daemon = status.get("daemon_state") if isinstance(status.get("daemon_state"), dict) else {}
    snapshot = status.get("remote_snapshot") if isinstance(status.get("remote_snapshot"), dict) else {}
    mac = _find_device(devices, "mac")
    openclaw = _find_device(devices, "oc-vps")
    top_issue = _operator_top_issue(blocked_items)
    action_guide = _operator_action_guide(health, blocked_items)
    if health == "green":
        next_action = "同步链路正常；继续观察 Mac / OpenClaw 自动周期。"
    elif health == "yellow" and top_issue:
        next_action = action_guide.get("summary") or top_issue["action"]
    elif health == "yellow":
        next_action = "先处理待审批队列；OpenClaw 本地改动需要 approved-push 后再上行。"
    elif health == "red":
        next_action = "先修复状态文件、WebDAV 快照或 sidecar 进程异常。"
    else:
        next_action = "状态未知；先刷新 dashboard 或查看 sidecar 日志。"
    return {
        "headline": _headline_for_health(health),
        "next_action": next_action,
        "sync_path": "Mac / OpenClaw <-> WebDAV -> 各工具目录",
        "snapshot_id": snapshot.get("snapshot_id"),
        "daemon": {
            "status": daemon.get("daemon_status") or daemon.get("status"),
            "target": daemon.get("target"),
            "writer_policy": daemon.get("writer_policy"),
            "interval_seconds": daemon.get("interval_seconds"),
            "last_updated_at": daemon.get("updated_at"),
            "cycles_run": daemon.get("cycles_run"),
        },
        "devices": {
            "mac": _operator_device_line(mac),
            "openclaw": _operator_device_line(openclaw),
        },
        "deferred_devices": {"windows": "本阶段跳过，后续需要三端同步时再接入。"},
        "blocked_count": len(blocked_items),
        "top_issue": top_issue,
        "action_guide": action_guide,
    }


def _operator_top_issue(blocked_items: list[dict]) -> Optional[dict]:
    if not blocked_items:
        return None
    item = blocked_items[0]
    peer_id = item.get("peer_id")
    peer_name = item.get("peer_name")
    skill_id = item.get("skill_id")
    status_action = item.get("status_action")
    category = item.get("category")
    return {
        "peer_id": peer_id,
        "peer_name": peer_name,
        "skill_id": skill_id,
        "status_action": status_action,
        "category": category,
        "source": item.get("source"),
        "reason": item.get("reason"),
        "recommendation": item.get("recommendation"),
        "action": item.get("operator_action")
        or _operator_issue_action(peer_id, peer_name, skill_id, status_action, category),
        "command": item.get("operator_command"),
    }


def _operator_issue_action(
    peer_id: Optional[str],
    peer_name: Optional[str],
    skill_id: Optional[str],
    status_action: Optional[str],
    category: Optional[str],
) -> str:
    target = _operator_issue_target(peer_id, peer_name, skill_id)
    if category == "conflict":
        return f"先处理 {target} 冲突；生成 conflict package 后人工合并。"
    if category == "writer_policy" and status_action in {"push", "push_new"}:
        return f"先处理 {target}；确认后运行 approved-push。"
    return f"先处理 {target}；查看待审批队列。"


def _operator_issue_target(peer_id: Optional[str], peer_name: Optional[str], skill_id: Optional[str]) -> str:
    peer = peer_name or peer_id or "unknown-peer"
    return f"{peer} / {skill_id or 'unknown-skill'}"


def _operator_action_guide(health: str, blocked_items: list[dict]) -> dict:
    openclaw_push_items = [item for item in blocked_items if _is_openclaw_writer_policy_push(item)]
    if health == "green":
        return {
            "state": "green",
            "title": "现在不用处理",
            "summary": "Mac、OpenClaw、WebDAV 当前同步链路正常，没有待审批项。",
            "steps": [
                {
                    "title": "继续观察",
                    "detail": "保持面板打开即可；需要手动复查时刷新页面或运行状态检查。",
                    "command": "scripts/operator-status.sh",
                    "kind": "verify",
                }
            ],
            "note": "绿色表示没有需要你马上决断的同步工作。",
        }
    if health == "yellow" and openclaw_push_items:
        skill_ids = _operator_skill_ids(openclaw_push_items)
        skill_hint = f"（{'、'.join(skill_ids)}）" if len(skill_ids) <= 2 else "（见下方列表）"
        dry_run = _approved_push_batch_command(skill_ids)
        publish = _approved_push_batch_command(skill_ids, yes=True)
        return {
            "state": "yellow",
            "title": "现在需要人工审核",
            "summary": f"OpenClaw 有 {len(skill_ids)} 个本地 skill 变更{skill_hint}，sidecar 已暂停自动上传；先 approved-push dry-run 审核，确认安全后再推送到中央仓库。",
            "steps": [
                {
                    "title": "先检查，不上传",
                    "detail": "在 Mac 的 skill-sync-sidecar 仓库运行 dry-run。它只做预检和预览，不会写入 WebDAV。",
                    "command": dry_run,
                    "kind": "dry_run",
                },
                {
                    "title": "确认安全后再发布",
                    "detail": "只有 dry-run 显示 safe_to_push=true，且这些 skill 不再继续编辑时，才运行确认发布。",
                    "command": publish,
                    "kind": "publish",
                },
                {
                    "title": "刷新状态",
                    "detail": "发布后等 1-2 分钟，刷新本页或运行状态检查，确认 OpenClaw 从 yellow 恢复。",
                    "command": "scripts/operator-status.sh",
                    "kind": "verify",
                },
            ],
            "skills": skill_ids,
            "note": "如果 OpenClaw 上这些 skill 仍在被优化，先不要发布；等那边改完再走 dry-run -> --yes。",
        }
    if health == "yellow":
        return {
            "state": "yellow",
            "title": "现在需要查看待审批队列",
            "summary": f"当前有 {len(blocked_items)} 个待审批项，系统已暂停自动写入以避免误同步。",
            "steps": [
                {
                    "title": "先看待审批队列",
                    "detail": "查看下面的待审批队列，确认每个 skill 的来源设备、原因和建议命令。",
                    "kind": "review",
                },
                {
                    "title": "处理后刷新状态",
                    "detail": "处理完成后刷新本页，确认待审批数量下降。",
                    "command": "scripts/operator-status.sh",
                    "kind": "verify",
                },
            ],
            "note": "黄色通常表示安全门禁正在保护你的远端快照，不等于服务故障。",
        }
    if health == "red":
        return {
            "state": "red",
            "title": "同步链路异常",
            "summary": "当前不是审批问题，而是状态文件、WebDAV、Gateway 或 sidecar 进程可能异常。",
            "steps": [
                {
                    "title": "先检查状态",
                    "detail": "运行 operator status，看错误集中在 WebDAV、缓存还是 peer status。",
                    "command": "scripts/operator-status.sh",
                    "kind": "diagnose",
                },
                {
                    "title": "再看服务日志",
                    "detail": "如果状态检查仍为 red，再查看 Gateway / sidecar 容器日志。",
                    "kind": "logs",
                },
            ],
            "note": "红色时不要执行 approved-push，先恢复链路可读性。",
        }
    return {
        "state": "unknown",
        "title": "状态未知",
        "summary": "面板还没有拿到足够信息判断下一步。",
        "steps": [
            {
                "title": "刷新状态",
                "detail": "先刷新页面；如果仍未知，再运行状态检查。",
                "command": "scripts/operator-status.sh",
                "kind": "verify",
            }
        ],
        "note": "未知状态下先不要发布或删除任何 skill。",
    }


def _operator_skill_ids(items: Sequence[dict]) -> list[str]:
    skill_ids: list[str] = []
    seen: set[str] = set()
    for item in items:
        skill_id = item.get("skill_id")
        if not skill_id:
            continue
        skill = str(skill_id)
        if skill not in seen:
            skill_ids.append(skill)
            seen.add(skill)
    return skill_ids


def _approved_push_batch_command(skill_ids: Sequence[str], *, yes: bool = False) -> str:
    args = ["scripts/openclaw-approved-push-batch.sh"]
    if yes:
        args.append("--yes")
    args.extend(str(skill_id) for skill_id in skill_ids)
    return " ".join(args)


def _is_openclaw_writer_policy_push(item: dict) -> bool:
    if item.get("peer_id") not in {"oc-vps", "openclaw"}:
        return False
    if item.get("category") != "writer_policy":
        return False
    status_action = item.get("status_action")
    if status_action in {"push", "push_new", "local_new"}:
        return True
    reason = str(item.get("reason") or "")
    return "push" in reason


def _local_workspace_model(devices: list[dict], device_tools: list[dict], blocked_items: list[dict]) -> dict:
    mac = _find_device(devices, "mac")
    mac_tools = _find_device_tool_group(device_tools, "mac")
    tools = mac_tools.get("tools") if isinstance(mac_tools.get("tools"), list) else []
    mac_blocked = [item for item in blocked_items if item.get("peer_id") == "mac"]
    remote_blocked = [item for item in blocked_items if item.get("peer_id") != "mac"]
    reported = bool(mac_tools.get("reported"))
    return {
        "title": "本地 Skill 工作区",
        "scope": "local",
        "device_id": "mac",
        "device_name": mac.get("name") or "Mac 本机",
        "health": mac.get("health") or "unknown",
        "reported": reported,
        "freshness": mac_tools.get("freshness") or mac.get("freshness") or _freshness_info(None),
        "tools": tools,
        "total_skills": sum(int(tool.get("skills") or 0) for tool in tools if isinstance(tool, dict)),
        "blocked": len(mac_blocked),
        "operations": {
            "scan_local": True,
            "dry_run": True,
            "publish_to_central": len(mac_blocked) > 0,
            "operate_other_devices": False,
        },
        "primary_action": "连接本机执行器后可实时扫描本机 skill，并对本机变更做 dry-run。",
        "boundary": "这里默认只操作浏览器所在 Mac；其他设备只读展示，除非该设备自己的 Agent 暴露受控操作。",
        "remote_blocked_note": f"当前另有 {len(remote_blocked)} 个非本机待审批项，放在设备地图里处理。",
    }


def _central_repository_model(status: dict, *, snapshot: Optional[dict], tools: list[dict], blocked_items: list[dict]) -> dict:
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    projection_total = sum(int((tool.get("projection") or {}).get("canonical_targeted") or 0) for tool in tools if isinstance(tool, dict))
    return {
        "title": "中央仓库",
        "scope": "central",
        "role": "WebDAV canonical snapshot",
        "health": "green" if snapshot.get("snapshot_id") else "unknown",
        "snapshot_id": snapshot.get("snapshot_id"),
        "created_at": snapshot.get("created_at"),
        "total_skills": snapshot.get("total"),
        "protocol_version": snapshot.get("protocol_version"),
        "targeted_projection_total": projection_total,
        "blocked": len(blocked_items),
        "operations": {
            "read_snapshot": True,
            "accept_approved_push": True,
            "direct_edit": False,
            "operate_devices": False,
        },
        "boundary": "中央仓库是共享事实源；面板只展示它的状态，写入只能来自本机或设备 Agent 的显式 approved push。",
    }


def _device_map_model(devices: list[dict], planned_devices: list[dict], device_tools: list[dict], blocked_items: list[dict]) -> dict:
    device_tool_by_id = {str(group.get("device_id")): group for group in device_tools if isinstance(group, dict)}
    items = []
    for device in devices + planned_devices:
        device_id = str(device.get("id") or "")
        if not device_id:
            continue
        group = device_tool_by_id.get(device_id, {})
        blocked = [item for item in blocked_items if item.get("peer_id") == device_id or (device_id == "oc-vps" and item.get("peer_id") == "openclaw")]
        if device_id == "mac":
            capability = "本机可操作"
            operation_scope = "local"
        elif device_id == "gateway":
            capability = "只读聚合"
            operation_scope = "read_only"
        elif device_id in {"oc-vps", "openclaw"}:
            capability = "远端只读观察"
            operation_scope = "remote_read_only"
        elif device_id == "win":
            capability = "未接入"
            operation_scope = "planned"
        else:
            capability = "只读观察"
            operation_scope = "remote_read_only"
        items.append(
            {
                "id": device_id,
                "name": device.get("name") or device_id,
                "kind": device.get("kind"),
                "health": device.get("health"),
                "skills": device.get("skills"),
                "blocked": len(blocked) if blocked else device.get("blocked"),
                "policy": device.get("policy"),
                "freshness": group.get("freshness") or device.get("freshness") or _freshness_info(None),
                "reported": group.get("reported", False),
                "tool_count": len(group.get("tools") or []) if isinstance(group.get("tools"), list) else 0,
                "capability": capability,
                "operation_scope": operation_scope,
                "note": device.get("note"),
            }
        )
    return {
        "title": "设备地图",
        "scope": "devices",
        "items": items,
        "boundary": "设备地图用于观察其他设备真实状态；默认不跨设备执行写操作。",
    }


def _find_device_tool_group(device_tools: list[dict], device_id: str) -> dict:
    for group in device_tools:
        if group.get("device_id") == device_id:
            return group
    return {}


def _find_device(devices: list[dict], device_id: str) -> dict:
    for device in devices:
        if device.get("id") == device_id:
            return device
    return {}


def _headline_for_health(health: str) -> str:
    if health == "green":
        return "同步正常，无待处理项"
    if health == "yellow":
        return "存在待审批同步项"
    if health == "red":
        return "同步链路异常"
    return "同步状态未知"


def _operator_device_line(device: dict) -> str:
    if not device:
        return "未读取到状态"
    skills = device.get("skills")
    blocked = device.get("blocked")
    policy = device.get("policy")
    health = device.get("health")
    freshness = device.get("freshness") if isinstance(device.get("freshness"), dict) else {}
    freshness_label = freshness.get("label") or "-"
    return f"{health}; skills={skills if skills is not None else '-'}; blocked={blocked if blocked is not None else '-'}; policy={policy or '-'}; freshness={freshness_label}"


def _blocked_items(status: dict, peers: Dict[str, dict]) -> list[dict]:
    items: list[dict] = []
    items.extend(_blocked_report_items("mac", "Mac 本机", status))
    for peer_id, peer_status in peers.items():
        peer_name = "oc-vps / OpenClaw" if peer_id in {"oc-vps", "openclaw"} else peer_id
        items.extend(_blocked_report_items(peer_id, peer_name, peer_status))
    return items


def _blocked_report_items(peer_id: str, peer_name: str, status: dict) -> list[dict]:
    report = status.get("blocked_report") if isinstance(status.get("blocked_report"), dict) else {}
    raw_items = report.get("items") if isinstance(report.get("items"), list) else []
    sync_plan = status.get("sync_plan") if isinstance(status.get("sync_plan"), dict) else {}
    plan_items = sync_plan.get("blocked_items") if isinstance(sync_plan.get("blocked_items"), list) else []
    expected_total = sync_plan.get("blocked")
    if plan_items and len(raw_items) != expected_total:
        raw_items = plan_items
    items = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        copied = dict(item)
        copied["peer_id"] = peer_id
        copied["peer_name"] = peer_name
        copied.setdefault("source", "live_sync_plan" if raw_items is plan_items else "blocked_report")
        copied.setdefault("operator_action", _blocked_item_operator_action(copied))
        command = _blocked_item_operator_command(copied)
        if command:
            copied.setdefault("operator_command", command)
        items.append(copied)
    return items


def _blocked_item_operator_action(item: dict) -> str:
    peer_id = item.get("peer_id")
    peer_name = item.get("peer_name")
    skill_id = item.get("skill_id")
    status_action = item.get("status_action")
    category = item.get("category")
    if category == "conflict":
        return _operator_issue_action(peer_id, peer_name, skill_id, status_action, category)
    if _is_openclaw_writer_policy_push(item):
        if peer_id in {"oc-vps", "openclaw"}:
            return f"先在 Mac 运行 OpenClaw approved-push dry-run 审核 {skill_id or 'unknown-skill'}，确认后再 --yes 发布。"
        return _operator_issue_action(peer_id, peer_name, skill_id, status_action, category)
    if category == "delete":
        return f"先人工确认 {skill_id or 'unknown-skill'} 是否应删除；未确认前不要自动 apply。"
    return _operator_issue_action(peer_id, peer_name, skill_id, status_action, category)


def _blocked_item_operator_command(item: dict) -> Optional[str]:
    skill_id = item.get("skill_id")
    if not skill_id:
        return None
    if item.get("category") == "writer_policy" and item.get("status_action") in {"push", "push_new", "local_new"}:
        if item.get("peer_id") in {"oc-vps", "openclaw"}:
            return f"scripts/openclaw-approved-push-batch.sh {skill_id}"
        return f"skill-sync approved-push --skill-id {skill_id} --dry-run"
    if item.get("category") == "conflict":
        return "skill-sync conflict-package --skill-id " + str(skill_id)
    return None


def _device_tool_overview(devices: list[dict], peers: Dict[str, dict]) -> list[dict]:
    groups = []
    for device in devices:
        device_id = str(device.get("id") or "")
        if device_id == "gateway":
            continue
        peer = peers.get(device_id)
        if not peer and device_id == "oc-vps":
            peer = peers.get("openclaw")
        tools = peer.get("tools") if isinstance(peer, dict) and isinstance(peer.get("tools"), list) else None
        reported = tools is not None
        last_seen_at = _status_last_seen_at(peer) if isinstance(peer, dict) else device.get("last_seen_at")
        groups.append(
            {
                "device_id": device_id,
                "device_name": device.get("name") or device_id,
                "health": device.get("health"),
                "reported": reported,
                "peer_status_version": peer.get("peer_status_version") if isinstance(peer, dict) else None,
                "last_seen_at": last_seen_at,
                "freshness": _freshness_info(last_seen_at),
                "note": "设备 Agent 已上报工具实测状态" if reported else _missing_tool_status_note(device, peer),
                "tools": tools if reported else [_unknown_tool_status(device, peer)],
            }
        )
    return groups


def _missing_tool_status_note(device: dict, peer: Optional[dict]) -> str:
    if not peer:
        return "尚未读取到该设备的 peer status。"
    if device.get("health") in {"not_configured", "not_connected"}:
        return "设备尚未接入 sidecar Agent。"
    return "该设备的 peer status 仍是旧格式，尚未上报 tools[]。"


def _unknown_tool_status(device: dict, peer: Optional[dict]) -> dict:
    state = "unsupported" if device.get("health") in {"not_configured", "not_connected"} else "unknown"
    return {
        "id": "tool-status",
        "name": "工具实测",
        "roots": [],
        "path": "",
        "role": "设备实测",
        "installed": None,
        "state": state,
        "skills": None,
        "risk": {},
        "measured_at": _status_last_seen_at(peer) if isinstance(peer, dict) else None,
        "note": _missing_tool_status_note(device, peer),
    }


def serve_dashboard(host: str, port: int, config: DashboardConfig) -> None:
    status_provider = lambda: build_dashboard_status(config)
    preview_provider = lambda: build_hub_import_preview_response(config.hub_import_work_dir)
    handler = _handler_factory(status_provider, preview_provider)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"skill-sync dashboard: http://{host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def serve_gateway(host: str, port: int, config: GatewayConfig) -> None:
    cache = RemoteSnapshotCache(config.remote, config.remote_prefix, config.cache_dir, config.refresh_interval_seconds)

    def status_provider() -> dict:
        try:
            remote_peers = _load_remote_peer_status(config.remote, config.remote_peer_status_paths or {})
            return build_gateway_status(cache, config.peer_status_files, remote_peers)
        except (RemoteError, OSError, json.JSONDecodeError) as exc:
            return {
                "ok": False,
                "health": "red",
                "mode": "gateway",
                "error": str(exc),
                "dashboard": {
                    "health": "red",
                    "blocked": 0,
                    "operator": {
                        "headline": "同步链路异常",
                        "next_action": "先检查 WebDAV 连接、认证或 gateway 缓存目录。",
                        "sync_path": "WebDAV -> Gateway",
                        "snapshot_id": None,
                        "devices": {},
                        "blocked_count": 0,
                    },
                    "blocked_items": [],
                    "devices": [],
                    "tools": [],
                    "device_tools": [],
                },
            }

    preview_provider = lambda: build_hub_import_preview_response(config.hub_import_work_dir)
    handler = _handler_factory(status_provider, preview_provider)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"skill-sync gateway: http://{host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _handler_factory(status_provider: Callable[[], dict], hub_import_preview_provider: Optional[Callable[[], dict]] = None):
    summary_cache = DashboardSummaryCache(
        status_provider,
        timeout_seconds=_float_env("SKILL_SYNC_SUMMARY_TIMEOUT_SECONDS", 2.0),
        stale_after_seconds=_float_env("SKILL_SYNC_SUMMARY_STALE_AFTER_SECONDS", 120.0),
    )

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "SkillSyncDashboard/0"

        def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
            path = self.path.split("?", 1)[0]
            if path in {"", "/"}:
                self._send(200, "text/html; charset=utf-8", DASHBOARD_HTML.encode("utf-8"))
                return
            if path == "/api/status":
                try:
                    payload = status_provider()
                    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
                    self._send(200, "application/json; charset=utf-8", body)
                except Exception as exc:  # pragma: no cover - defensive server boundary
                    body = json.dumps({"ok": False, "health": "red", "error": str(exc)}, ensure_ascii=False).encode("utf-8")
                    self._send(500, "application/json; charset=utf-8", body)
                return
            if path == "/api/summary":
                status_code, payload = summary_cache.get_summary()
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self._send(status_code, "application/json; charset=utf-8", body)
                return
            if path == "/healthz":
                payload = {
                    "ok": True,
                    "service": "skill-sync-dashboard",
                    "version": "0",
                    "time": datetime.now(timezone.utc).isoformat(),
                    "summary_cache": summary_cache.healthz(),
                }
                self._send(200, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                return
            if path == "/favicon.ico":
                self._send(204, "image/x-icon", b"")
                return
            self._send(404, "text/plain; charset=utf-8", b"not found\n")

        def do_POST(self) -> None:  # noqa: N802 - stdlib hook name
            path = self.path.split("?", 1)[0]
            self._drain_request_body()
            if path == "/api/hub-import-preview":
                if hub_import_preview_provider is None:
                    self._send(404, "application/json; charset=utf-8", b'{"ok":false,"error":"hub import preview is unavailable"}\n')
                    return
                try:
                    payload = hub_import_preview_provider()
                    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
                    self._send(200, "application/json; charset=utf-8", body)
                except Exception as exc:  # pragma: no cover - defensive server boundary
                    body = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8")
                    self._send(500, "application/json; charset=utf-8", body)
                return
            self._send(404, "application/json; charset=utf-8", b'{"ok":false,"error":"not found"}\n')

        def do_HEAD(self) -> None:  # noqa: N802 - stdlib hook name
            path = self.path.split("?", 1)[0]
            if path in {"", "/"}:
                self._send(200, "text/html; charset=utf-8", b"")
                return
            if path in {"/api/status", "/api/summary"}:
                self._send(200, "application/json; charset=utf-8", b"")
                return
            if path == "/healthz":
                self._send(200, "application/json; charset=utf-8", b"")
                return
            if path == "/favicon.ico":
                self._send(204, "image/x-icon", b"")
                return
            self._send(404, "text/plain; charset=utf-8", b"")

        def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib hook signature
            return

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _drain_request_body(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                length = 0
            if length > 0:
                self.rfile.read(length)

    return DashboardHandler


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Skill Sync Sidecar</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f9fc;
      --panel: #ffffff;
      --ink: #172033;
      --muted: #667085;
      --line: #dde4ee;
      --green: #147d50;
      --yellow: #9a6700;
      --red: #c63232;
      --blue: #2557a7;
      --soft: #eef2f7;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .portal-link {
      display: inline-block;
      margin: 10px 24px 0 max(24px, calc((100vw - 1120px) / 2 + 24px));
      font-size: 13px;
      color: var(--muted);
      text-decoration: none;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px max(24px, calc((100vw - 1120px) / 2 + 24px));
      border-bottom: 1px solid var(--line);
      background: #fff;
    }
    h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 650;
      letter-spacing: 0;
    }
    .brand {
      display: grid;
      gap: 2px;
    }
    .brand-subtitle {
      color: var(--muted);
      font-size: 12px;
    }
    main {
      max-width: 1120px;
      margin: 0 auto;
      padding: 16px 24px 32px;
    }
    .toolbar {
      display: flex;
      align-items: center;
      gap: 12px;
      color: var(--muted);
      min-width: 0;
    }
    button {
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
      border-radius: 6px;
      padding: 7px 12px;
      font: inherit;
      font-weight: 650;
      cursor: pointer;
    }
    button:disabled {
      cursor: default;
      color: var(--muted);
      background: #f3f5f8;
    }
    button:hover { border-color: #aeb7c6; }
    .status-strip {
      display: grid;
      grid-template-columns: 1.2fr repeat(4, minmax(100px, .7fr));
      gap: 10px;
      margin-bottom: 12px;
    }
    .status-chip {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      background: #fff;
      min-width: 0;
    }
    .status-chip.primary {
      border-left: 4px solid #d8a300;
      padding-left: 10px;
      background: #fff;
    }
    .status-chip-label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      margin-bottom: 3px;
    }
    .status-chip-value {
      color: var(--ink);
      font-size: 18px;
      font-weight: 820;
      line-height: 1.15;
      overflow-wrap: anywhere;
    }
    .operator-band {
      display: grid;
      grid-template-columns: minmax(320px, 1.35fr) minmax(240px, .65fr);
      gap: 12px;
      margin-bottom: 12px;
    }
    .scope-switchboard {
      display: grid;
      grid-template-columns: minmax(420px, 1.55fr) minmax(260px, .75fr);
      gap: 12px;
      margin-bottom: 12px;
      align-items: stretch;
    }
    .scope-readonly-rail {
      display: grid;
      gap: 12px;
      min-width: 0;
    }
    .scope-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 14px 16px;
      min-width: 0;
    }
    .scope-card.local {
      border-left: 4px solid var(--blue);
      background: #fafdff;
      padding: 18px;
    }
    .scope-card.readonly {
      background: #fbfcfe;
    }
    .scope-card h2 {
      margin: 0;
      font-size: 16px;
      line-height: 1.25;
    }
    .scope-card-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 8px;
    }
    .scope-card-count {
      color: var(--ink);
      font-size: 18px;
      font-weight: 820;
      line-height: 1.2;
      margin-bottom: 4px;
    }
    .scope-card.local .scope-card-count {
      font-size: 24px;
      margin-bottom: 8px;
    }
    .scope-card-note {
      color: var(--muted);
      min-height: 38px;
      overflow-wrap: anywhere;
    }
    .scope-card-focus {
      color: var(--ink);
      font-weight: 720;
      line-height: 1.45;
      margin: 10px 0 2px;
    }
    .scope-card-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }
    .decision-console {
      display: grid;
      grid-template-columns: minmax(280px, .75fr) minmax(420px, 1.25fr);
      gap: 12px;
      margin-bottom: 12px;
    }
    .decision-status {
      border-left: 4px solid var(--muted);
      display: flex;
      flex-direction: column;
      justify-content: flex-start;
    }
    .decision-status.green { border-left-color: var(--green); background: #fbfffd; }
    .decision-status.yellow { border-left-color: #d8a300; background: #fff; }
    .decision-status.red { border-left-color: var(--red); background: #fffafa; }
    .decision-next {
      margin-bottom: 0;
    }
    .operator-title-row {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 8px;
    }
    .operator-title-row .operator-title {
      margin-bottom: 0;
    }
    .technical-summary {
      margin-top: 10px;
      border-top: 1px solid var(--line);
      padding-top: 8px;
    }
    .technical-summary > summary {
      cursor: pointer;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
    }
    .decision-boundary {
      grid-column: 1 / -1;
      padding: 0;
      background: #fff;
    }
    .decision-boundary > summary {
      cursor: pointer;
      list-style: none;
      display: grid;
      grid-template-columns: 86px minmax(0, 1fr) auto;
      gap: 12px;
      align-items: center;
      padding: 10px 14px;
    }
    .decision-boundary > summary::-webkit-details-marker {
      display: none;
    }
    .decision-boundary > summary::after {
      content: "展开";
      justify-self: end;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      grid-column: 3;
    }
    .decision-boundary[open] > summary {
      border-bottom: 1px solid var(--line);
    }
    .decision-boundary[open] > summary::after {
      content: "收起";
    }
    .boundary-title {
      font-weight: 800;
      color: var(--ink);
    }
    .boundary-body {
      padding: 10px 14px 12px;
    }
    .section-label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      margin-bottom: 6px;
    }
    .operator-title {
      font-size: 24px;
      font-weight: 720;
      margin-bottom: 8px;
    }
    .operator-verdict {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      border-radius: 999px;
      padding: 2px 9px;
      margin-bottom: 8px;
      border: 1px solid var(--line);
      background: #fafbfc;
      color: var(--ink);
      font-size: 12px;
      font-weight: 750;
      letter-spacing: 0;
      max-width: 100%;
      overflow-wrap: anywhere;
    }
    .operator-verdict.green { border-color: #b8d8c8; color: var(--green); background: #f1faf5; }
    .operator-verdict.yellow { border-color: #e8d29c; color: var(--yellow); background: #fff8e6; }
    .operator-verdict.red { border-color: #efb8b8; color: var(--red); background: #fff1f1; }
    .operator-text {
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    #operator-path {
      font-family: inherit;
      font-size: 13px;
      color: var(--ink);
    }
    #operator-snapshot {
      margin-top: 8px;
      color: var(--muted);
    }
    .operator-brief {
      display: none;
      gap: 6px;
      margin: 10px 0;
      color: var(--muted);
      font-size: 12px;
    }
    .brief-line {
      display: grid;
      grid-template-columns: 76px minmax(0, 1fr);
      gap: 10px;
    }
    .brief-label {
      color: var(--muted);
      font-weight: 650;
    }
    .brief-value {
      color: var(--ink);
      overflow-wrap: anywhere;
    }
    .device-lines {
      display: grid;
      gap: 8px;
    }
    .device-line {
      display: grid;
      grid-template-columns: 86px minmax(0, 1fr);
      gap: 10px;
    }
    .status-band {
      display: grid;
      grid-template-columns: minmax(220px, 1.2fr) repeat(4, minmax(120px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .panel, .metric {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      min-width: 0;
    }
    .scope-list {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin-top: 10px;
    }
    .scope-line {
      display: grid;
      grid-template-columns: 44px minmax(0, 1fr);
      gap: 10px;
      align-items: start;
      color: var(--muted);
      font-size: 13px;
    }
    .scope-line strong {
      color: var(--ink);
      font-size: 12px;
    }
    .metric-label {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 6px;
    }
    .metric-value {
      font-size: 22px;
      font-weight: 680;
      letter-spacing: 0;
      overflow-wrap: anywhere;
    }
    .health {
      display: flex;
      align-items: center;
      gap: 10px;
      min-height: 62px;
    }
    .dot {
      width: 14px;
      height: 14px;
      border-radius: 50%;
      background: var(--muted);
      flex: 0 0 auto;
    }
    .green .dot { background: var(--green); }
    .yellow .dot { background: var(--yellow); }
    .red .dot { background: var(--red); }
    .health-title {
      font-size: 24px;
      font-weight: 720;
      text-transform: capitalize;
    }
    .health-subtitle {
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    .grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(320px, .8fr);
      gap: 16px;
    }
    .section-title {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      margin: 18px 0 10px;
    }
    .section-title h2 { margin: 0; }
    .section-help {
      color: var(--muted);
      font-size: 12px;
    }
    .cards {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }
    .device-card, .tool-card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      min-width: 0;
    }
    .planned-card {
      border-style: dashed;
      background: #fbfcfe;
    }
    .device-tool-group {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      margin-bottom: 12px;
      min-width: 0;
    }
    .device-tool-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      margin-bottom: 12px;
    }
    .device-tool-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }
    .card-head {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: flex-start;
      margin-bottom: 10px;
    }
    .card-name {
      font-size: 16px;
      font-weight: 700;
      overflow-wrap: anywhere;
    }
    .card-kind {
      color: var(--muted);
      font-size: 12px;
      margin-top: 2px;
    }
    .card-note {
      color: var(--muted);
      min-height: 38px;
      overflow-wrap: anywhere;
    }
    .card-stats {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-top: 12px;
    }
    .mini-stat {
      border-top: 1px solid var(--line);
      padding-top: 8px;
    }
    .mini-label {
      color: var(--muted);
      font-size: 12px;
    }
    .mini-value {
      font-weight: 700;
      overflow-wrap: anywhere;
    }
    .mini-value.subtle {
      color: var(--muted);
      font-weight: 600;
      font-size: 12px;
    }
    .freshness {
      display: inline-flex;
      align-items: center;
      max-width: 100%;
      border-radius: 999px;
      padding: 2px 7px;
      border: 1px solid var(--line);
      font-size: 12px;
      font-weight: 700;
      color: var(--muted);
      background: #f6f8fb;
      overflow-wrap: anywhere;
    }
    .freshness.fresh {
      color: #247a4a;
      background: #e8f6ee;
      border-color: #bfe5cc;
    }
    .freshness.aging {
      color: #8a5b00;
      background: #fff7dd;
      border-color: #f1d58a;
    }
    .freshness.stale {
      color: #9b2c2c;
      background: #fdecec;
      border-color: #f5c2c2;
    }
    h2 {
      font-size: 14px;
      margin: 0 0 10px;
      letter-spacing: 0;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }
    th, td {
      padding: 9px 8px;
      border-top: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      overflow-wrap: anywhere;
    }
    th {
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
    }
    .empty {
      color: var(--muted);
      padding: 12px 0 2px;
    }
    .stack {
      display: grid;
      gap: 16px;
    }
    .kv {
      display: grid;
      grid-template-columns: 132px minmax(0, 1fr);
      gap: 8px 12px;
      padding-top: 4px;
    }
    .plan-strip {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 8px;
      margin: 8px 0 12px;
    }
    .plan-cell {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px;
      min-width: 0;
    }
    .panel-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 8px;
    }
    .panel-head h2 { margin: 0; }
    .key { color: var(--muted); }
    .value { overflow-wrap: anywhere; }
    .action-cell {
      display: grid;
      gap: 4px;
      min-width: 180px;
    }
    .action-primary {
      color: var(--ink);
      overflow-wrap: anywhere;
    }
    .action-command {
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    .review-queue {
      margin-bottom: 12px;
      border-left: 4px solid #d8a300;
    }
    .review-queue-summary {
      color: var(--muted);
      margin-bottom: 10px;
      overflow-wrap: anywhere;
    }
    .review-list {
      display: grid;
      gap: 6px;
    }
    .review-more {
      border: 1px dashed var(--line);
      border-radius: 8px;
      padding: 9px 12px;
      color: var(--muted);
      background: #fff;
      font-weight: 700;
    }
    .review-item {
      display: grid;
      grid-template-columns: minmax(180px, .85fr) minmax(0, 1.25fr) minmax(150px, auto);
      gap: 12px;
      align-items: start;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      background: #fbfcfe;
      min-width: 0;
    }
    .review-skill {
      font-weight: 800;
      overflow-wrap: anywhere;
    }
    .review-source {
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .review-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 6px;
    }
    .review-meta-item {
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      background: #fff;
      font-size: 11px;
      font-weight: 720;
      line-height: 1.2;
      padding: 3px 7px;
      max-width: 100%;
      overflow-wrap: anywhere;
    }
    .review-action {
      color: var(--ink);
      overflow-wrap: anywhere;
    }
    .review-next-step {
      color: var(--muted);
      font-size: 12px;
      margin-top: 4px;
      overflow-wrap: anywhere;
    }
    .review-controls {
      display: grid;
      justify-items: end;
      gap: 8px;
      min-width: 0;
    }
    .review-controls button {
      width: 100%;
      white-space: nowrap;
    }
    .review-command {
      margin-top: 6px;
    }
    .review-command summary {
      cursor: pointer;
      color: var(--blue);
      font-size: 12px;
      font-weight: 700;
    }
    .action-guide {
      margin-bottom: 12px;
    }
    .action-guide.green { border-color: #b8d8c8; background: #fbfffd; }
    .action-guide.yellow { border-color: #e8d29c; background: #fff; }
    .action-guide.red { border-color: #efb8b8; background: #fffafa; }
    .guide-summary {
      color: var(--ink);
      font-weight: 700;
      font-size: 16px;
      margin-bottom: 8px;
      overflow-wrap: anywhere;
    }
    .guide-note {
      color: var(--muted);
      margin-top: 10px;
      overflow-wrap: anywhere;
    }
    .guide-skills {
      color: var(--muted);
      margin: 8px 0 12px;
      overflow-wrap: anywhere;
    }
    .skill-chip-row {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
    }
    .skill-chip {
      display: inline-flex;
      align-items: center;
      max-width: 100%;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 2px 8px;
      background: #f7f9fc;
      color: var(--ink);
      font-size: 12px;
      font-weight: 700;
      overflow-wrap: anywhere;
    }
    .skill-more {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }
    .guide-steps {
      list-style: none;
      padding: 0;
      margin: 12px 0 0;
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .guide-details {
      margin-top: 10px;
      border-top: 1px solid var(--line);
      padding-top: 8px;
    }
    .guide-details > summary {
      cursor: pointer;
      color: var(--blue);
      font-size: 12px;
      font-weight: 750;
    }
    .guide-step {
      display: grid;
      grid-template-columns: 24px minmax(0, 1fr);
      gap: 10px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
    }
    .step-index {
      width: 24px;
      height: 24px;
      border-radius: 50%;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: #eef2f7;
      color: var(--ink);
      font-weight: 800;
      font-size: 12px;
    }
    .step-title {
      font-weight: 760;
      margin-bottom: 3px;
      overflow-wrap: anywhere;
    }
    .step-detail {
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    .command-detail {
      margin-top: 8px;
    }
    .command-detail summary {
      cursor: pointer;
      color: var(--blue);
      font-size: 12px;
      font-weight: 700;
    }
    .command-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: stretch;
      margin-top: 8px;
    }
    .guide-command {
      margin: 0;
      padding: 9px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #f7f9fc;
      color: var(--ink);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .copy-button {
      min-width: 58px;
      white-space: nowrap;
    }
    .executor-panel {
      margin-top: 14px;
      padding-top: 12px;
      border-top: 1px solid var(--line);
      display: grid;
      gap: 10px;
    }
    .executor-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .executor-status {
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    .executor-output {
      display: none;
      margin: 0;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #f7f9fc;
      color: var(--ink);
      max-height: 340px;
      overflow: auto;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .workbench-grid {
      display: grid;
      grid-template-columns: minmax(340px, 1.2fr) minmax(280px, .9fr) minmax(260px, .9fr);
      gap: 12px;
      margin: 12px 0 0;
    }
    .workspace-overview {
      margin-bottom: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      overflow: hidden;
    }
    .workspace-overview-head {
      padding: 14px 16px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      border-bottom: 1px solid var(--line);
      background: #fbfcfe;
    }
    .overview-title {
      display: grid;
      gap: 2px;
      min-width: 0;
    }
    .overview-title strong {
      font-size: 15px;
      font-weight: 820;
    }
    .overview-subtitle {
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
      overflow-wrap: anywhere;
    }
    .local-workspace-panel {
      border-left: 4px solid var(--blue);
      background: #fafdff;
    }
    .workspace-eyebrow {
      color: var(--blue);
      font-size: 12px;
      font-weight: 800;
      margin-bottom: 4px;
    }
    .workbench-full {
      grid-column: auto;
    }
    .workspace-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 8px;
    }
    .workspace-title h2 {
      margin: 0;
      font-size: 15px;
    }
    .workspace-subtitle {
      color: var(--muted);
      margin-bottom: 10px;
      overflow-wrap: anywhere;
    }
    .workspace-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 10px 0 12px;
    }
    .workspace-actions button.primary {
      background: var(--ink);
      color: #fff;
      border-color: var(--ink);
    }
    .local-action-note {
      color: var(--muted);
      font-size: 12px;
      margin: -4px 0 10px;
      overflow-wrap: anywhere;
    }
    .workspace-metrics {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin: 12px 0;
    }
    .workspace-metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px 10px;
      background: #fff;
      min-width: 0;
    }
    .workspace-metric-value {
      font-size: 20px;
      font-weight: 800;
      line-height: 1.1;
      overflow-wrap: anywhere;
    }
    .workspace-metric-label {
      color: var(--muted);
      font-size: 12px;
      margin-top: 3px;
    }
    .workspace-tools {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 6px;
      margin-top: 10px;
    }
    .workspace-tool {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 6px 8px;
      min-width: 0;
      background: #fff;
    }
    .workspace-tool-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto auto;
      gap: 8px;
      align-items: center;
    }
    .workspace-tool-name {
      font-weight: 700;
      overflow-wrap: anywhere;
    }
    .workspace-tool-count {
      font-weight: 800;
    }
    .device-map-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 6px;
    }
    .device-map-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px;
      background: #fff;
      min-width: 0;
    }
    .device-map-item .card-head {
      margin-bottom: 6px;
    }
    .device-map-item .card-name {
      font-size: 14px;
    }
    .device-map-item .card-kind {
      display: none;
    }
    .device-map-meta {
      display: grid;
      gap: 2px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }
    .readonly-kicker {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      margin-bottom: 8px;
    }
    .boundary-note {
      color: var(--muted);
      border-top: 1px solid var(--line);
      padding-top: 9px;
      margin-top: 10px;
      overflow-wrap: anywhere;
    }
    .advanced-diagnostics {
      margin-top: 16px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      overflow: hidden;
    }
    .advanced-diagnostics > summary {
      cursor: pointer;
      list-style: none;
      padding: 13px 16px;
      font-weight: 720;
      color: var(--ink);
      background: #fafbfd;
      border-bottom: 1px solid transparent;
    }
    .advanced-diagnostics[open] > summary {
      border-bottom-color: var(--line);
    }
    .advanced-diagnostics > summary::-webkit-details-marker {
      display: none;
    }
    .advanced-diagnostics > summary::after {
      content: "展开";
      float: right;
      color: var(--muted);
      font-weight: 650;
      font-size: 12px;
    }
    .advanced-diagnostics[open] > summary::after {
      content: "收起";
    }
    .advanced-body {
      padding: 16px;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      border-radius: 999px;
      padding: 2px 8px;
      border: 1px solid var(--line);
      background: #fafbfc;
      color: var(--ink);
      font-size: 12px;
      max-width: 100%;
      overflow-wrap: anywhere;
    }
    .pill.green { border-color: #b8d8c8; color: var(--green); background: #f1faf5; }
    .pill.yellow { border-color: #e8d29c; color: var(--yellow); background: #fff8e6; }
    .pill.red { border-color: #efb8b8; color: var(--red); background: #fff1f1; }
    .mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
    }
    .error {
      color: var(--red);
      background: #fff1f1;
      border: 1px solid #efb8b8;
      border-radius: 8px;
      padding: 12px;
      margin-bottom: 16px;
      display: none;
    }
    @media (max-width: 860px) {
      header { align-items: flex-start; flex-direction: column; }
      .operator-band { grid-template-columns: 1fr; }
      .status-strip { grid-template-columns: 1fr 1fr; }
      .scope-switchboard { grid-template-columns: 1fr; }
      .scope-readonly-rail { grid-template-columns: 1fr 1fr; }
      .decision-console { grid-template-columns: 1fr; }
      .decision-boundary { grid-column: auto; }
      .scope-list { grid-template-columns: 1fr; }
      .review-item { grid-template-columns: 1fr; }
      .status-band { grid-template-columns: 1fr 1fr; }
      .status-band .panel { grid-column: 1 / -1; }
      .workbench-grid { grid-template-columns: 1fr; }
      .cards { grid-template-columns: 1fr; }
      .device-tool-grid { grid-template-columns: 1fr; }
      .device-map-grid { grid-template-columns: 1fr 1fr; }
      .guide-steps { grid-template-columns: 1fr; }
      .workspace-metrics { grid-template-columns: 1fr; }
      .grid { grid-template-columns: 1fr; }
      .plan-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 560px) {
      main { padding: 14px; }
      .status-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .scope-switchboard { grid-template-columns: 1fr; }
      .scope-readonly-rail { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
      .scope-card { padding: 12px; }
      .scope-switchboard { gap: 8px; }
      .scope-card-head { margin-bottom: 4px; }
      .scope-card-count { font-size: 16px; margin-bottom: 2px; }
      .scope-card.local .scope-card-count { font-size: 18px; margin-bottom: 4px; }
      .scope-card-focus { display: none; }
      .scope-card-note { font-size: 12px; line-height: 1.35; }
      .scope-card.readonly .scope-card-note { display: none; }
      .scope-card-actions { margin-top: 8px; }
      .scope-card-actions button { flex: 1 1 92px; padding: 7px 8px; }
      .scope-card-note { min-height: 0; }
      .status-chip { padding: 8px 10px; }
      .status-chip.primary { grid-column: auto; }
      .status-strip .status-chip:nth-child(4),
      .status-strip .status-chip:nth-child(5) {
        display: none;
      }
      .review-queue-summary {
        font-size: 12px;
        line-height: 1.4;
        margin-bottom: 6px;
      }
      .review-list {
        gap: 4px;
      }
      .review-item {
        grid-template-columns: minmax(0, 1fr) auto;
        gap: 4px 8px;
        padding: 8px 10px;
      }
      .review-source,
      .review-action {
        display: none;
      }
      .review-meta {
        margin-top: 4px;
      }
      .review-meta-item:not(:last-child),
      .review-command {
        display: none;
      }
      .review-controls {
        grid-column: 1 / -1;
        grid-template-columns: minmax(0, 1fr) auto;
        align-items: center;
        justify-items: stretch;
      }
      .review-controls .pill {
        justify-self: end;
      }
      .review-next-step {
        grid-column: 1 / -1;
        display: none;
      }
      .review-item > div:nth-child(2) {
        grid-column: 1 / -1;
      }
      .review-command {
        margin-top: 0;
      }
      .review-more {
        padding: 7px 10px;
        font-size: 12px;
      }
      .status-band { grid-template-columns: 1fr; }
      .kv { grid-template-columns: 1fr; }
      .plan-strip { grid-template-columns: 1fr; }
      .command-row { grid-template-columns: 1fr; }
      .device-map-grid { grid-template-columns: 1fr; }
      .decision-boundary > summary {
        grid-template-columns: minmax(0, 1fr) auto;
      }
      .decision-boundary > summary .boundary-title {
        grid-column: auto;
      }
      #operator-path {
        display: none;
      }
    }
  </style>
</head>
<body>
  <a class="portal-link" href="http://100.123.208.32:17172/portal">← 报表门户</a>
  <header>
    <div class="brand">
      <h1>Skill 同步工作台</h1>
      <div class="brand-subtitle">本机操作 · 中央仓库 · 设备状态</div>
    </div>
    <div class="toolbar">
      <span id="updated">Loading</span>
      <button id="refresh" type="button" title="刷新状态">刷新</button>
    </div>
  </header>
  <main>
    <div id="error" class="error"></div>
    <section class="status-strip" aria-label="同步状态摘要">
      <div class="status-chip primary">
        <div class="status-chip-label">当前状态</div>
        <div id="strip-health" class="status-chip-value">读取中</div>
      </div>
      <div class="status-chip">
        <div class="status-chip-label">待你处理</div>
        <div id="strip-blocked" class="status-chip-value">-</div>
      </div>
      <div class="status-chip">
        <div class="status-chip-label">本机 skill</div>
        <div id="strip-local" class="status-chip-value">-</div>
      </div>
      <div class="status-chip">
        <div class="status-chip-label">中央 skill</div>
        <div id="strip-central" class="status-chip-value">-</div>
      </div>
      <div class="status-chip">
        <div class="status-chip-label">设备</div>
        <div id="strip-devices" class="status-chip-value">-</div>
      </div>
    </section>
    <section class="scope-switchboard" aria-label="Skill 同步分区">
      <div class="scope-card local">
        <div class="scope-card-head">
          <h2>本机 Skill 工作区</h2>
          <span class="pill green">可操作</span>
        </div>
        <div id="scope-local-count" class="scope-card-count">-</div>
        <div id="scope-local-note" class="scope-card-note">只扫描和处理当前浏览器所在设备。</div>
        <div class="scope-card-focus">授权发现本机目录是管理本地 skill 的必要权限；这里的操作只影响当前设备，发布也必须显式推送到中央仓库。</div>
        <div class="scope-card-actions">
          <button id="scope-scan" type="button" class="primary" onclick="refreshLocalWorkspace()">扫描本机</button>
          <button id="scope-dry-run" type="button" onclick="runExecutorAction('dry_run')" disabled>预检待推送</button>
          <button id="scope-publish" type="button" onclick="runExecutorAction('publish')" disabled>推送到中央</button>
        </div>
      </div>
      <div class="scope-readonly-rail" aria-label="中央仓库和其他设备只读状态">
        <div class="scope-card readonly">
          <div class="scope-card-head">
            <h2>中央仓库</h2>
            <span class="pill">read-only</span>
          </div>
          <div id="scope-central-count" class="scope-card-count">-</div>
          <div id="scope-central-note" class="scope-card-note">WebDAV canonical snapshot，只接受显式 approved push。</div>
        </div>
        <div class="scope-card readonly">
          <div class="scope-card-head">
            <h2>其他设备</h2>
            <span class="pill">read-only</span>
          </div>
          <div id="scope-device-count" class="scope-card-count">-</div>
          <div id="scope-device-note" class="scope-card-note">Mac / OpenClaw / Windows 自己上报实测状态，Gateway 不远程改设备。</div>
        </div>
      </div>
    </section>
    <section id="review-queue-panel" class="review-queue panel" hidden>
      <div class="panel-head">
        <div>
          <div class="section-label">需要你判断</div>
          <h2>待审批清单</h2>
        </div>
        <span id="review-queue-count" class="pill">0</span>
      </div>
      <div id="review-queue-summary" class="review-queue-summary"></div>
      <div id="review-queue" class="review-list"></div>
    </section>
    <section class="decision-console">
      <div id="operator-panel" class="panel decision-status">
        <div class="section-label">当前要做</div>
        <div class="operator-title-row">
          <div id="operator-headline" class="operator-title">读取同步状态中</div>
          <div id="operator-verdict" class="operator-verdict">UNKNOWN</div>
        </div>
        <div id="operator-next" class="operator-text">等待 sidecar 返回状态。</div>
        <details class="technical-summary">
          <summary>技术摘要</summary>
          <div id="operator-brief" class="operator-brief"></div>
        </details>
      </div>
      <section id="action-guide" class="action-guide panel decision-next" hidden>
        <div class="section-label">下一步</div>
        <div class="panel-head">
          <h2 id="action-guide-title">现在怎么做</h2>
          <span id="action-guide-state" class="pill">unknown</span>
        </div>
        <div id="action-guide-summary" class="guide-summary"></div>
        <div id="action-guide-skills" class="guide-skills"></div>
        <div id="action-guide-note" class="guide-note"></div>
        <details class="guide-details">
          <summary>执行细节和本机执行器</summary>
          <div id="executor-panel" class="executor-panel" hidden>
            <div class="panel-head">
              <h2>本机执行器</h2>
              <span id="executor-pill" class="pill">checking</span>
            </div>
            <div id="executor-status" class="executor-status">正在检查 Mac 本机执行器。</div>
            <div class="executor-actions">
              <button id="executor-check" type="button" onclick="checkExecutor()">重新检查</button>
              <button id="executor-dry-run" type="button" onclick="runExecutorAction('dry_run')" disabled>一键 dry-run</button>
              <button id="executor-publish" type="button" onclick="runExecutorAction('publish')" disabled>确认发布</button>
            </div>
            <pre id="executor-output" class="executor-output mono"></pre>
          </div>
          <ol id="action-guide-steps" class="guide-steps"></ol>
        </details>
      </section>
      <details class="panel decision-boundary">
        <summary>
          <span class="boundary-title">安全边界</span>
          <span id="operator-path" class="operator-text">-</span>
        </summary>
        <div class="boundary-body">
          <div id="operator-snapshot" class="operator-text mono">-</div>
          <div class="scope-list">
            <div class="scope-line"><strong>本机</strong><span>可扫描、预检、显式推送</span></div>
            <div class="scope-line"><strong>中央</strong><span>只展示 WebDAV 共享快照</span></div>
            <div class="scope-line"><strong>设备</strong><span>只读观察各 Agent 上报状态</span></div>
          </div>
        </div>
      </details>
    </section>
    <section class="workspace-overview" aria-labelledby="workspace-overview-title">
      <div class="workspace-overview-head">
        <span class="overview-title">
          <strong id="workspace-overview-title">Skill 管理工作台</strong>
          <span id="workspace-overview-summary" class="overview-subtitle">读取中</span>
        </span>
        <span class="pill green">只操作本机</span>
      </div>
      <section class="workbench-grid">
        <div class="panel local-workspace-panel">
          <div class="workspace-eyebrow">可操作 · 只影响当前设备</div>
          <div class="workspace-title">
            <h2>本机 Skill 工作区</h2>
            <span id="local-workspace-pill" class="pill">checking</span>
          </div>
          <div id="local-workspace-summary" class="workspace-subtitle">正在读取本机工作区。</div>
          <div class="workspace-metrics">
            <div class="workspace-metric">
              <div id="local-workspace-total" class="workspace-metric-value">-</div>
              <div class="workspace-metric-label">本机 skill</div>
            </div>
            <div class="workspace-metric">
              <div id="local-workspace-blocked" class="workspace-metric-value">-</div>
              <div class="workspace-metric-label">本机待处理</div>
            </div>
            <div class="workspace-metric">
              <div id="local-workspace-source" class="workspace-metric-value">-</div>
              <div class="workspace-metric-label">数据来源</div>
            </div>
          </div>
          <div class="workspace-actions">
            <button id="local-workspace-refresh" type="button" class="primary" onclick="refreshLocalWorkspace()">扫描本机</button>
            <button id="local-workspace-dry-run" type="button" onclick="runExecutorAction('dry_run')" disabled>预检待推送</button>
            <button id="local-workspace-publish" type="button" onclick="runExecutorAction('publish')" disabled>推送到中央仓库</button>
          </div>
          <div id="local-workspace-action-note" class="local-action-note">正在检查本机执行器。</div>
          <div id="local-workspace-tools" class="workspace-tools"></div>
          <div id="local-workspace-boundary" class="boundary-note"></div>
        </div>
        <div class="panel">
          <div class="readonly-kicker">只读状态 · 不直接编辑</div>
          <div class="workspace-title">
            <h2>中央仓库状态</h2>
            <span id="central-repository-pill" class="pill">readonly</span>
          </div>
          <div id="central-repository-summary" class="workspace-subtitle"></div>
          <div id="central-repository-kv" class="kv"></div>
          <div id="central-repository-boundary" class="boundary-note"></div>
        </div>
        <div class="panel workbench-full">
          <div class="readonly-kicker">设备实测 · 只读观察</div>
          <div class="workspace-title">
            <h2>其他设备状态</h2>
            <span class="pill">read-only</span>
          </div>
          <div id="device-map-summary" class="workspace-subtitle"></div>
          <div id="device-map" class="device-map-grid"></div>
        </div>
      </section>
    </section>
    <details class="advanced-diagnostics">
      <summary>高级诊断：状态、设备、工具、队列明细</summary>
      <div class="advanced-body">
    <section class="status-band">
      <div id="health-card" class="panel health">
        <span class="dot"></span>
        <div>
          <div id="health" class="health-title">Unknown</div>
          <div id="next-action" class="health-subtitle">Waiting for status</div>
        </div>
      </div>
      <div class="metric">
        <div class="metric-label">待审批</div>
        <div id="blocked" class="metric-value">-</div>
      </div>
      <div class="metric">
        <div class="metric-label">Allowed</div>
        <div id="allowed" class="metric-value">-</div>
      </div>
      <div class="metric">
        <div class="metric-label">Remote Skills</div>
        <div id="remote-total" class="metric-value">-</div>
      </div>
      <div class="metric">
        <div class="metric-label">Cycles</div>
        <div id="cycles" class="metric-value">-</div>
      </div>
    </section>
    <div class="section-title">
      <h2>设备</h2>
      <span class="section-help">当前接入链路：Mac、OpenClaw、Gateway</span>
    </div>
    <section id="devices" class="cards"></section>
    <div id="planned-devices-title" class="section-title">
      <h2>后续接入</h2>
      <span class="section-help">本阶段不作为验收门槛</span>
    </div>
    <section id="planned-devices" class="cards"></section>
    <div class="section-title">
      <h2>工具</h2>
      <span class="section-help">WebDAV canonical snapshot 对各工具的目标覆盖，不代表某台设备已安装</span>
    </div>
    <section id="tools" class="cards"></section>
    <div class="section-title">
      <h2>设备工具实测</h2>
      <span class="section-help">由每台已接入设备 Agent 上报真实工具目录</span>
    </div>
    <section id="device-tools"></section>
    <div class="panel">
      <div class="panel-head">
        <h2>skillshub 导入诊断</h2>
        <button id="hub-import-preview-button" type="button">生成预览包</button>
      </div>
      <div id="hub-import-summary" class="kv"></div>
      <div id="hub-import-plan" class="plan-strip"></div>
      <div id="hub-import-preview-status" class="operator-text"></div>
      <div id="hub-import-preview-result" class="kv"></div>
      <table id="hub-import-apply-table" hidden>
        <thead><tr><th>Skill</th><th>Apply</th><th>原因</th></tr></thead>
        <tbody id="hub-import-apply-body"></tbody>
      </table>
      <table id="hub-import-table" hidden>
        <thead><tr><th>Skill</th><th>判断</th><th>建议</th><th>来源</th></tr></thead>
        <tbody id="hub-import-body"></tbody>
      </table>
      <div id="hub-import-empty" class="empty">No external import candidates.</div>
    </div>
    <section class="grid">
      <div class="stack">
        <div class="panel">
          <h2>待审批队列</h2>
          <div id="blocked-empty" class="empty">No pending approval items.</div>
          <table id="blocked-table" hidden>
            <thead><tr><th>Skill</th><th>Status</th><th>Category</th><th>Hashes</th><th>Recommendation / Next step</th></tr></thead>
            <tbody id="blocked-body"></tbody>
          </table>
        </div>
        <div class="panel">
          <h2>Sync Summary</h2>
          <div id="summary" class="kv"></div>
        </div>
      </div>
      <div class="stack">
        <div class="panel">
          <h2>Daemon</h2>
          <div id="daemon" class="kv"></div>
        </div>
        <div class="panel">
          <h2>Peer Local Policy</h2>
        <div id="overrides" class="kv"></div>
      </div>
        <div class="panel">
          <h2>设备摘要</h2>
          <div id="operator-devices" class="device-lines"></div>
        </div>
        <div class="panel">
          <h2>Artifacts</h2>
          <div id="artifacts" class="kv"></div>
        </div>
      </div>
    </section>
      </div>
    </details>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const EXECUTOR_URL = "http://127.0.0.1:18765";
    let currentGuideSkills = [];
    let executorAvailable = false;
    let executorAllowPublish = false;
    let lastDryRunSafe = false;
    let localWorkspaceFromExecutor = null;
    const text = (value) => value === undefined || value === null || value === "" ? "-" : String(value);
    const pretty = (value) => {
      if (value === undefined || value === null) return "-";
      if (typeof value === "object") return JSON.stringify(value);
      return String(value);
    };
    const row = (key, value) => `<div class="key">${key}</div><div class="value mono">${escapeHtml(pretty(value))}</div>`;
    const pill = (label, kind) => `<span class="pill ${kind || ""}">${escapeHtml(text(label))}</span>`;
    const escapeHtml = (value) => String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");

    function nextAction(status) {
      if (status.health === "green") return "当前没有需要审核的同步项。";
      if (status.health === "yellow") return "先处理待审批队列，再决定是否发布到中央仓库。";
      if (status.health === "red") return "先修复状态文件、WebDAV 或 sidecar 进程异常。";
      return "状态暂不可读。";
    }

    function render(status) {
      $("error").style.display = "none";
      const dashboard = status.dashboard || {};
      window.lastDashboard = dashboard;
      const operator = dashboard.operator || {};
      const health = dashboard.health || status.health || "unknown";
      const plan = status.sync_plan || {};
      const snapshot = status.remote_snapshot || {};
      const daemon = status.daemon_state || {};
      const blockedReport = status.blocked_report || {};
      $("health-card").className = `panel health ${health}`;
      $("health").textContent = health;
      $("next-action").textContent = operator.next_action || nextAction({ ...status, health });
      $("operator-headline").textContent = operator.headline || "同步状态未知";
      $("operator-panel").className = `panel decision-status ${deviceKind(health)}`;
      $("operator-verdict").textContent = operatorVerdict(health);
      $("operator-verdict").className = `operator-verdict ${deviceKind(health)}`;
      renderOperatorBrief(dashboard, snapshot);
      renderActionGuide(operator.action_guide || {});
      renderStatusStrip(dashboard, health);
      renderScopeSwitchboard(dashboard);
      renderWorkbench(dashboard);
      $("operator-next").textContent = conciseOperatorNext(dashboard, operator, { ...status, health });
      $("operator-path").textContent = "本机可操作；中央只接收显式推送；其他设备只读。";
      $("operator-snapshot").textContent = `当前中央版本：${text(operator.snapshot_id)}`;
      $("blocked").textContent = text(dashboard.blocked ?? plan.blocked ?? blockedReport.total);
      $("allowed").textContent = text(plan.allowed);
      $("remote-total").textContent = text(snapshot.total);
      $("cycles").textContent = text(daemon.cycles_run);
      $("updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
      renderDevices(Array.isArray(dashboard.devices) ? dashboard.devices : []);
      renderPlannedDevices(Array.isArray(dashboard.planned_devices) ? dashboard.planned_devices : []);
      renderTools(Array.isArray(dashboard.tools) ? dashboard.tools : []);
      renderDeviceTools(Array.isArray(dashboard.device_tools) ? dashboard.device_tools : []);
      renderHubImport(dashboard.hub_import || {});

      const blockedItems = Array.isArray(dashboard.blocked_items) ? dashboard.blocked_items : (Array.isArray(blockedReport.items) ? blockedReport.items : []);
      renderReviewQueue(blockedItems);
      $("blocked-empty").hidden = blockedItems.length > 0;
      $("blocked-table").hidden = blockedItems.length === 0;
      $("blocked-body").innerHTML = blockedItems.map((item) => `
        <tr>
          <td class="mono">${escapeHtml(text(item.peer_name || item.peer_id))} / ${escapeHtml(text(item.skill_id))}</td>
          <td>${escapeHtml(text(item.status_action))}<div class="mini-label">${escapeHtml(text(item.plan_action))}</div></td>
          <td>${escapeHtml(text(item.category))}<div class="mini-label">${escapeHtml(text(item.source))}</div></td>
          <td class="mono">
            <div>base ${shortHash(item.base_hash)}</div>
            <div>local ${shortHash(item.local_hash)}</div>
            <div>remote ${shortHash(item.remote_hash)}</div>
          </td>
          <td>${blockedItemAction(item)}</td>
        </tr>
      `).join("");

      $("summary").innerHTML = [
        row("writer_policy", plan.writer_policy || status.writer_policy),
        row("safe_to_apply", plan.safe_to_apply),
        row("sync_summary", plan.summary),
        row("status_summary", plan.status_summary),
      ].join("");
      $("daemon").innerHTML = [
        row("status", daemon.daemon_status || daemon.status),
        row("target", daemon.target),
        row("daemon_writer_policy", daemon.writer_policy),
        row("stop_on_blocked", daemon.stop_on_blocked),
        row("interval_seconds", daemon.interval_seconds),
        row("updated_at", daemon.updated_at),
        row("last_cycle", daemon.last_cycle),
        row("state_file", daemon.path),
      ].join("");
      const localOverrides = plan.local_overrides || {};
      $("overrides").innerHTML = [
        row("total", localOverrides.total),
        row("skills", localOverrides.skills),
      ].join("");
      renderOperatorDevices(operator.devices || {});
      $("artifacts").innerHTML = [
        row("local_root", status.local_root),
        row("snapshot", snapshot.snapshot_id),
        row("snapshot_path", snapshot.path),
        row("base_record", (status.base_record || {}).path),
        row("blocked_report", blockedReport.path),
      ].join("");
    }

    function deviceKind(health) {
      if (health === "green") return "green";
      if (health === "yellow") return "yellow";
      if (health === "red") return "red";
      return "";
    }

    function operatorVerdict(health) {
      if (health === "green") return "正常";
      if (health === "yellow") return "需要审核";
      if (health === "red") return "需要处理";
      return "未知";
    }

    function renderStatusStrip(dashboard, health) {
      const local = dashboard.local_workspace || {};
      const central = dashboard.central_repository || {};
      const map = dashboard.device_map || {};
      const deviceCount = Array.isArray(map.items) ? map.items.length : 0;
      $("strip-health").textContent = operatorVerdict(health);
      $("strip-blocked").textContent = text(dashboard.blocked);
      $("strip-local").textContent = text(local.total_skills);
      $("strip-central").textContent = text(central.total_skills);
      $("strip-devices").textContent = text(deviceCount);
    }

    function conciseOperatorNext(dashboard, operator, status) {
      const blocked = Number(dashboard.blocked || 0);
      if ((dashboard.health || status.health) === "yellow" && blocked > 0) {
        return `先审 ${blocked} 个待审批项；dry-run 只预览，确认后再发布到中央仓库。`;
      }
      return operator.next_action || nextAction(status);
    }

    function conciseGuideSummary(guide) {
      const skills = Array.isArray(guide.skills) ? guide.skills : [];
      if ((guide.state || "") === "yellow" && skills.length > 0) {
        return `重点是 ${skills.length} 个待审批 skill。先看上方清单，再按执行细节 dry-run。`;
      }
      return guide.summary || "";
    }

    function renderScopeSwitchboard(dashboard) {
      const local = dashboard.local_workspace || {};
      const central = dashboard.central_repository || {};
      const map = dashboard.device_map || {};
      const items = Array.isArray(map.items) ? map.items : [];
      $("scope-local-count").textContent = `${text(local.total_skills)} 个本机 skill`;
      $("scope-central-count").textContent = `${text(central.total_skills)} 个中央 skill`;
      $("scope-device-count").textContent = `${text(items.length)} 台设备`;
      $("scope-local-note").textContent = "授权扫描本机目录；操作只影响当前设备。";
      $("scope-central-note").textContent = `中央仓库是 WebDAV 共享事实源；当前 ${text(central.blocked)} 个变更需要显式审批。`;
      $("scope-device-note").textContent = "其他设备只展示各自 Agent 上报的实测状态，Gateway 不远程改设备。";
    }

    function statusLabel(value) {
      if (value === "green") return "正常";
      if (value === "yellow") return "需要审核";
      if (value === "red") return "需要处理";
      if (value === "local") return "本机可操作";
      if (value === "read_only") return "只读";
      if (value === "remote_read_only") return "远端只读";
      if (value === "planned") return "待接入";
      return text(value || "未知");
    }

    function scopeLabel(value) {
      if (value === "local") return "本机可操作";
      if (value === "read_only") return "只读聚合";
      if (value === "remote_read_only") return "远端只读";
      if (value === "planned") return "待接入";
      return text(value || "未知");
    }

    function renderOperatorBrief(dashboard, snapshot) {
      const operator = dashboard.operator || {};
      const devices = Array.isArray(dashboard.devices) ? dashboard.devices : [];
      const planned = Array.isArray(dashboard.planned_devices) ? dashboard.planned_devices : [];
      const active = devices
        .filter((device) => ["gateway", "mac", "oc-vps", "openclaw"].includes(device.id))
        .map((device) => `${text(device.id)}=${text(device.health)}/${text(device.skills)}/${text((device.freshness || {}).label)}`);
      const deferred = planned
        .map((device) => `${text(device.id)}=${text(device.policy || device.health)}`);
      const issue = operator.top_issue || (Array.isArray(dashboard.blocked_items) ? dashboard.blocked_items[0] : null);
      const lines = [
        briefLine("snapshot", `${text(snapshot.snapshot_id)} total=${text(snapshot.total)} blocked=${text(dashboard.blocked)}`),
        briefLine("devices", active.length ? active.join("; ") : "-"),
        briefLine("deferred", deferred.length ? deferred.join("; ") : "-"),
      ];
      if (issue) {
        lines.push(briefLine("issue", topIssueText(issue)));
      }
      $("operator-brief").innerHTML = lines.join("");
    }

    function topIssueText(issue) {
      const peer = issue.peer_name || issue.peer_id || "unknown-peer";
      const skill = issue.skill_id || "unknown-skill";
      const action = issue.status_action || issue.plan_action || "-";
      const category = issue.category || "-";
      return `${peer} / ${skill} ${action} ${category}`;
    }

    function blockedItemAction(item) {
      const action = item.operator_action || item.recommendation || item.reason || "-";
      const command = item.operator_command || "";
      const reason = item.reason || item.recommendation || "";
      return `
        <div class="action-cell">
          <div class="action-primary">${escapeHtml(text(action))}</div>
          ${command ? `<div class="action-command mono">${escapeHtml(command)}</div>` : ""}
          ${reason ? `<div class="mini-label">${escapeHtml(reason)}</div>` : ""}
        </div>
      `;
    }

    function renderReviewQueue(items) {
      const panel = $("review-queue-panel");
      if (!Array.isArray(items) || items.length === 0) {
        panel.hidden = true;
        return;
      }
      panel.hidden = false;
      $("review-queue-count").outerHTML = pill(`${items.length} 项`, "yellow").replace("<span", "<span id=\"review-queue-count\"");
      const peers = [...new Set(items.map((item) => text(item.peer_name || item.peer_id)).filter(Boolean))];
      $("review-queue-summary").textContent = `${peers.join("、") || "其他设备"} 有 ${items.length} 个变更等待确认。先 dry-run，看清楚再决定是否推送到中央仓库。`;
      const visibleItems = items.slice(0, 4);
      const hiddenCount = items.length - visibleItems.length;
      const rows = visibleItems.map((item) => {
        const command = item.operator_command || "";
        return `
          <div class="review-item">
            <div>
              <div class="review-skill">${escapeHtml(text(item.skill_id))}</div>
              <div class="review-source">${escapeHtml(text(item.peer_name || item.peer_id))}</div>
              <div class="review-meta">
                <span class="review-meta-item">${escapeHtml(reviewSourceText(item))}</span>
                <span class="review-meta-item">${escapeHtml(reviewCategoryText(item))}</span>
                <span class="review-meta-item">${escapeHtml(reviewRiskText(item))}</span>
              </div>
            </div>
            <div>
              <div class="review-action">${escapeHtml(reviewActionText(item))}</div>
              <div class="review-next-step">${escapeHtml(reviewNextStepText(item))}</div>
              ${command ? `
                <details class="review-command">
                  <summary>查看 dry-run 命令</summary>
                  <div class="command-row">
                    <pre class="guide-command mono"><code>${escapeHtml(command)}</code></pre>
                    <button type="button" class="copy-button" data-command="${escapeHtml(command)}" onclick="copyCommand(this)">复制</button>
                  </div>
                </details>
              ` : ""}
            </div>
            <div class="review-controls">
              ${pill(reviewStatusText(item), "yellow")}
              <button type="button" class="review-dry-run-button" data-skill-id="${escapeHtml(text(item.skill_id))}" onclick="runExecutorActionForSkill(this.dataset.skillId)" disabled>预检此项</button>
            </div>
          </div>
        `;
      }).join("");
      const more = hiddenCount > 0
        ? `<div class="review-more">还有 ${hiddenCount} 项，完整明细见下方高级诊断。</div>`
        : "";
      $("review-queue").innerHTML = `${rows}${more}`;
      setExecutorButtons(executorAvailable);
    }

    function reviewActionText(item) {
      if (item.category === "conflict") return "存在冲突，先人工合并，不要直接发布。";
      if (item.status_action === "local_new") return "远端新增 skill，先预检内容和目标工具，再决定是否发布。";
      if (item.status_action === "push_new") return "新 skill 等待发布，先 dry-run 审核。";
      if (item.status_action === "push") return "已有 skill 有更新，先 dry-run 审核差异。";
      return item.operator_action || item.recommendation || item.reason || "查看高级诊断里的建议动作。";
    }

    function reviewSourceText(item) {
      const peer = item.peer_name || item.peer_id || "未知设备";
      return `来源 ${text(peer)}`;
    }

    function reviewCategoryText(item) {
      if (item.category === "writer_policy") return "需要显式发布";
      if (item.category === "conflict") return "冲突";
      if (item.category === "delete") return "删除确认";
      return text(item.category || item.status_action || "待审批");
    }

    function reviewRiskText(item) {
      if (item.category === "conflict") return "高风险";
      if (item.category === "delete") return "高风险";
      if (item.status_action === "push_new" || item.status_action === "local_new") return "中风险";
      if (item.status_action === "push") return "低风险";
      return "需确认";
    }

    function reviewNextStepText(item) {
      if (item.category === "conflict") return "下一步：先生成冲突包并人工合并。";
      if (item.category === "delete") return "下一步：确认删除意图，未确认前不要 apply。";
      if (item.status_action === "push_new" || item.status_action === "local_new") return "下一步：预检内容和目标工具，确认后再发布。";
      if (item.status_action === "push") return "下一步：预检差异，通过后再发布到中央仓库。";
      return "下一步：查看 dry-run 输出和高级诊断。";
    }

    function reviewStatusText(item) {
      if (item.status_action === "local_new") return "新增";
      if (item.status_action === "push_new") return "新发布";
      if (item.status_action === "push") return "更新";
      if (item.category === "conflict") return "冲突";
      return statusLabel(item.status_action || item.category || "待处理");
    }

    function renderActionGuide(guide) {
      const panel = $("action-guide");
      if (!guide || !guide.title) {
        panel.hidden = true;
        return;
      }
      panel.hidden = false;
      const state = guide.state || "unknown";
      panel.className = `action-guide panel decision-next ${deviceKind(state)}`;
      $("action-guide-title").textContent = guide.title || "现在怎么做";
      $("action-guide-state").outerHTML = pill(statusLabel(state), deviceKind(state)).replace("<span", "<span id=\"action-guide-state\"");
      $("action-guide-summary").textContent = conciseGuideSummary(guide);
      const skills = Array.isArray(guide.skills) ? guide.skills : [];
      currentGuideSkills = skills;
      lastDryRunSafe = false;
      $("action-guide-skills").innerHTML = renderSkillChips(skills);
      const steps = Array.isArray(guide.steps) ? guide.steps : [];
      $("action-guide-steps").innerHTML = steps.map((step, index) => {
        const command = step.command || "";
        return `
          <li class="guide-step">
            <div class="step-index">${index + 1}</div>
            <div>
              <div class="step-title">${escapeHtml(text(step.title))}</div>
              <div class="step-detail">${escapeHtml(text(step.detail))}</div>
              ${command ? `
                <details class="command-detail">
                  <summary>查看命令</summary>
                  <div class="command-row">
                    <pre class="guide-command mono"><code>${escapeHtml(command)}</code></pre>
                    <button type="button" class="copy-button" data-command="${escapeHtml(command)}" onclick="copyCommand(this)">复制</button>
                  </div>
                </details>
              ` : ""}
            </div>
          </li>
        `;
      }).join("");
      $("action-guide-note").textContent = guide.note || "";
      renderExecutorPanel(guide);
    }

    function renderSkillChips(skills) {
      if (!Array.isArray(skills) || skills.length === 0) return "";
      const visible = skills.slice(0, 3);
      const hidden = skills.length - visible.length;
      const chips = visible
        .map((skill) => `<span class="skill-chip">${escapeHtml(text(skill))}</span>`)
        .join("");
      const more = hidden > 0 ? `<span class="skill-more">另 ${hidden} 个，见待审批清单</span>` : "";
      return `<div class="skill-chip-row" aria-label="涉及 skill">${chips}${more}</div>`;
    }

    async function copyCommand(button) {
      const command = button.dataset.command || "";
      if (!command) return;
      try {
        await navigator.clipboard.writeText(command);
        button.textContent = "已复制";
        setTimeout(() => { button.textContent = "复制"; }, 1200);
      } catch (err) {
        button.textContent = "手动复制";
        setTimeout(() => { button.textContent = "复制"; }, 1600);
      }
    }

    function renderExecutorPanel(guide) {
      const panel = $("executor-panel");
      const skills = Array.isArray(guide.skills) ? guide.skills : [];
      if (!skills.length || guide.state !== "yellow") {
        panel.hidden = true;
        return;
      }
      panel.hidden = false;
      $("executor-output").style.display = "none";
      setExecutorStatus("checking", "正在检查 Mac 本机执行器。", "yellow");
      checkExecutor();
    }

    async function checkExecutor() {
      setExecutorButtons(false);
      try {
        const response = await fetch(`${EXECUTOR_URL}/healthz`, { method: "GET", cache: "no-store" });
        const payload = await response.json();
        executorAvailable = response.ok && payload.ok;
        executorAllowPublish = Boolean(payload.allow_publish);
        if (executorAvailable) {
          setExecutorStatus(
            "online",
            executorAllowPublish
              ? "Mac 本机执行器在线：可以在面板内 dry-run；dry-run 安全后可确认发布。"
              : "Mac 本机执行器在线：可以在面板内 dry-run；发布端点未开启，避免误写 WebDAV。",
            "green",
          );
          setExecutorButtons(true);
          refreshLocalWorkspace();
        } else {
          setExecutorOffline();
        }
      } catch (err) {
        setExecutorOffline();
      }
    }

    function setExecutorOffline() {
      executorAvailable = false;
      executorAllowPublish = false;
      setExecutorStatus(
        "offline",
        "本机执行器未启动。请复制上面的命令执行，或在 Mac 上启动：skill-sync operator-executor --repo-root /Users/mac/workspace_codex/skill-sync-sidecar --allow-publish",
        "yellow",
      );
      setExecutorButtons(false);
    }

    function setExecutorStatus(label, detail, kind) {
      $("executor-pill").outerHTML = pill(label, kind).replace("<span", "<span id=\"executor-pill\"");
      $("executor-status").textContent = detail;
      const localNote = $("local-workspace-action-note");
      if (localNote) localNote.textContent = detail;
    }

    function setExecutorButtons(available) {
      $("executor-dry-run").disabled = !available || currentGuideSkills.length === 0;
      $("executor-publish").disabled = !available || !executorAllowPublish || !lastDryRunSafe;
      $("scope-dry-run").disabled = !available || currentGuideSkills.length === 0;
      $("scope-publish").disabled = !available || !executorAllowPublish || !lastDryRunSafe;
      $("local-workspace-dry-run").disabled = !available || currentGuideSkills.length === 0;
      $("local-workspace-publish").disabled = !available || !executorAllowPublish || !lastDryRunSafe;
      document.querySelectorAll(".review-dry-run-button").forEach((button) => {
        button.disabled = !available || !button.dataset.skillId;
      });
    }

    async function runExecutorAction(mode) {
      if (!executorAvailable || currentGuideSkills.length === 0) return;
      const isPublish = mode === "publish";
      if (isPublish) {
        if (!lastDryRunSafe) {
          showExecutorOutput("请先运行 dry-run，并确认 safe_to_push=true。");
          return;
        }
        const typed = window.prompt("发布会写入 WebDAV。请输入 PUBLISH 确认：");
        if (typed !== "PUBLISH") {
          showExecutorOutput("已取消发布。");
          return;
        }
      }
      setExecutorButtons(false);
      setExecutorStatus(isPublish ? "publishing" : "dry-run", isPublish ? "正在发布，请不要关闭页面。" : "正在运行 dry-run，请稍等。", "yellow");
      try {
        const endpoint = isPublish ? "/api/openclaw-approved-push-publish" : "/api/openclaw-approved-push-dry-run";
        const response = await fetch(`${EXECUTOR_URL}${endpoint}`, {
          method: "POST",
          cache: "no-store",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            skill_ids: currentGuideSkills,
            confirm: isPublish ? "PUBLISH" : undefined,
          }),
        });
        const payload = await response.json();
        lastDryRunSafe = !isPublish && Boolean(payload.ok && payload.safe_to_push);
        showExecutorOutput(formatExecutorResult(payload));
        if (payload.ok) {
          setExecutorStatus(isPublish ? "published" : "dry-run ok", isPublish ? "发布完成。请等待 1-2 分钟后刷新状态。" : "dry-run 通过：safe_to_push=true，可以继续确认发布。", "green");
        } else {
          setExecutorStatus("failed", payload.error || "执行失败，请查看输出。", "red");
        }
      } catch (err) {
        showExecutorOutput(String(err));
        setExecutorStatus("failed", "执行器调用失败，请确认本机服务仍在线。", "red");
      } finally {
        setExecutorButtons(executorAvailable);
      }
    }

    async function runExecutorActionForSkill(skillId) {
      if (!executorAvailable || !skillId) return;
      setExecutorButtons(false);
      setExecutorStatus("dry-run", `正在预检 ${skillId}，请稍等。`, "yellow");
      try {
        const response = await fetch(`${EXECUTOR_URL}/api/openclaw-approved-push-dry-run`, {
          method: "POST",
          cache: "no-store",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ skill_ids: [skillId] }),
        });
        const payload = await response.json();
        showExecutorOutput(formatExecutorResult(payload));
        if (payload.ok && payload.safe_to_push) {
          setExecutorStatus("dry-run ok", `${skillId} 预检通过：safe_to_push=true。`, "green");
        } else if (payload.ok) {
          setExecutorStatus("needs review", `${skillId} 预检完成，但还不能发布，请看输出。`, "yellow");
        } else {
          setExecutorStatus("failed", payload.error || `${skillId} 预检失败，请查看输出。`, "red");
        }
      } catch (err) {
        showExecutorOutput(String(err));
        setExecutorStatus("failed", "执行器调用失败，请确认本机服务仍在线。", "red");
      } finally {
        setExecutorButtons(executorAvailable);
      }
    }

    function formatExecutorResult(payload) {
      const lines = [
        `ok=${text(payload.ok)}`,
        `mode=${text(payload.mode)}`,
        `exit_code=${text(payload.exit_code)}`,
        `safe_to_push=${text(payload.safe_to_push)}`,
        `approved=${text(payload.approved)}`,
        `skills=${Array.isArray(payload.approved_skill_ids) ? payload.approved_skill_ids.join(", ") : text(payload.approved_skill_ids)}`,
        `command=${text(payload.command)}`,
      ];
      if (payload.error) lines.push(`error=${payload.error}`);
      if (payload.stderr_tail) lines.push(`\nstderr:\n${payload.stderr_tail}`);
      if (payload.stdout_tail) lines.push(`\nstdout:\n${payload.stdout_tail}`);
      return lines.join("\n");
    }

    function showExecutorOutput(value) {
      $("executor-output").style.display = "block";
      $("executor-output").textContent = value;
    }

    function renderWorkbench(dashboard) {
      renderLocalWorkspace(dashboard.local_workspace || {});
      renderCentralRepository(dashboard.central_repository || {});
      renderDeviceMap(dashboard.device_map || {});
      renderWorkspaceOverviewSummary(dashboard);
      if (!executorAvailable) {
        checkExecutor();
      }
    }

    function renderWorkspaceOverviewSummary(dashboard) {
      const local = dashboard.local_workspace || {};
      const central = dashboard.central_repository || {};
      const map = dashboard.device_map || {};
      const deviceCount = Array.isArray(map.items) ? map.items.length : 0;
      $("workspace-overview-summary").textContent = `左侧管理本机 skill；中央 ${text(central.total_skills)} 个 skill 只读展示；${text(deviceCount)} 台设备只读上报`;
    }

    function renderLocalWorkspace(workspace) {
      const live = localWorkspaceFromExecutor || {};
      const tools = Array.isArray(live.tools) ? live.tools : (Array.isArray(workspace.tools) ? workspace.tools : []);
      const total = live.total_skills ?? workspace.total_skills;
      const blocked = live.blocked ?? workspace.blocked;
      const source = localWorkspaceFromExecutor ? "本机实时扫描" : (workspace.reported ? "最近一次 Mac 上报" : "等待本机授权");
      $("local-workspace-pill").outerHTML = pill(source, localWorkspaceFromExecutor ? "green" : deviceKind(workspace.health)).replace("<span", "<span id=\"local-workspace-pill\"");
      $("local-workspace-summary").textContent = `这里是唯一可直接操作的区域。扫描、预检、发布都只针对 ${text(workspace.device_name || live.device_name || "Mac 本机")}；其他设备不会被远程改动。`;
      $("local-workspace-total").textContent = text(total);
      $("local-workspace-blocked").textContent = text(blocked);
      $("local-workspace-source").textContent = localWorkspaceFromExecutor ? "实时" : (workspace.reported ? "上报" : "未授权");
      $("local-workspace-tools").innerHTML = tools.map((tool) => `
        <div class="workspace-tool">
          <div class="workspace-tool-row">
            <div class="workspace-tool-name">${escapeHtml(text(tool.name))}</div>
            <div class="workspace-tool-count">${escapeHtml(text(tool.skills))}</div>
            ${toolStatePill(tool)}
          </div>
        </div>
      `).join("");
      $("local-workspace-boundary").textContent = workspace.boundary || "本地工作区只操作浏览器所在设备。";
    }

    function renderCentralRepository(repo) {
      $("central-repository-pill").outerHTML = pill("WebDAV 快照", "green").replace("<span", "<span id=\"central-repository-pill\"");
      $("central-repository-summary").textContent = `共享事实源收录 ${text(repo.total_skills)} 个 skill；当前 ${text(repo.blocked)} 个变更需要显式审批。`;
      $("central-repository-kv").innerHTML = [
        row("中央版本", repo.snapshot_id),
        row("更新时间", repo.created_at),
        row("协议版本", repo.protocol_version),
        row("目标覆盖", repo.targeted_projection_total),
      ].join("");
      $("central-repository-boundary").textContent = repo.boundary || "中央仓库只接受显式 approved push。";
    }

    function renderDeviceMap(map) {
      const items = Array.isArray(map.items) ? map.items : [];
      $("device-map-summary").textContent = map.boundary || "设备地图默认只读。";
      $("device-map").innerHTML = items.map((device) => `
        <div class="device-map-item">
          <div class="card-head">
            <div>
              <div class="card-name">${escapeHtml(text(device.name))}</div>
              <div class="card-kind">${escapeHtml(text(device.capability))}</div>
            </div>
            ${pill(statusLabel(device.health || device.operation_scope), deviceKind(device.health))}
          </div>
          <div class="device-map-meta">
            <div>技能 ${escapeHtml(text(device.skills))} · 待处理 ${escapeHtml(text(device.blocked))}</div>
            <div>权限 ${escapeHtml(scopeLabel(device.operation_scope))} · ${freshnessPill(device.freshness)}</div>
          </div>
        </div>
      `).join("");
    }

    async function refreshLocalWorkspace() {
      try {
        const response = await fetch(`${EXECUTOR_URL}/api/local-workspace`, { method: "GET", cache: "no-store" });
        const payload = await response.json();
        if (response.ok && payload.ok) {
          localWorkspaceFromExecutor = payload;
          renderLocalWorkspace(window.lastDashboard ? window.lastDashboard.local_workspace || {} : {});
          setExecutorStatus("online", payload.allow_publish ? "Mac 本机执行器在线：本机扫描可用，发布端点已开启。" : "Mac 本机执行器在线：本机扫描和 dry-run 可用，发布端点未开启。", "green");
          executorAvailable = true;
          executorAllowPublish = Boolean(payload.allow_publish);
          setExecutorButtons(true);
        } else {
          throw new Error(payload.error || "local workspace scan failed");
        }
      } catch (err) {
        localWorkspaceFromExecutor = null;
        setExecutorOffline();
      }
    }

    function briefLine(label, value) {
      return `
        <div class="brief-line">
          <div class="brief-label">${escapeHtml(label)}</div>
          <div class="brief-value mono">${escapeHtml(value)}</div>
        </div>
      `;
    }

    function renderDevices(devices) {
      $("devices").innerHTML = devices.map((device) => `
        <article class="device-card">
          <div class="card-head">
            <div>
              <div class="card-name">${escapeHtml(device.name)}</div>
              <div class="card-kind">${escapeHtml(device.kind)}</div>
            </div>
            ${pill(device.health, deviceKind(device.health))}
          </div>
          <div class="card-note">${escapeHtml(device.note)}</div>
          <div class="card-stats">
            <div class="mini-stat"><div class="mini-label">技能数</div><div class="mini-value">${escapeHtml(text(device.skills))}</div></div>
            <div class="mini-stat"><div class="mini-label">待处理</div><div class="mini-value">${escapeHtml(text(device.blocked))}</div></div>
            <div class="mini-stat"><div class="mini-label">策略</div><div class="mini-value">${escapeHtml(text(device.policy))}</div></div>
            <div class="mini-stat"><div class="mini-label">本机例外</div><div class="mini-value">${escapeHtml(pretty(device.local_policy || []))}</div></div>
            <div class="mini-stat"><div class="mini-label">更新于</div><div class="mini-value subtle">${escapeHtml(formatDateTime(device.last_seen_at))}</div></div>
            <div class="mini-stat"><div class="mini-label">新鲜度</div><div class="mini-value">${freshnessPill(device.freshness)}</div></div>
          </div>
        </article>
      `).join("");
    }

    function renderPlannedDevices(devices) {
      $("planned-devices-title").hidden = devices.length === 0;
      $("planned-devices").hidden = devices.length === 0;
      $("planned-devices").innerHTML = devices.map((device) => `
        <article class="device-card planned-card">
          <div class="card-head">
            <div>
              <div class="card-name">${escapeHtml(device.name)}</div>
              <div class="card-kind">${escapeHtml(device.kind)}</div>
            </div>
            ${pill(device.policy || device.health, "")}
          </div>
          <div class="card-note">${escapeHtml(device.note)}</div>
        </article>
      `).join("");
    }

    function freshnessPill(freshness) {
      const state = freshness && freshness.state ? freshness.state : "unknown";
      const label = freshness && freshness.label ? freshness.label : "未知";
      return `<span class="freshness ${freshnessClass(state)}">${escapeHtml(label)}</span>`;
    }

    function freshnessClass(state) {
      if (state === "fresh" || state === "aging" || state === "stale") return state;
      return "";
    }

    function formatDateTime(value) {
      if (!value) return "-";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return text(value);
      return date.toLocaleString();
    }

    function renderTools(tools) {
      $("tools").innerHTML = tools.map((tool) => `
        <article class="tool-card">
          <div class="card-head">
            <div>
              <div class="card-name">${escapeHtml(tool.name)}</div>
              <div class="card-kind">${escapeHtml(tool.role)}</div>
            </div>
            ${toolStatePill(tool)}
          </div>
          <div class="card-note mono">${escapeHtml(tool.path)}</div>
          <div class="card-stats">
            <div class="mini-stat"><div class="mini-label">技能数</div><div class="mini-value">${escapeHtml(text(tool.skills))}</div></div>
            <div class="mini-stat"><div class="mini-label">风险</div><div class="mini-value">${escapeHtml(pretty(tool.risk))}</div></div>
            <div class="mini-stat"><div class="mini-label">目标数</div><div class="mini-value">${escapeHtml(text((tool.projection || {}).canonical_targeted))}</div></div>
            <div class="mini-stat"><div class="mini-label">缺失/漂移</div><div class="mini-value">${escapeHtml(projectionGap(tool.projection))}</div></div>
          </div>
        </article>
      `).join("");
    }

    function renderDeviceTools(groups) {
      $("device-tools").innerHTML = groups.map((group) => `
        <article class="device-tool-group">
          <div class="device-tool-head">
            <div>
              <div class="card-name">${escapeHtml(text(group.device_name))}</div>
              <div class="card-kind">${escapeHtml(text(group.note))}</div>
            </div>
            <div>${freshnessPill(group.freshness)}</div>
          </div>
          <div class="device-tool-grid">
            ${(Array.isArray(group.tools) ? group.tools : []).map((tool) => `
              <article class="tool-card">
                <div class="card-head">
                  <div>
                    <div class="card-name">${escapeHtml(text(tool.name))}</div>
                    <div class="card-kind">${escapeHtml(text(tool.role))}</div>
                  </div>
                  ${toolStatePill(tool)}
                </div>
                <div class="card-note mono">${escapeHtml(text(tool.path || (Array.isArray(tool.roots) ? tool.roots.join(", ") : "")))}</div>
                <div class="card-stats">
                  <div class="mini-stat"><div class="mini-label">技能数</div><div class="mini-value">${escapeHtml(text(tool.skills))}</div></div>
                  <div class="mini-stat"><div class="mini-label">风险</div><div class="mini-value">${escapeHtml(pretty(tool.risk))}</div></div>
                  <div class="mini-stat"><div class="mini-label">实测时间</div><div class="mini-value subtle">${escapeHtml(formatDateTime(tool.measured_at))}</div></div>
                  <div class="mini-stat"><div class="mini-label">说明</div><div class="mini-value subtle">${escapeHtml(text(tool.note))}</div></div>
                </div>
              </article>
            `).join("")}
          </div>
        </article>
      `).join("");
    }

    function toolStatePill(tool) {
      if (tool.state === "observer") return pill("已上报", "green");
      if (tool.state === "error") return pill("异常", "red");
      if (tool.state === "detected" || tool.installed === true) return pill("已发现", "green");
      if (tool.state === "unsupported") return pill("暂不支持", "");
      if (tool.installed === false) return pill("未发现", "");
      return pill(text(tool.state || "unknown"), "");
    }

    function renderHubImport(hubImport) {
      const summary = hubImport.summary || {};
      const actionPlan = hubImport.action_plan || {};
      const actionSummary = actionPlan.summary || {};
      $("hub-import-summary").innerHTML = [
        row("Hub 已有", hubImport.hub_total),
        row("外部候选", hubImport.source_total),
        row("可导入", summary.importable || 0),
        row("可更新", summary.update_available || 0),
        row("无需导入", summary.already_in_hub || 0),
      ].join("");
      $("hub-import-plan").innerHTML = [
        planCell("模式", actionPlan.mode || "dry_run"),
        planCell("预演导入", actionSummary.preview_import || 0),
        planCell("更新审查", actionSummary.review_update || 0),
        planCell("选择来源", actionSummary.review_duplicate_import || 0),
        planCell("跳过", actionSummary.skip_existing || 0),
      ].join("");
      const items = hubImportPreviewItems(Array.isArray(hubImport.items) ? hubImport.items : []);
      $("hub-import-empty").hidden = items.length > 0;
      $("hub-import-table").hidden = items.length === 0;
      $("hub-import-body").innerHTML = items.map((item) => `
        <tr>
          <td class="mono">${escapeHtml(text(item.skill_id))}</td>
          <td>${pill(item.status_label || item.status, hubStatusKind(item.status))}<div class="mini-label">${escapeHtml(text(item.reason_label))}</div></td>
          <td>${escapeHtml(text(item.operator_action))}<div class="mini-label">${escapeHtml(text(item.status_description))}</div></td>
          <td class="mono">${escapeHtml(text(item.source))}<div class="mini-label">${escapeHtml(duplicateSourceText(item))}</div></td>
        </tr>
      `).join("");
    }

    async function generateHubImportPreview() {
      const button = $("hub-import-preview-button");
      button.disabled = true;
      $("hub-import-preview-status").textContent = "生成预览包中...";
      $("hub-import-preview-result").innerHTML = "";
      $("hub-import-apply-table").hidden = true;
      $("hub-import-apply-body").innerHTML = "";
      try {
        const response = await fetch("/api/hub-import-preview", { method: "POST", cache: "no-store" });
        const payload = await response.json();
        if (!response.ok || !payload.ok) throw new Error(payload.error || `HTTP ${response.status}`);
        renderHubImportPreview(payload);
      } catch (error) {
        $("hub-import-preview-status").textContent = `生成失败：${error.message}`;
      } finally {
        button.disabled = false;
      }
    }

    function renderHubImportPreview(payload) {
      const preview = payload.preview || {};
      const applyPlan = payload.apply_plan || {};
      $("hub-import-preview-status").textContent = "预览包已生成，当前只展示 apply dry-run，不执行写入。";
      $("hub-import-preview-result").innerHTML = [
        row("preview_json", preview.preview_json),
        row("preview_md", preview.preview_md),
        row("dry_run_allowed", applyPlan.allowed),
        row("dry_run_blocked", applyPlan.blocked),
        row("dry_run_total", applyPlan.total),
      ].join("");
      const items = Array.isArray(applyPlan.items) ? applyPlan.items.slice(0, 12) : [];
      $("hub-import-apply-table").hidden = items.length === 0;
      $("hub-import-apply-body").innerHTML = items.map((item) => `
        <tr>
          <td class="mono">${escapeHtml(text(item.skill_id))}</td>
          <td>${pill(item.allowed ? "allow" : "block", item.allowed ? "green" : "yellow")}<div class="mini-label">${escapeHtml(text(item.action))}</div></td>
          <td>${escapeHtml(text(item.reason))}</td>
        </tr>
      `).join("");
    }

    function planCell(label, value) {
      return `<div class="plan-cell"><div class="mini-label">${escapeHtml(label)}</div><div class="mini-value mono">${escapeHtml(text(value))}</div></div>`;
    }

    function hubImportPreviewItems(items) {
      const order = ["importable", "update_available", "already_in_hub"];
      const selected = [];
      for (const status of order) {
        selected.push(...items.filter((item) => item.status === status).slice(0, 8));
      }
      return selected;
    }

    function hubStatusKind(status) {
      if (status === "importable") return "green";
      if (status === "update_available") return "yellow";
      if (status === "already_in_hub") return "";
      return "";
    }

    function duplicateSourceText(item) {
      const duplicateSources = Array.isArray(item.duplicate_sources) ? item.duplicate_sources : [];
      if (!duplicateSources.length) return text(item.path);
      return `${text(item.path)}; 同名来源 ${duplicateSources.length} 个`;
    }

    function projectionGap(projection) {
      if (!projection) return "-";
      return `${text(projection.missing)} / ${text(projection.drift)}`;
    }

    function shortHash(value) {
      if (!value) return "-";
      const raw = String(value);
      if (raw.length <= 12) return escapeHtml(raw);
      return `<span title="${escapeHtml(raw)}">${escapeHtml(raw.slice(0, 12))}</span>`;
    }

    function renderOperatorDevices(devices) {
      const rows = [
        ["Mac", devices.mac],
        ["OpenClaw", devices.openclaw],
      ];
      $("operator-devices").innerHTML = rows.map(([name, value]) => `
        <div class="device-line">
          <div class="key">${escapeHtml(name)}</div>
          <div class="value mono">${escapeHtml(text(value))}</div>
        </div>
      `).join("");
    }

    async function refresh() {
      try {
        const response = await fetch("/api/summary", { cache: "no-store" });
        const status = await response.json();
        if (!response.ok) throw new Error(status.error || `HTTP ${response.status}`);
        render(status);
      } catch (error) {
        $("error").textContent = error.message;
        $("error").style.display = "block";
        $("updated").textContent = "Update failed";
      }
    }

    $("refresh").addEventListener("click", refresh);
    $("hub-import-preview-button").addEventListener("click", generateHubImportPreview);
    refresh();
    setInterval(refresh, 30000);
  </script>
</body>
</html>
"""
