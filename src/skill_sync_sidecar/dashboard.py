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
        return self._snapshot_dir(force=False)

    def force_refresh(self) -> Path:
        return self._snapshot_dir(force=True)

    def _snapshot_dir(self, *, force: bool) -> Path:
        index_path = self.cache_dir / "index.json"
        now = time.monotonic()
        stale = now - self._last_refresh >= self.refresh_interval_seconds
        if force or stale or not index_path.exists():
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

    def get_summary(self, *, force: bool = False) -> tuple[int, dict]:
        if force:
            with self._lock:
                self._last_attempt_at = datetime.now(timezone.utc).isoformat()
            try:
                payload = self._refresh()
            except Exception as exc:  # pragma: no cover - defensive cache boundary
                with self._lock:
                    self._last_error = str(exc)
                    if self._inflight is not None and self._inflight.done():
                        self._inflight = None
                return 500, self._miss_payload(str(exc))
            with self._lock:
                self._store_locked(payload)
                if self._inflight is not None and self._inflight.done():
                    self._inflight = None
                return 200, self._payload_with_metadata("fresh", 0.0)

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
                    "next_action": "检查共享仓库、设备上报或 gateway 日志；没有可用缓存时 summary 会返回 503。",
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
    dashboard_health = _aggregate_health([status.get("health")] + [device.get("health") for device in devices])
    status["service_health"] = status.get("health")
    status["health"] = dashboard_health
    status["dashboard"] = {
        "health": dashboard_health,
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
        "skill_inventory": _skill_inventory_model(
            device_tools,
            central_skills=_central_snapshot_skill_items(config.remote_snapshot),
            blocked_items=blocked_items,
        ),
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
        "skills": _central_snapshot_skill_items(snapshot_dir, index=index),
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
    dashboard_health = _aggregate_health([status.get("health")] + [device.get("health") for device in devices])
    status["service_health"] = status.get("health")
    status["health"] = dashboard_health
    status["dashboard"] = {
        "health": dashboard_health,
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
        "skill_inventory": _skill_inventory_model(device_tools, central_skills=snapshot.get("skills"), blocked_items=blocked_items),
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
        "health": dashboard.get("health") or status.get("health"),
        "service_health": status.get("service_health"),
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
            "skill_inventory": dashboard.get("skill_inventory", {}),
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
                "note": "Gateway 只观察共享仓库快照；不扫描 NAS 容器内的工具目录。",
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
    current_note = "同步已完成" if status.get("health") == "green" else "需要查看确认清单"
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
            "note": "直接读取共享仓库快照，不依赖 Mac 静态导出",
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
        note = "远端同步已完成"
    elif health == "yellow":
        note = "远端有需要确认的同步项"
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
        next_action = "先处理待确认队列；OpenClaw 本地改动需要你确认后再发布。"
    elif health == "red":
        next_action = "先修复共享仓库快照、设备上报或 sidecar 进程异常。"
    else:
        next_action = "状态未知；先刷新 dashboard 或查看 sidecar 日志。"
    return {
        "headline": _headline_for_health(health),
        "next_action": next_action,
        "sync_path": "Mac / OpenClaw <-> 共享仓库 -> 各工具目录",
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
        return f"先处理 {target} 版本差异；生成只读差异报告后选择保留版本。"
    if category in {"delete", "delete_review"} or status_action == "local_deleted":
        return f"先处理 {target} 缺失项；建议先从共享仓库找回，确认废弃时再单独删除。"
    if status_action == "remote_deleted":
        return f"先处理 {target} 删除差异；确认是否保留本机版本，或接受共享仓库删除。"
    if category == "writer_policy" and status_action in {"push", "push_new"}:
        return f"先处理 {target}；确认后发布。"
    return f"先处理 {target}；查看确认清单。"


def _operator_issue_target(peer_id: Optional[str], peer_name: Optional[str], skill_id: Optional[str]) -> str:
    peer = peer_name or peer_id or "unknown-peer"
    return f"{peer} / {skill_id or 'unknown-skill'}"


def _operator_action_guide(health: str, blocked_items: list[dict]) -> dict:
    openclaw_push_items = [item for item in blocked_items if _is_openclaw_writer_policy_push(item)]
    conflict_items = [
        item for item in blocked_items
        if item.get("category") == "conflict" or item.get("status_action") == "conflict"
    ]
    delete_items = [
        item for item in blocked_items
        if item.get("category") in {"delete", "delete_review"} or item.get("status_action") in {"local_deleted", "remote_deleted"}
    ]
    if health == "green":
        return {
            "state": "green",
            "title": "同步已完成",
            "summary": "Mac、OpenClaw、共享仓库当前已对齐。需要新增、安装或同步 skill 时，展开“可选：新增或同步 skill”。",
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
    if health == "yellow" and conflict_items:
        skill_ids = _operator_skill_ids(conflict_items)
        skill_hint = "、".join(skill_ids[:3]) if skill_ids else "unknown-skill"
        if len(skill_ids) > 3:
            skill_hint += f" 等 {len(skill_ids)} 个"
        command = "skill-sync conflict-package --skill-id " + (skill_ids[0] if skill_ids else "unknown-skill")
        return {
            "state": "yellow",
            "title": "只剩版本差异需要确认",
            "summary": f"当前不能一键发布；只剩 {len(conflict_items)} 个版本差异：{skill_hint}。先生成只读差异报告，报告会告诉你该恢复共享仓库版、发布 OpenClaw 版，还是手动处理。",
            "steps": [
                {
                    "title": "生成只读差异报告",
                    "detail": "只把两边版本整理出来给你查看，不写共享仓库，也不改 OpenClaw。",
                    "command": command,
                    "kind": "review",
                },
                {
                    "title": "选择保留哪边",
                    "detail": "报告会给出推荐动作：恢复共享仓库版、发布 OpenClaw 版，或手动整理最终版本。",
                    "kind": "publish",
                },
                {
                    "title": "刷新状态",
                    "detail": "处理完成后刷新本页，确认项数字下降才算闭环。",
                    "command": "scripts/operator-status.sh",
                    "kind": "verify",
                },
            ],
            "skills": skill_ids,
            "note": "黄色在这里表示安全暂停，不表示服务坏了。",
        }
    if health == "yellow" and delete_items:
        skill_ids = _operator_skill_ids(delete_items)
        skill_hint = "、".join(skill_ids[:3]) if skill_ids else "unknown-skill"
        if len(skill_ids) > 3:
            skill_hint += f" 等 {len(skill_ids)} 个"
        return {
            "state": "yellow",
            "title": "先处理缺失/删除确认",
            "summary": f"当前有 {len(delete_items)} 个缺失/删除确认：{skill_hint}。默认安全动作是保留共享仓库，不会静默删除；如果是误删，先从共享仓库恢复到缺失设备。",
            "steps": [
                {
                    "title": "先保留共享仓库",
                    "detail": "删除确认不会通过发布按钮自动处理，也不会一键删除共享仓库版本。",
                    "kind": "review",
                },
                {
                    "title": "误删就恢复",
                    "detail": "如果这个 skill 还要用，点页面里的恢复按钮，从共享仓库恢复到缺失设备。",
                    "kind": "publish",
                },
                {
                    "title": "确实废弃再删除",
                    "detail": "只有确认 skill 已废弃时，才走单独删除审批；不要用普通发布流程处理删除。",
                    "kind": "verify",
                },
            ],
            "skills": skill_ids,
            "note": "红色邮件提醒来自这类高风险删除确认；它需要你决定保留并恢复，还是单独审批删除。",
        }
    if health == "yellow" and openclaw_push_items:
        skill_ids = _operator_skill_ids(openclaw_push_items)
        skill_hint = f"（{'、'.join(skill_ids)}）" if len(skill_ids) <= 2 else "（见下方列表）"
        dry_run = _approved_push_batch_command(skill_ids)
        publish = _approved_push_batch_command(skill_ids, yes=True)
        source_changed_count = sum(1 for item in openclaw_push_items if item.get("operator_state") == "source_changed")
        if source_changed_count:
            title = "OpenClaw 还有新修改"
            summary = f"OpenClaw 有 {source_changed_count} 个 skill 的本地版本又不同于共享仓库{skill_hint}。这不是发布失败；如果还在改，可以先放着，如果已经改完，直接检查最新版本。"
            first_step = "改完后检查最新版本"
            first_detail = "检查只读，不写共享仓库；如果检查期间 skill 又变化，系统会自动拒绝写入。"
            second_detail = "检查结果显示可以发布后，再输入 PUBLISH 写入共享仓库。"
            note = "反复出现同一个 skill 时，通常表示源端还在写文件；这只会保护该 skill，不应阻塞其他独立更新。"
        else:
            title = "OpenClaw 更新需要确认"
            summary = f"OpenClaw 有 {len(skill_ids)} 个本地 skill 变更{skill_hint}，sidecar 已暂停自动同步；先检查确认，安全后再发布到共享仓库。刚发布过同一项又出现，表示 OpenClaw 又产生了新修改，不是按钮失效。"
            first_step = "先检查，不上传"
            first_detail = "只看这次会改什么，不写共享仓库。"
            second_detail = "只有检查结果显示可以发布，且这些 skill 不再继续编辑时，才确认发布。"
            note = "如果 OpenClaw 上这些 skill 仍在被优化，可以先放着继续做；改完后重新检查并确认发布。反复出现同一个 skill 时，只保护该 skill，不应阻塞其他独立更新。"
        return {
            "state": "yellow",
            "title": title,
            "summary": summary,
            "steps": [
                {
                    "title": first_step,
                    "detail": first_detail,
                    "command": dry_run,
                    "kind": "dry_run",
                },
                {
                    "title": "确认安全后再发布",
                    "detail": second_detail,
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
            "note": note,
        }
    if health == "yellow":
        return {
            "state": "yellow",
            "title": "有同步事项需要确认",
            "summary": f"当前有 {len(blocked_items)} 个需要确认的同步事项，系统已暂停自动写入以避免误同步。",
            "steps": [
                {
                    "title": "先看确认清单",
                    "detail": "查看下面的确认清单，确认每个 skill 的来源设备、原因和建议命令。",
                    "kind": "review",
                },
                {
                    "title": "处理后刷新状态",
                    "detail": "处理完成后刷新本页，确认数量下降。",
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
            "summary": "当前不是审批问题，而是共享仓库、设备上报或后台服务可能异常。",
            "steps": [
                {
                    "title": "先检查状态",
                    "detail": "运行状态检查，看错误集中在共享仓库、缓存还是设备上报。",
                    "command": "scripts/operator-status.sh",
                    "kind": "diagnose",
                },
                {
                    "title": "再看服务日志",
                    "detail": "如果状态检查仍为 red，再查看 Gateway / sidecar 容器日志。",
                    "kind": "logs",
                },
            ],
            "note": "红色时先不要发布，先恢复状态可读性。",
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


def _is_openclaw_source_changed(item: dict) -> bool:
    if not _is_openclaw_writer_policy_push(item):
        return False
    status_action = item.get("status_action")
    if status_action not in {"push", "push_new", "local_new"}:
        return False
    local_hash = item.get("local_hash")
    if not local_hash:
        return False
    remote_hash = item.get("remote_hash")
    if status_action in {"push_new", "local_new"}:
        return remote_hash in {None, "", "null"}
    base_hash = item.get("base_hash")
    return bool(base_hash and remote_hash and base_hash == remote_hash and local_hash != remote_hash)


def _local_workspace_model(devices: list[dict], device_tools: list[dict], blocked_items: list[dict]) -> dict:
    mac = _find_device(devices, "mac")
    mac_tools = _find_device_tool_group(device_tools, "mac")
    tools = mac_tools.get("tools") if isinstance(mac_tools.get("tools"), list) else []
    mac_blocked = [item for item in blocked_items if item.get("peer_id") == "mac"]
    remote_blocked = [item for item in blocked_items if item.get("peer_id") != "mac"]
    reported = bool(mac_tools.get("reported"))
    return {
        "title": "本机 Skill 管理",
        "scope": "local",
        "device_id": "mac",
        "device_name": mac.get("name") or "Mac 本机",
        "health": mac.get("health") or "unknown",
        "reported": reported,
        "freshness": mac_tools.get("freshness") or mac.get("freshness") or _freshness_info(None),
        "tools": tools,
        "total_skills": sum(int(tool.get("skills") or 0) for tool in tools if isinstance(tool, dict)),
        "blocked": len(mac_blocked),
        "remote_blocked": len(remote_blocked),
        "operations": {
            "scan_local": True,
            "dry_run": True,
            "publish_to_central": len(mac_blocked) > 0,
            "operate_other_devices": False,
        },
        "primary_action": "本机助手在线后，可扫描本机 skill，并在发布前做安全检查。",
        "boundary": "这里默认只操作浏览器所在 Mac；其他设备只读展示，除非该设备自己的 Agent 暴露受控操作。",
        "remote_blocked_note": f"当前另有 {len(remote_blocked)} 个非本机确认项，放在设备地图里处理。",
    }


def _central_repository_model(status: dict, *, snapshot: Optional[dict], tools: list[dict], blocked_items: list[dict]) -> dict:
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    projection_total = sum(int((tool.get("projection") or {}).get("canonical_targeted") or 0) for tool in tools if isinstance(tool, dict))
    return {
        "title": "共享仓库",
        "scope": "central",
        "role": "共享版本快照",
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
        "boundary": "共享仓库保存各设备共同使用的版本；只有你明确确认后才会写入。",
    }


def _central_snapshot_skill_items(snapshot_dir: Path, *, index: Optional[dict] = None) -> list[dict]:
    try:
        snapshot_index = index if isinstance(index, dict) else json.loads((snapshot_dir / "index.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = []
    for skill in snapshot_index.get("skills", []):
        if not isinstance(skill, dict):
            continue
        skill_id = str(skill.get("skill_id") or "").strip()
        if not skill_id:
            continue
        items.append(
            {
                "skill_id": skill_id,
                "name": skill.get("name"),
                "description": skill.get("description"),
                "scope": skill.get("scope") or "global",
                "content_hash": skill.get("content_hash"),
                "targets": skill.get("targets") or [],
            }
        )
    items.sort(key=lambda item: str(item.get("skill_id") or ""))
    return items


def _skill_inventory_model(device_tools: list[dict], *, central_skills: object, blocked_items: list[dict]) -> dict:
    central_items = central_skills if isinstance(central_skills, list) else []
    by_skill: dict[str, dict] = {}
    central_ids = set()
    for skill in central_items:
        if not isinstance(skill, dict):
            continue
        skill_id = str(skill.get("skill_id") or "").strip()
        if not skill_id:
            continue
        central_ids.add(skill_id)
        entry = by_skill.setdefault(skill_id, _empty_skill_inventory_item(skill_id))
        entry["name"] = skill.get("name") or entry.get("name") or skill_id
        entry["description"] = skill.get("description") or entry.get("description")
        entry["scope"] = skill.get("scope") or entry.get("scope") or "global"
        entry["central"] = {
            "state": "published",
            "content_hash": skill.get("content_hash"),
            "targets": skill.get("targets") or [],
        }

    for group in device_tools:
        if not isinstance(group, dict):
            continue
        device_id = str(group.get("device_id") or "unknown")
        device_name = group.get("device_name") or device_id
        tools = group.get("tools") if isinstance(group.get("tools"), list) else []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_id = str(tool.get("id") or "unknown")
            tool_name = tool.get("name") or tool_id
            for skill in tool.get("skill_items") or []:
                if not isinstance(skill, dict):
                    continue
                skill_id = str(skill.get("skill_id") or "").strip()
                if not skill_id:
                    continue
                entry = by_skill.setdefault(skill_id, _empty_skill_inventory_item(skill_id))
                entry["name"] = entry.get("name") or skill.get("name") or skill_id
                entry["description"] = entry.get("description") or skill.get("description")
                entry["scope"] = _merge_skill_scope(entry.get("scope"), skill.get("scope"))
                entry["installations"].append(
                    {
                        "device_id": device_id,
                        "device_name": device_name,
                        "tool_id": tool_id,
                        "tool_name": tool_name,
                        "state": "installed",
                        "path": skill.get("path"),
                        "content_hash": skill.get("content_hash"),
                        "risk_level": skill.get("risk_level"),
                    }
                )
                if skill_id not in central_ids:
                    entry["central"]["state"] = "unpublished"

    blocked_by_skill: dict[str, list[dict]] = {}
    for item in blocked_items:
        if not isinstance(item, dict):
            continue
        skill_id = str(item.get("skill_id") or "").strip()
        if skill_id:
            blocked_by_skill.setdefault(skill_id, []).append(item)
    for skill_id, items in blocked_by_skill.items():
        entry = by_skill.setdefault(skill_id, _empty_skill_inventory_item(skill_id))
        entry["sync_state"] = _inventory_sync_state(items)
        entry["pending"] = len(items)

    items = []
    for entry in by_skill.values():
        installs = entry.get("installations") if isinstance(entry.get("installations"), list) else []
        devices = sorted({str(item.get("device_id")) for item in installs if item.get("device_id")})
        tools = sorted({str(item.get("tool_id")) for item in installs if item.get("tool_id")})
        entry["installed_devices"] = devices
        entry["installed_tools"] = tools
        entry["tool_count"] = len(tools)
        entry["device_count"] = len(devices)
        entry["action"] = _inventory_action(entry)
        items.append(entry)
    items.sort(key=lambda item: (0 if item.get("pending") else 1, str(item.get("skill_id") or "")))
    visible = items[:80]
    return {
        "title": "Skill 清单",
        "scope": "current-client",
        "summary": "按 skill 查看中央仓库和各工具安装状态；首页只显示下一步，完整列表放在这里。",
        "total": len(items),
        "published": sum(1 for item in items if (item.get("central") or {}).get("state") == "published"),
        "unpublished": sum(1 for item in items if (item.get("central") or {}).get("state") == "unpublished"),
        "project": sum(1 for item in items if item.get("scope") == "project"),
        "global": sum(1 for item in items if item.get("scope") == "global"),
        "pending": sum(1 for item in items if int(item.get("pending") or 0) > 0),
        "visible_limit": len(visible),
        "items": visible,
    }


def _empty_skill_inventory_item(skill_id: str) -> dict:
    return {
        "skill_id": skill_id,
        "name": skill_id,
        "description": None,
        "scope": "global",
        "central": {"state": "unpublished"},
        "sync_state": "ok",
        "pending": 0,
        "installations": [],
    }


def _merge_skill_scope(existing: object, incoming: object) -> str:
    if existing == "project" or incoming == "project":
        return "project"
    if existing == "device-private" or incoming == "device-private":
        return "device-private"
    return str(existing or incoming or "global")


def _inventory_sync_state(items: list[dict]) -> str:
    states = {str(item.get("operator_state") or item.get("category") or item.get("status_action") or "") for item in items}
    if "conflict" in states:
        return "conflict"
    if "delete_review" in states or "delete" in states:
        return "delete_review"
    if "source_changed" in states:
        return "source_changed"
    return "pending_publish"


def _inventory_action(entry: dict) -> str:
    central_state = (entry.get("central") or {}).get("state")
    sync_state = entry.get("sync_state")
    if sync_state == "source_changed":
        return "改完后检查最新版本。"
    if sync_state == "conflict":
        return "先看只读差异报告。"
    if sync_state == "delete_review":
        return "先决定恢复还是废弃。"
    if sync_state == "pending_publish":
        return "检查通过后可发布中央仓库。"
    if central_state == "unpublished":
        return "可选择发布到中央仓库。"
    return "可选择安装到本机工具。"


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
        return "同步已完成"
    if health == "yellow":
        return "存在需要确认的同步项"
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
    return sorted(items, key=_blocked_item_sort_key)


def _blocked_item_sort_key(item: dict) -> tuple[int, str, str]:
    state = item.get("operator_state") or _blocked_item_operator_state(item)
    if state == "conflict":
        priority = 0
    elif state == "delete_review":
        priority = 1
    elif state == "source_changed":
        priority = 2
    elif state == "explicit_publish":
        priority = 3
    else:
        priority = 4
    return (
        priority,
        str(item.get("peer_id") or item.get("peer_name") or ""),
        str(item.get("skill_id") or ""),
    )


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
        copied.setdefault("operator_state", _blocked_item_operator_state(copied))
        copied.setdefault("status_description", _blocked_item_status_description(copied))
        copied.setdefault("operator_action", _blocked_item_operator_action(copied))
        command = _blocked_item_operator_command(copied)
        if command:
            copied.setdefault("operator_command", command)
        items.append(copied)
    return items


def _blocked_item_operator_state(item: dict) -> str:
    if _is_openclaw_source_changed(item):
        return "source_changed"
    if item.get("category") == "conflict" or item.get("status_action") == "conflict":
        return "conflict"
    if item.get("status_action") in {"local_deleted", "remote_deleted"} or item.get("category") in {"delete", "delete_review"}:
        return "delete_review"
    if _is_openclaw_writer_policy_push(item):
        return "explicit_publish"
    return "review_required"


def _blocked_item_status_description(item: dict) -> str:
    if item.get("operator_state") == "source_changed" or _is_openclaw_source_changed(item):
        return "OpenClaw 当前版本又不同于共享仓库；这表示源端有新修改，不是上次发布失败。"
    if _is_openclaw_writer_policy_push(item):
        return "OpenClaw 本地版本需要你显式确认后才会写入共享仓库。"
    if item.get("category") == "conflict" or item.get("status_action") == "conflict":
        return "两端都改过，不能自动覆盖；先看只读差异报告。"
    if item.get("status_action") in {"local_deleted", "remote_deleted"} or item.get("category") in {"delete", "delete_review"}:
        return "这是删除/缺失决策，当前按钮不会静默删除共享仓库。"
    return "需要人工确认后再继续。"


def _blocked_item_operator_action(item: dict) -> str:
    peer_id = item.get("peer_id")
    peer_name = item.get("peer_name")
    skill_id = item.get("skill_id")
    status_action = item.get("status_action")
    category = item.get("category")
    if item.get("operator_state") == "source_changed" or _is_openclaw_source_changed(item):
        return f"OpenClaw 又产生了 {skill_id or 'unknown-skill'} 的新修改；改完后检查最新版本，再确认发布。"
    if category == "conflict":
        return _operator_issue_action(peer_id, peer_name, skill_id, status_action, category)
    if _is_openclaw_writer_policy_push(item):
        if peer_id in {"oc-vps", "openclaw"}:
            return f"先在 Mac 检查 OpenClaw 更新 {skill_id or 'unknown-skill'}，确认后再发布。"
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
                        "next_action": "先检查共享仓库连接、认证或 gateway 缓存目录。",
                        "sync_path": "共享仓库 -> Gateway",
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
    handler = _handler_factory(status_provider, preview_provider, cache.force_refresh)
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


def _query_has_force_refresh(path: str) -> bool:
    query = path.split("?", 1)[1] if "?" in path else ""
    return any(part in {"refresh=1", "refresh=true", "force=1", "force=true"} for part in query.split("&"))


def _handler_factory(
    status_provider: Callable[[], dict],
    hub_import_preview_provider: Optional[Callable[[], dict]] = None,
    force_refresh_provider: Optional[Callable[[], None]] = None,
):
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
                    if _query_has_force_refresh(self.path) and force_refresh_provider is not None:
                        force_refresh_provider()
                    payload = status_provider()
                    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
                    self._send(200, "application/json; charset=utf-8", body)
                except Exception as exc:  # pragma: no cover - defensive server boundary
                    body = json.dumps({"ok": False, "health": "red", "error": str(exc)}, ensure_ascii=False).encode("utf-8")
                    self._send(500, "application/json; charset=utf-8", body)
                return
            if path == "/api/summary":
                force = _query_has_force_refresh(self.path)
                if force and force_refresh_provider is not None:
                    force_refresh_provider()
                status_code, payload = summary_cache.get_summary(force=force)
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
      grid-template-columns: minmax(0, 1fr);
      gap: 8px;
      margin: 8px 0 12px;
      align-items: stretch;
    }
    .status-chip {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      background: #fff;
      min-width: 0;
    }
    .focus-main {
      border-left: 0;
      padding: 9px 12px;
      background: #fbfcfe;
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      gap: 10px;
      align-items: center;
    }
    .focus-title {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: 6px;
      color: var(--ink);
      font-size: 14px;
      font-weight: 850;
      line-height: 1.15;
      margin-top: 0;
    }
    .focus-title strong {
      font-size: 18px;
      line-height: 1;
    }
    .focus-note {
      color: var(--muted);
      margin-top: 0;
      overflow-wrap: anywhere;
      font-size: 12px;
    }
    .focus-side {
      display: none;
      gap: 8px;
      align-content: center;
      background: #fbfcfe;
    }
    .focus-side button {
      width: 100%;
    }
    .focus-side-actions {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 7px;
    }
    .focus-side-note {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .focus-metrics {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 6px;
    }
    .focus-metric {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 5px 7px;
      background: #fff;
      min-width: 0;
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
    .focus-metric .status-chip-value {
      font-size: 15px;
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
    .secondary-context {
      margin: 12px 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      overflow: hidden;
    }
    .secondary-context > summary {
      cursor: pointer;
      list-style: none;
      padding: 13px 16px;
      font-weight: 720;
      color: var(--ink);
      background: #fafbfd;
    }
    .secondary-context > summary::-webkit-details-marker {
      display: none;
    }
    .secondary-context > summary::after {
      content: "展开";
      float: right;
      color: var(--muted);
      font-weight: 650;
      font-size: 12px;
    }
    .secondary-context[open] > summary {
      border-bottom: 1px solid var(--line);
    }
    .secondary-context[open] > summary::after {
      content: "收起";
    }
    .secondary-context-body {
      display: grid;
      gap: 12px;
      padding: 14px 16px 16px;
    }
    .secondary-context .scope-switchboard,
    .secondary-context .decision-console {
      margin-bottom: 0;
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
      margin: 12px 0;
      border-left: 4px solid #d8a300;
    }
    .review-queue-summary {
      color: var(--muted);
      margin-bottom: 10px;
      overflow-wrap: anywhere;
    }
    .review-recommendation {
      display: grid;
      gap: 8px;
      border: 1px solid #c8d7ef;
      border-radius: 8px;
      background: #f7fbff;
      padding: 10px 12px;
      margin: 10px 0;
    }
    .review-recommendation-title {
      color: var(--ink);
      font-weight: 850;
      font-size: 13px;
    }
    .review-recommendation-summary {
      color: var(--ink);
      font-weight: 720;
      overflow-wrap: anywhere;
    }
    .review-recommendation-steps {
      display: grid;
      gap: 6px;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .review-recommendation-step {
      display: grid;
      grid-template-columns: 24px minmax(0, 1fr);
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .review-recommendation-index {
      width: 22px;
      height: 22px;
      border-radius: 999px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      color: #1f4f8a;
      background: #e8f1ff;
      font-weight: 850;
      font-size: 11px;
    }
    .review-recommendation-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .review-recommendation-note {
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .review-list {
      display: grid;
      gap: 6px;
    }
    .review-group {
      display: grid;
      gap: 6px;
      min-width: 0;
    }
    .review-group-title {
      color: var(--ink);
      font-weight: 850;
      font-size: 13px;
      margin-top: 4px;
      overflow-wrap: anywhere;
    }
    .review-group-note {
      color: var(--muted);
      font-size: 12px;
      margin-top: -2px;
      overflow-wrap: anywhere;
    }
    .review-progress {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin: 10px 0;
    }
    .review-stage {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 8px 10px;
      min-width: 0;
    }
    .review-stage-title {
      color: var(--ink);
      font-size: 12px;
      font-weight: 800;
      margin-bottom: 2px;
      overflow-wrap: anywhere;
    }
    .review-stage-note {
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .review-feedback {
      display: grid;
      gap: 3px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px 10px;
      margin: 8px 0;
      background: #fff;
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    .review-feedback[hidden] {
      display: none;
    }
    .review-feedback strong {
      color: var(--ink);
    }
    .review-feedback.green {
      border-color: #b8d8c8;
      background: #f6fbf8;
    }
    .review-feedback.yellow {
      border-color: #e8d29c;
      background: #fffaf0;
    }
    .review-feedback.red {
      border-color: #efb8b8;
      background: #fff5f5;
    }
    .review-item {
      display: grid;
      grid-template-columns: minmax(180px, .9fr) minmax(0, 1fr) minmax(104px, auto);
      gap: 8px;
      align-items: start;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 10px;
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
      font-weight: 720;
      overflow-wrap: anywhere;
    }
    .review-next-step {
      color: var(--muted);
      font-size: 12px;
      margin-top: 4px;
      overflow-wrap: anywhere;
    }
    .review-decision {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      color: var(--ink);
      font-size: 12px;
      line-height: 1.45;
      margin-top: 7px;
      padding: 7px 8px;
      overflow-wrap: anywhere;
    }
    .review-decision strong {
      display: block;
      margin-bottom: 2px;
    }
    .review-result {
      margin-top: 6px;
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
    .support-drawer {
      margin: 10px 0 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      overflow: hidden;
    }
    .support-drawer > summary {
      cursor: pointer;
      list-style: none;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 780;
      background: #fbfcfe;
    }
    .support-drawer > summary::-webkit-details-marker {
      display: none;
    }
    .support-drawer > summary::after {
      content: "打开";
      color: var(--muted);
      font-size: 12px;
      font-weight: 760;
      flex: 0 0 auto;
    }
    .support-drawer[open] > summary {
      border-bottom: 1px solid var(--line);
    }
    .support-drawer[open] > summary::after {
      content: "收起";
    }
    .support-drawer-title {
      display: grid;
      gap: 2px;
      min-width: 0;
    }
    .support-drawer-title strong {
      color: var(--ink);
      font-size: 14px;
      font-weight: 850;
    }
    .support-drawer-title span {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .support-drawer-body {
      display: grid;
      gap: 10px;
      padding: 10px;
      background: #f8fafc;
    }
    .easy-workspace {
      margin: 0;
      padding: 0;
      overflow: hidden;
      background: #fff;
    }
    .easy-workspace-head {
      cursor: pointer;
      list-style: none;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 16px 18px;
      background: #fbfcff;
    }
    .easy-workspace-head::-webkit-details-marker {
      display: none;
    }
    .easy-workspace-head::after {
      content: "展开";
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      margin-left: 4px;
      flex: 0 0 auto;
    }
    .easy-workspace[open] .easy-workspace-head {
      border-bottom: 1px solid var(--line);
    }
    .easy-workspace[open] .easy-workspace-head::after {
      content: "收起";
    }
    .easy-workspace-title {
      display: grid;
      gap: 3px;
      min-width: 0;
    }
    .easy-workspace-title strong {
      color: var(--ink);
      font-size: 18px;
      font-weight: 880;
    }
    .easy-workspace-title span {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .easy-workspace-grid {
      display: grid;
      grid-template-columns: minmax(320px, 1.05fr) minmax(280px, .95fr);
      gap: 0;
    }
    .easy-card {
      padding: 16px 18px;
      display: grid;
      gap: 12px;
      min-width: 0;
    }
    .easy-card + .easy-card {
      border-left: 1px solid var(--line);
    }
    .easy-card-label {
      color: var(--blue);
      font-size: 12px;
      font-weight: 860;
    }
    .easy-card h2 {
      margin: 0;
      color: var(--ink);
      font-size: 17px;
      line-height: 1.25;
    }
    .easy-card p {
      margin: 0;
      color: var(--muted);
      line-height: 1.45;
      overflow-wrap: anywhere;
    }
    .easy-action-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .easy-action-row.pending {
      display: none;
    }
    .easy-action-row.pending.ready {
      display: flex;
    }
    .easy-action-row button.primary {
      background: var(--ink);
      color: #fff;
      border-color: var(--ink);
    }
    .easy-sync-empty {
      border: 1px solid #cce4d6;
      background: #f5fbf7;
      border-radius: 8px;
      padding: 10px 12px;
      color: var(--green);
      font-weight: 760;
      line-height: 1.35;
    }
    .easy-sync-empty.has-work {
      display: none;
    }
    .easy-steps {
      display: grid;
      gap: 8px;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .easy-steps li {
      display: grid;
      grid-template-columns: 28px minmax(0, 1fr);
      gap: 8px;
      align-items: start;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.4;
    }
    .easy-steps strong {
      width: 24px;
      height: 24px;
      border-radius: 999px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: #e8f1ff;
      color: #1f4f8a;
      font-size: 12px;
      font-weight: 880;
    }
    .workbench-grid {
      display: grid;
      grid-template-columns: minmax(340px, 1.2fr) minmax(280px, .9fr) minmax(260px, .9fr);
      gap: 12px;
      margin: 12px 0 0;
    }
    .simple-action-panel {
      margin: 10px 0 8px;
      display: grid;
      gap: 14px;
      border-left: 0;
      background: #fbfdff;
      padding: 20px;
    }
    .simple-action-panel.green {
      border-left-color: var(--green);
      background: #f8fffb;
    }
    .simple-action-panel.green .simple-action-hero {
      grid-template-columns: minmax(0, 1fr);
    }
    .simple-action-panel.yellow {
      background: #fffdf7;
      border-color: #e8d29c;
    }
    .simple-action-panel.version-difference {
      background: #f8fbff;
      border-color: #b8cef0;
    }
    .simple-action-panel.red {
      border-left-color: var(--red);
      background: #fff8f8;
    }
    .simple-action-hero {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(240px, auto);
      gap: 20px;
      align-items: center;
    }
    .simple-action-plain {
      display: grid;
      gap: 8px;
    }
    .simple-action-eyebrow {
      color: var(--muted);
      font-size: 12px;
      font-weight: 820;
      text-transform: none;
    }
    .simple-action-title {
      color: var(--ink);
      font-size: 26px;
      font-weight: 880;
      line-height: 1.2;
      overflow-wrap: anywhere;
    }
    .simple-action-summary {
      color: var(--muted);
      font-size: 15px;
      line-height: 1.5;
      max-width: 900px;
      overflow-wrap: anywhere;
    }
    .simple-action-grid {
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(260px, .9fr);
      gap: 12px;
      align-items: start;
    }
    .simple-action-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 12px;
      min-width: 0;
    }
    .simple-action-card-title {
      color: var(--ink);
      font-weight: 840;
      margin-bottom: 6px;
    }
    .simple-action-steps {
      display: grid;
      gap: 8px;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .simple-action-step {
      display: grid;
      grid-template-columns: 26px minmax(0, 1fr);
      gap: 8px;
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    .simple-action-index {
      width: 24px;
      height: 24px;
      border-radius: 999px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: #e8f1ff;
      color: #1f4f8a;
      font-size: 12px;
      font-weight: 860;
    }
    .simple-action-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }
    .simple-action-actions.single-primary {
      justify-content: flex-end;
      margin-top: 0;
    }
    .simple-action-actions.single-primary button {
      min-width: 220px;
      min-height: 48px;
      font-size: 15px;
      white-space: normal;
      text-align: center;
    }
    .simple-action-actions.single-primary button span {
      display: block;
      font-size: 12px;
      font-weight: 600;
      opacity: .78;
      line-height: 1.35;
      margin-top: 2px;
    }
    .simple-choice-grid {
      display: grid;
      gap: 8px;
      min-width: min(360px, 100%);
    }
    .simple-choice-grid button {
      width: 100%;
      min-width: 0;
      min-height: 54px;
      text-align: left;
      white-space: normal;
    }
    .simple-choice-grid button.primary {
      background: var(--ink);
      color: #fff;
      border-color: var(--ink);
    }
    .simple-choice-grid button span {
      display: block;
      font-size: 12px;
      font-weight: 600;
      opacity: .78;
      line-height: 1.35;
      margin-top: 2px;
    }
    .simple-choice-grid.single-choice {
      min-width: min(340px, 100%);
    }
    .simple-choice-grid.single-choice button {
      text-align: center;
    }
    .simple-action-actions .primary {
      background: var(--ink);
      color: #fff;
      border-color: var(--ink);
    }
    .simple-action-note {
      color: var(--muted);
      font-size: 12px;
      margin-top: 8px;
      overflow-wrap: anywhere;
    }
    .simple-action-feedback {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      background: #fff;
      color: var(--muted);
      display: grid;
      gap: 2px;
      font-size: 13px;
    }
    .simple-action-feedback[hidden] {
      display: none;
    }
    .simple-action-feedback strong {
      color: var(--ink);
      font-size: 14px;
    }
    .simple-action-feedback.green {
      border-color: #a8dec2;
      background: #f4fff8;
    }
    .simple-action-feedback.yellow {
      border-color: #efd59a;
      background: #fffaf0;
    }
    .simple-action-feedback.red {
      border-color: #efb1b1;
      background: #fff7f7;
    }
    .simple-action-list {
      display: grid;
      gap: 6px;
      margin-top: 8px;
    }
    .simple-action-more {
      border-top: 1px solid var(--line);
      padding-top: 8px;
    }
    .simple-action-more > summary {
      cursor: pointer;
      list-style: none;
      color: var(--muted);
      font-size: 12px;
      font-weight: 780;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .simple-action-more > summary::-webkit-details-marker {
      display: none;
    }
    .simple-action-more > summary::after {
      content: "展开";
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
    }
    .simple-action-more[open] > summary::after {
      content: "收起";
    }
    .simple-action-more-body {
      display: grid;
      gap: 8px;
      margin-top: 8px;
    }
    .simple-action-facts {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }
    .simple-action-fact {
      border-top: 1px solid var(--line);
      padding: 10px 0 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .simple-action-fact strong {
      display: block;
      color: var(--ink);
      font-size: 13px;
      margin-bottom: 2px;
    }
    .simple-action-done-line {
      border-top: 1px solid var(--line);
      padding-top: 10px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }
    .simple-action-done-line strong {
      color: var(--ink);
      font-weight: 820;
    }
    .simple-task-list {
      display: grid;
      gap: 8px;
    }
    .simple-task-card {
      display: grid;
      gap: 8px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 10px;
      min-width: 0;
    }
    .simple-task-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }
    .simple-task-title {
      color: var(--ink);
      font-weight: 840;
      overflow-wrap: anywhere;
    }
    .simple-task-detail {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }
    .simple-task-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .simple-task-actions .primary {
      background: var(--ink);
      color: #fff;
      border-color: var(--ink);
    }
    .simple-decision-list {
      display: grid;
      gap: 8px;
      margin-top: 10px;
    }
    .simple-decision-card {
      display: grid;
      gap: 8px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 10px 12px;
      min-width: 0;
    }
    .simple-decision-card.warning {
      border-color: #e8d29c;
      background: #fffdf7;
    }
    .simple-decision-head {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      align-items: flex-start;
    }
    .simple-decision-title {
      color: var(--ink);
      font-weight: 850;
      overflow-wrap: anywhere;
    }
    .simple-decision-source {
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .simple-decision-copy {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }
    .simple-decision-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .simple-decision-actions .primary {
      background: var(--ink);
      color: #fff;
      border-color: var(--ink);
    }
    .conflict-resolution {
      display: grid;
      gap: 14px;
      border: 1px solid #e8d29c;
      border-radius: 8px;
      background: #fffdf7;
      padding: 18px;
      margin-top: 10px;
    }
    .conflict-resolution-title {
      color: var(--ink);
      font-size: 20px;
      font-weight: 860;
      overflow-wrap: anywhere;
    }
    .conflict-resolution-summary {
      color: var(--muted);
      line-height: 1.45;
      overflow-wrap: anywhere;
    }
    .conflict-next-action {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(220px, auto);
      gap: 14px;
      align-items: center;
      border: 1px solid #b8cef0;
      border-radius: 8px;
      background: #f8fbff;
      padding: 14px;
    }
    .conflict-next-title {
      color: var(--ink);
      font-size: 18px;
      font-weight: 860;
      overflow-wrap: anywhere;
    }
    .conflict-next-copy {
      color: var(--muted);
      line-height: 1.45;
      margin-top: 4px;
      overflow-wrap: anywhere;
    }
    .conflict-next-action button.primary {
      background: var(--ink);
      color: #fff;
      border-color: var(--ink);
      min-height: 44px;
      width: 100%;
    }
    .conflict-alternatives {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      overflow: hidden;
    }
    .conflict-alternatives > summary {
      cursor: pointer;
      list-style: none;
      padding: 11px 13px;
      color: var(--blue);
      font-weight: 820;
    }
    .conflict-alternatives > summary::-webkit-details-marker {
      display: none;
    }
    .conflict-alternatives > summary::after {
      content: "展开";
      float: right;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }
    .conflict-alternatives[open] > summary {
      border-bottom: 1px solid var(--line);
    }
    .conflict-alternatives[open] > summary::after {
      content: "收起";
    }
    .conflict-choice-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      padding: 10px;
    }
    .conflict-choice {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 12px;
      min-width: 0;
      display: grid;
      gap: 6px;
    }
    .conflict-choice strong {
      color: var(--ink);
      font-size: 13px;
      overflow-wrap: anywhere;
    }
    .conflict-choice span {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
      overflow-wrap: anywhere;
    }
    .conflict-choice button {
      margin-top: 2px;
      min-height: 38px;
    }
    .conflict-version-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }
    .conflict-version-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 12px;
      min-width: 0;
      display: grid;
      gap: 7px;
    }
    .conflict-version-label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 820;
    }
    .conflict-version-title {
      color: var(--ink);
      font-weight: 860;
      overflow-wrap: anywhere;
    }
    .conflict-version-desc,
    .conflict-version-files {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
      overflow-wrap: anywhere;
    }
    .conflict-version-files {
      border-top: 1px solid var(--line);
      padding-top: 7px;
    }
    .conflict-diagnostic {
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .conflict-diagnostic summary {
      cursor: pointer;
      color: var(--blue);
      font-weight: 760;
    }
    .simple-action-item {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      border-top: 1px solid var(--line);
      padding-top: 7px;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .advanced-workspace {
      margin: 12px 0;
    }
    .quick-status-details {
      margin: 10px 0 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      overflow: hidden;
    }
    .quick-status-details > summary {
      cursor: pointer;
      list-style: none;
      padding: 10px 12px;
      color: var(--muted);
      font-weight: 760;
      font-size: 13px;
    }
    .quick-status-details > summary::-webkit-details-marker {
      display: none;
    }
    .quick-status-details > summary::after {
      content: "展开";
      margin-left: 8px;
      color: var(--muted);
      font-weight: 700;
      font-size: 12px;
    }
    .quick-status-details[open] > summary {
      border-bottom: 1px solid var(--line);
    }
    .quick-status-details[open] > summary::after {
      content: "收起";
    }
    .quick-status-details .status-strip {
      margin: 0;
      padding: 10px;
    }
    .advanced-workspace > summary {
      cursor: pointer;
      color: var(--blue);
      font-weight: 840;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      list-style: none;
    }
    .advanced-workspace > summary::-webkit-details-marker {
      display: none;
    }
    .advanced-workspace > summary::after {
      content: "展开";
      margin-left: 8px;
      color: var(--muted);
      font-weight: 700;
    }
    .advanced-workspace[open] > summary::after {
      content: "收起";
    }
    .plain-detail-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin: 12px 0;
    }
    .plain-detail-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 12px;
      min-width: 0;
      display: grid;
      gap: 6px;
    }
    .plain-detail-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
    }
    .plain-detail-title {
      color: var(--ink);
      font-weight: 850;
      overflow-wrap: anywhere;
    }
    .plain-detail-line {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.4;
      overflow-wrap: anywhere;
    }
    .plain-detail-action {
      color: var(--ink);
      font-size: 13px;
      font-weight: 720;
      overflow-wrap: anywhere;
    }
    .technical-workspace {
      margin: 12px 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      overflow: hidden;
    }
    .technical-workspace > summary {
      cursor: pointer;
      list-style: none;
      padding: 11px 14px;
      color: var(--blue);
      font-weight: 820;
      background: #fbfcfe;
    }
    .technical-workspace > summary::-webkit-details-marker {
      display: none;
    }
    .technical-workspace > summary::after {
      content: "展开";
      margin-left: 8px;
      color: var(--muted);
      font-weight: 700;
    }
    .technical-workspace[open] > summary {
      border-bottom: 1px solid var(--line);
    }
    .technical-workspace[open] > summary::after {
      content: "收起";
    }
    .workspace-overview {
      margin: 12px 0;
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
      background: #f8fafc;
    }
    .overview-title {
      display: grid;
      gap: 2px;
      min-width: 0;
    }
    .overview-title strong {
      font-size: 16px;
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
      margin: 8px 0 10px;
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
    .workspace-flow {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin: 10px 0;
    }
    .workspace-step {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 8px 9px;
      min-width: 0;
    }
    .workspace-step strong {
      display: block;
      font-size: 13px;
      margin-bottom: 2px;
    }
    .workspace-step span {
      display: block;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .workspace-secondary {
      margin-top: 10px;
      border-top: 1px solid var(--line);
      padding-top: 9px;
    }
    .workspace-secondary > summary {
      cursor: pointer;
      color: var(--blue);
      font-size: 12px;
      font-weight: 760;
      list-style: none;
    }
    .workspace-secondary > summary::-webkit-details-marker {
      display: none;
    }
    .workspace-secondary > summary::after {
      content: "展开";
      margin-left: 6px;
      color: var(--muted);
      font-weight: 650;
    }
    .workspace-secondary[open] > summary::after {
      content: "收起";
    }
    .workspace-tool-summary {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 6px;
      margin: 10px 0;
    }
    .workspace-tool-summary-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 7px 8px;
      min-width: 0;
    }
    .workspace-tool-summary-value {
      color: var(--ink);
      font-size: 16px;
      font-weight: 820;
      line-height: 1.1;
      overflow-wrap: anywhere;
    }
    .workspace-tool-summary-label {
      color: var(--muted);
      font-size: 11px;
      margin-top: 2px;
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
    .workspace-tool-details {
      margin-top: 8px;
      border-top: 1px solid var(--line);
      padding-top: 8px;
    }
    .workspace-tool-details > summary {
      cursor: pointer;
      color: var(--blue);
      font-size: 12px;
      font-weight: 760;
      list-style: none;
    }
    .workspace-tool-details > summary::-webkit-details-marker {
      display: none;
    }
    .workspace-tool-details > summary::after {
      content: "展开";
      margin-left: 6px;
      color: var(--muted);
      font-weight: 650;
    }
    .workspace-tool-details[open] > summary::after {
      content: "收起";
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
      grid-template-columns: minmax(0, 1fr) auto auto auto auto;
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
    .skill-inventory-panel {
      margin: 12px 0;
    }
    .skill-inventory-head {
      cursor: pointer;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      list-style: none;
    }
    .skill-inventory-head::-webkit-details-marker {
      display: none;
    }
    .skill-inventory-head > span:first-child {
      display: grid;
      gap: 3px;
      min-width: 0;
    }
    #skill-inventory-summary {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .skill-inventory-simple {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
      margin: 12px 0 8px;
    }
    .skill-inventory-metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 8px 10px;
      min-width: 0;
    }
    .skill-inventory-metric strong {
      display: block;
      font-size: 18px;
      line-height: 1.1;
      overflow-wrap: anywhere;
    }
    .skill-inventory-metric span,
    .skill-inventory-note {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }
    .skill-inventory-list {
      display: grid;
      gap: 7px;
      margin-top: 10px;
    }
    .skill-inventory-row {
      display: grid;
      grid-template-columns: minmax(180px, 1.1fr) minmax(220px, 1.2fr) minmax(160px, .8fr);
      gap: 10px;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 9px 10px;
      min-width: 0;
    }
    .skill-inventory-name {
      color: var(--ink);
      font-weight: 820;
      overflow-wrap: anywhere;
    }
    .skill-inventory-meta,
    .skill-inventory-action {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .skill-tool-checks {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .skill-tool-check {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 4px 8px;
      color: var(--muted);
      font-size: 12px;
      background: var(--soft);
      white-space: nowrap;
    }
    .skill-tool-check.installed {
      border-color: #b8d8c8;
      background: #ecfdf5;
      color: #047857;
      font-weight: 760;
    }
    .skill-tool-check.pending {
      border-color: #e8d29c;
      background: #fffbeb;
      color: #a16207;
      font-weight: 760;
    }
    .local-skill-manager {
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      padding: 10px 0;
      margin: 10px 0;
      display: grid;
      gap: 8px;
    }
    .local-skill-manager-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
    }
    .local-skill-manager-title {
      font-size: 13px;
      font-weight: 820;
    }
    .local-skill-input-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: center;
    }
    .local-skill-input-row input {
      min-width: 0;
      height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 9px;
      font: inherit;
      font-size: 12px;
      background: #fff;
    }
    .local-skill-result {
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .local-skill-followup {
      display: none;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      align-items: center;
    }
    .local-skill-followup.ready {
      display: grid;
    }
    .local-skill-followup button {
      width: 100%;
    }
    .local-skill-tools {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 6px;
    }
    .local-skill-tool {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 6px 8px;
      background: #fff;
      min-width: 0;
    }
    .local-skill-tool strong,
    .local-skill-tool span {
      display: block;
      overflow-wrap: anywhere;
    }
    .local-skill-tool span {
      color: var(--muted);
      font-size: 11px;
      margin-top: 2px;
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
      .status-strip { grid-template-columns: 1fr; }
      .scope-switchboard { grid-template-columns: 1fr; }
      .scope-readonly-rail { grid-template-columns: 1fr 1fr; }
      .decision-console { grid-template-columns: 1fr; }
      .decision-boundary { grid-column: auto; }
      .scope-list { grid-template-columns: 1fr; }
      .review-item { grid-template-columns: 1fr; }
      .status-band { grid-template-columns: 1fr 1fr; }
      .status-band .panel { grid-column: 1 / -1; }
      .easy-workspace-grid { grid-template-columns: 1fr; }
      .easy-card + .easy-card {
        border-left: 0;
        border-top: 1px solid var(--line);
      }
      .workbench-grid { grid-template-columns: 1fr; }
      .plain-detail-grid { grid-template-columns: 1fr; }
      .skill-inventory-row { grid-template-columns: 1fr; }
      .local-skill-input-row { grid-template-columns: 1fr; }
      .local-skill-followup.ready { grid-template-columns: 1fr; }
      .local-skill-tools { grid-template-columns: 1fr; }
      .cards { grid-template-columns: 1fr; }
      .device-tool-grid { grid-template-columns: 1fr; }
      .device-map-grid { grid-template-columns: 1fr 1fr; }
      .skill-inventory-simple { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .guide-steps { grid-template-columns: 1fr; }
      .grid { grid-template-columns: 1fr; }
      .plan-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 560px) {
      html,
      body {
        max-width: 100%;
        overflow-x: hidden;
      }
      .portal-link { margin: 8px 14px 0; }
      header {
        flex-direction: row;
        align-items: center;
        gap: 10px;
        padding: 10px 14px;
      }
      h1 { font-size: 17px; }
      .brand-subtitle { display: none; }
      .toolbar { gap: 8px; font-size: 12px; }
      #updated { display: none; }
      .toolbar button { padding: 6px 9px; }
      main { padding: 14px; }
      .status-strip { grid-template-columns: 1fr; gap: 8px; }
      .focus-main { padding: 12px; }
      .focus-title { font-size: 18px; }
      .focus-title strong { font-size: 22px; }
      .focus-note { display: none; }
      .focus-side { padding: 8px; align-content: center; }
      .focus-side button { padding: 7px 8px; }
      .focus-side-actions { grid-template-columns: 1fr 1fr; }
      .focus-side-note { display: none; }
      .focus-metrics { display: none; }
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
      .easy-workspace-head {
        align-items: flex-start;
        flex-direction: column;
        padding: 13px 14px;
      }
      .easy-card { padding: 13px 14px; }
      .easy-action-row {
        display: grid;
        grid-template-columns: 1fr;
      }
      .easy-action-row button { width: 100%; }
      .plain-detail-grid { grid-template-columns: 1fr; gap: 8px; }
      .workspace-overview-head {
        align-items: flex-start;
        flex-direction: column;
      }
      .workspace-metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; }
      .workspace-metric { padding: 7px 6px; }
      .workspace-metric-value { font-size: 17px; }
      .workspace-metric-label { font-size: 11px; line-height: 1.2; }
      .workspace-tool-summary { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 5px; }
      .workspace-tool-summary-item { padding: 6px; }
      .workspace-tool-summary-value { font-size: 15px; }
      .workspace-tool-summary-label { font-size: 10px; }
      .workspace-flow { grid-template-columns: 1fr; gap: 6px; }
      .workspace-subtitle { font-size: 12px; line-height: 1.35; margin-bottom: 8px; }
      .workspace-actions { display: grid; grid-template-columns: 1fr; gap: 6px; margin: 6px 0 8px; }
      .workspace-actions button { padding: 7px 8px; }
      .simple-action-panel { gap: 9px; }
      .simple-action-title { font-size: 18px; }
      .simple-action-grid { grid-template-columns: 1fr; gap: 8px; }
      .simple-action-actions { display: grid; grid-template-columns: 1fr; }
      .simple-choice-grid { min-width: 0; }
      .simple-action-item { display: grid; }
      .local-skill-manager { margin: 8px 0; padding: 8px 0; }
      .local-skill-input-row { grid-template-columns: 1fr; gap: 6px; }
      .local-skill-followup.ready { grid-template-columns: 1fr; gap: 6px; }
      .local-skill-input-row input { height: 32px; }
      .local-skill-tools { grid-template-columns: 1fr; max-height: 86px; overflow: hidden; }
      .local-action-note,
      #local-workspace-boundary,
      #central-repository-boundary,
      #central-repository-kv,
      #device-map {
        display: none;
      }
      .workspace-tools {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        max-height: 78px;
        overflow: hidden;
      }
      .workspace-tool { padding: 5px 6px; }
      .workspace-tool-row { grid-template-columns: minmax(0, 1fr) auto; gap: 6px; }
      .workspace-tool-row .pill { display: none; }
      .review-queue-summary {
        font-size: 12px;
        line-height: 1.4;
        margin-bottom: 6px;
      }
      .review-list {
        gap: 4px;
      }
      .review-progress {
        grid-template-columns: 1fr;
        gap: 5px;
        margin: 6px 0;
      }
      .review-stage {
        padding: 7px 9px;
      }
      .review-stage-note {
        display: none;
      }
      .review-feedback {
        font-size: 12px;
        margin: 6px 0;
      }
      .review-item {
        grid-template-columns: minmax(0, 1fr) auto;
        gap: 6px;
        padding: 7px 8px;
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
        grid-column: auto;
        grid-template-columns: 1fr;
        align-items: center;
        justify-items: end;
        gap: 5px;
      }
      .review-controls .pill {
        display: none;
      }
      .review-controls button {
        width: auto;
        padding: 6px 8px;
      }
      .review-item > div:nth-child(2) {
        grid-column: 1 / -1;
      }
      .review-command {
        margin-top: 0;
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
      <h1>Skill 管理</h1>
      <div class="brand-subtitle">只看顶部卡片即可：它会告诉你现在要不要点按钮。下面都是可选详情。</div>
    </div>
    <div class="toolbar">
      <span id="updated">读取中</span>
      <button id="refresh" type="button" title="刷新状态">刷新</button>
    </div>
  </header>
  <main>
    <div id="error" class="error"></div>
    <section id="simple-action-panel" class="simple-action-panel panel" aria-label="现在建议"></section>
    <section id="conflict-resolution-panel" class="conflict-resolution" hidden aria-label="版本差异处理向导"></section>
    <details class="support-drawer">
      <summary>
        <span class="support-drawer-title">
          <strong>更多操作和详情</strong>
          <span>新增 skill、查看设备状态、排查问题时再打开；日常只看上方任务卡。</span>
        </span>
      </summary>
      <div class="support-drawer-body">
    <details id="easy-workspace" class="easy-workspace panel" aria-label="可选操作">
      <summary class="easy-workspace-head">
        <div class="easy-workspace-title">
          <strong>我要新增、安装或手动同步 skill</strong>
          <span>日常不用展开；只有你主动要导入本机 skill，或想手动同步时再打开。</span>
        </div>
        <span class="pill green">只操作本机</span>
      </summary>
      <div class="easy-workspace-grid">
        <div class="easy-card">
          <div class="easy-card-label">新增/安装</div>
          <h2>让某个 skill 在本机可用</h2>
          <p>粘贴一个 skill 文件夹或 SKILL.md 路径，sidecar 会判断能安装到哪些本机工具。</p>
          <div class="local-skill-manager" aria-label="导入本地 Skill">
            <div class="local-skill-manager-head">
              <div class="local-skill-manager-title">本地 skill 路径</div>
              <span id="local-skill-pill" class="pill">待分析</span>
            </div>
            <div class="local-skill-input-row">
              <input id="local-skill-path" type="text" value="" placeholder="粘贴 skill 目录或 SKILL.md 路径" />
              <button id="local-skill-analyze" type="button" onclick="analyzeLocalSkill()">一键分析</button>
            </div>
            <div id="local-skill-followup" class="local-skill-followup" hidden aria-label="分析后的下一步">
              <button id="local-skill-install" type="button" onclick="installLocalSkill()" disabled>安装到本机</button>
              <button id="local-skill-publish-check" type="button" onclick="publishLocalSkill(false)" disabled>检查同步</button>
              <button id="local-skill-publish" type="button" onclick="publishLocalSkill(true)" disabled>同步到其他设备</button>
            </div>
            <div id="local-skill-result" class="local-skill-result">等待输入一个 skill 目录或 SKILL.md 路径。</div>
            <div id="local-skill-tools" class="local-skill-tools"></div>
          </div>
        </div>
        <div class="easy-card">
          <div class="easy-card-label">同步更新</div>
          <h2>把已确认的更新同步出去</h2>
          <p>有设备更新时，这里会出现“先检查”和“同步到其他设备”。没有需要确认的事项时不用点任何按钮。</p>
          <div id="easy-sync-empty" class="easy-sync-empty">当前没有待同步更新。顶部显示“现在不用做任何事”时，可以关闭页面或继续工作。</div>
          <div id="easy-sync-actions" class="easy-action-row pending" hidden>
            <button type="button" class="primary" onclick="refreshLocalWorkspace()">刷新本机</button>
            <button id="easy-dry-run" type="button" onclick="runExecutorAction('dry_run')" disabled>先检查</button>
            <button id="easy-publish" type="button" onclick="runExecutorAction('publish')" disabled>同步到其他设备</button>
          </div>
          <ol id="easy-sync-steps" class="easy-steps" aria-label="发布流程" hidden>
            <li><strong>1</strong><span>先检查会同步哪些 skill；这一步不会写入。</span></li>
            <li><strong>2</strong><span>确认同步；发布前还会要求输入确认词。</span></li>
            <li><strong>3</strong><span>看到“现在不用做任何事”才算完成。</span></li>
          </ol>
        </div>
      </div>
    </details>
    <details class="skill-inventory-panel panel">
      <summary class="skill-inventory-head">
        <span>
          <strong>Skill 清单</strong>
          <span id="skill-inventory-summary">读取中</span>
        </span>
        <span class="pill green">按 skill 查看</span>
      </summary>
      <div class="skill-inventory-simple">
        <div class="skill-inventory-metric"><strong id="skill-inventory-total">-</strong><span>全部 skill</span></div>
        <div class="skill-inventory-metric"><strong id="skill-inventory-published">-</strong><span>已进共享仓库</span></div>
        <div class="skill-inventory-metric"><strong id="skill-inventory-unpublished">-</strong><span>本机/设备独有</span></div>
        <div class="skill-inventory-metric"><strong id="skill-inventory-project">-</strong><span>项目级</span></div>
      </div>
      <div class="skill-inventory-note">这里只展示安装矩阵；首页仍只显示下一步。安装/卸载会在当前设备客户端执行，不跨设备直接写文件。</div>
      <div id="skill-inventory-list" class="skill-inventory-list"></div>
    </details>
    <details class="quick-status-details">
      <summary>一般不用看：状态数字</summary>
      <section class="status-strip" aria-label="状态摘要">
        <div class="status-chip focus-main">
          <div class="status-chip-label">当前状态</div>
          <div class="focus-title"><strong id="strip-blocked">-</strong><span id="strip-health">读取中</span></div>
          <div id="strip-focus-note" class="focus-note">正在读取同步状态。</div>
        </div>
        <div class="status-chip focus-side">
          <div class="focus-side-actions">
            <button id="strip-scan-local" type="button" class="primary" onclick="refreshLocalWorkspace()">扫描本机</button>
            <button id="strip-dry-run" type="button" onclick="runExecutorAction('dry_run')" disabled>检查同步</button>
          </div>
          <div id="strip-action-note" class="focus-side-note">只操作 Mac 本机；共享仓库和其他设备只读。</div>
          <div class="focus-metrics" aria-label="同步范围摘要">
            <div class="focus-metric">
              <div class="status-chip-label">本机</div>
              <div id="strip-local" class="status-chip-value">-</div>
            </div>
            <div class="focus-metric">
              <div class="status-chip-label">共享仓库</div>
              <div id="strip-central" class="status-chip-value">-</div>
            </div>
            <div class="focus-metric">
              <div class="status-chip-label">设备</div>
              <div id="strip-devices" class="status-chip-value">-</div>
            </div>
          </div>
        </div>
      </section>
    </details>
    <details class="advanced-workspace">
      <summary>一般不用看：Mac / OpenClaw / 中央库明细</summary>
    <section id="plain-detail-grid" class="plain-detail-grid" aria-label="同步对象概览"></section>
    <details class="technical-workspace">
      <summary>高级：工具目录、版本号、原始队列</summary>
    <section class="workspace-overview" aria-labelledby="workspace-overview-title">
      <div class="workspace-overview-head">
        <span class="overview-title">
          <strong id="workspace-overview-title">高级明细</strong>
          <span id="workspace-overview-summary" class="overview-subtitle">读取中</span>
        </span>
        <span class="pill green">只操作本机</span>
      </div>
      <section class="workbench-grid">
        <div class="panel local-workspace-panel">
          <div class="workspace-eyebrow">可操作 · 只影响当前设备</div>
          <div class="workspace-title">
            <h2>本机操作</h2>
            <span id="local-workspace-pill" class="pill">检查中</span>
          </div>
          <div id="local-workspace-summary" class="workspace-subtitle">正在读取本机工作区。</div>
          <div class="workspace-flow" aria-label="本机操作流程">
            <div class="workspace-step"><strong>1. 扫描</strong><span>读取当前 Mac 上各工具的 skill。</span></div>
            <div class="workspace-step"><strong>2. 检查</strong><span>只看会改什么，不写共享仓库。</span></div>
            <div class="workspace-step"><strong>3. 发布</strong><span>确认无误后再写入共享仓库。</span></div>
          </div>
          <div class="workspace-actions">
            <button id="local-workspace-refresh" type="button" class="primary" onclick="refreshLocalWorkspace()">1 扫描本机</button>
            <button id="local-workspace-dry-run" type="button" onclick="runExecutorAction('dry_run')" disabled>2 检查</button>
            <button id="local-workspace-publish" type="button" onclick="runExecutorAction('publish')" disabled>3 发布共享仓库</button>
          </div>
          <div id="local-workspace-action-note" class="local-action-note">正在检查本机助手。</div>
          <details class="workspace-secondary">
            <summary>查看数量和工具目录</summary>
            <div class="workspace-metrics">
              <div class="workspace-metric">
                <div id="local-workspace-total" class="workspace-metric-value">-</div>
                <div class="workspace-metric-label">本机 skill</div>
              </div>
              <div class="workspace-metric">
                <div id="local-workspace-blocked" class="workspace-metric-value">-</div>
                <div class="workspace-metric-label">需我确认</div>
              </div>
              <div class="workspace-metric">
                <div id="local-workspace-source" class="workspace-metric-value">-</div>
                <div class="workspace-metric-label">数据来源</div>
              </div>
            </div>
            <div id="local-workspace-tool-summary" class="workspace-tool-summary"></div>
            <details class="workspace-tool-details">
              <summary>工具目录明细</summary>
              <div id="local-workspace-tools" class="workspace-tools"></div>
            </details>
          </details>
          <div id="local-workspace-boundary" class="boundary-note"></div>
        </div>
        <div class="panel">
          <div class="readonly-kicker">只读状态 · 不直接编辑</div>
          <div class="workspace-title">
            <h2>共享仓库状态</h2>
            <span id="central-repository-pill" class="pill">只读</span>
          </div>
          <div id="central-repository-summary" class="workspace-subtitle"></div>
          <div id="central-repository-kv" class="kv"></div>
          <div id="central-repository-boundary" class="boundary-note"></div>
        </div>
        <div class="panel workbench-full">
          <div class="readonly-kicker">其他设备 · 只读观察</div>
          <div class="workspace-title">
            <h2>其他设备状态</h2>
            <span class="pill">只读</span>
          </div>
          <div id="device-map-summary" class="workspace-subtitle"></div>
      <div id="device-map" class="device-map-grid"></div>
        </div>
      </section>
    </section>
    <section id="review-queue-panel" class="review-queue panel" hidden>
      <div class="panel-head">
        <div>
          <div id="review-queue-label" class="section-label">需要确认</div>
          <h2 id="review-queue-title">确认清单</h2>
        </div>
        <span id="review-queue-count" class="pill">0</span>
      </div>
      <div id="review-queue-summary" class="review-queue-summary"></div>
      <div id="review-recommendation" class="review-recommendation"></div>
      <div id="review-progress" class="review-progress" aria-label="确认处理进度"></div>
      <div id="review-feedback" class="review-feedback" hidden>
        <strong id="review-feedback-title">等待操作</strong>
        <span id="review-feedback-detail">先检查。</span>
      </div>
      <div id="review-queue" class="review-list"></div>
    </section>
    </details>
    </details>
    <details class="secondary-context">
      <summary>权限边界和执行细节</summary>
      <div class="secondary-context-body">
    <section class="scope-switchboard" aria-label="Skill 同步分区">
      <div class="scope-card local">
        <div class="scope-card-head">
          <h2>本机操作</h2>
          <span class="pill green">可操作</span>
        </div>
        <div id="scope-local-count" class="scope-card-count">-</div>
        <div id="scope-local-note" class="scope-card-note">只扫描和处理当前浏览器所在设备。</div>
        <div class="scope-card-focus">授权发现本机目录是管理本地 skill 的必要权限；这里的操作只影响当前设备，发布也必须你明确确认。</div>
        <div class="scope-card-actions">
          <button id="scope-scan" type="button" class="primary" onclick="refreshLocalWorkspace()">扫描本机</button>
          <button id="scope-dry-run" type="button" onclick="runExecutorAction('dry_run')" disabled>检查同步</button>
          <button id="scope-publish" type="button" onclick="runExecutorAction('publish')" disabled>发布共享仓库</button>
        </div>
      </div>
      <div class="scope-readonly-rail" aria-label="共享仓库和其他设备只读状态">
        <div class="scope-card readonly">
          <div class="scope-card-head">
            <h2>共享仓库</h2>
            <span class="pill">只读</span>
          </div>
          <div id="scope-central-count" class="scope-card-count">-</div>
          <div id="scope-central-note" class="scope-card-note">共享版本库，只接受你确认后的发布。</div>
        </div>
        <div class="scope-card readonly">
          <div class="scope-card-head">
            <h2>其他设备</h2>
            <span class="pill">只读</span>
          </div>
          <div id="scope-device-count" class="scope-card-count">-</div>
          <div id="scope-device-note" class="scope-card-note">OpenClaw / Windows 自己上报实测状态，Gateway 不远程改设备。</div>
        </div>
      </div>
    </section>
    <section class="decision-console">
      <div id="operator-panel" class="panel decision-status">
        <div class="section-label">当前要做</div>
        <div class="operator-title-row">
          <div id="operator-headline" class="operator-title">读取同步状态中</div>
          <div id="operator-verdict" class="operator-verdict">未知</div>
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
          <span id="action-guide-state" class="pill">未知</span>
        </div>
        <div id="action-guide-summary" class="guide-summary"></div>
        <div id="action-guide-skills" class="guide-skills"></div>
        <div id="action-guide-note" class="guide-note"></div>
        <details class="guide-details">
          <summary>高级：本机助手和执行日志</summary>
          <div id="executor-panel" class="executor-panel" hidden>
            <div class="panel-head">
              <h2>本机助手</h2>
              <span id="executor-pill" class="pill">检查中</span>
            </div>
            <div id="executor-status" class="executor-status">正在检查 Mac 本机助手。</div>
            <div class="executor-actions">
              <button id="executor-check" type="button" onclick="checkExecutor()">重新检查</button>
              <button id="executor-dry-run" type="button" onclick="runExecutorAction('dry_run')" disabled>一键检查</button>
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
            <div class="scope-line"><strong>本机</strong><span>可扫描、检查、显式发布</span></div>
            <div class="scope-line"><strong>共享仓库</strong><span>只展示共同版本</span></div>
            <div class="scope-line"><strong>设备</strong><span>只读观察各 Agent 上报状态</span></div>
          </div>
        </div>
      </details>
    </section>
      </div>
    </details>
    <details class="advanced-diagnostics">
      <summary>高级诊断：状态、设备、工具、队列明细</summary>
      <div class="advanced-body">
    <section class="status-band">
      <div id="health-card" class="panel health">
        <span class="dot"></span>
        <div>
          <div id="health" class="health-title">未知</div>
          <div id="next-action" class="health-subtitle">等待状态</div>
        </div>
      </div>
      <div class="metric">
        <div class="metric-label">需确认</div>
        <div id="blocked" class="metric-value">-</div>
      </div>
      <div class="metric">
        <div class="metric-label">允许操作</div>
        <div id="allowed" class="metric-value">-</div>
      </div>
      <div class="metric">
        <div class="metric-label">共享技能</div>
        <div id="remote-total" class="metric-value">-</div>
      </div>
      <div class="metric">
        <div class="metric-label">同步轮次</div>
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
      <span class="section-help">共享仓库对各工具的目标覆盖，不代表某台设备已安装</span>
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
        <thead><tr><th>Skill</th><th>操作</th><th>原因</th></tr></thead>
        <tbody id="hub-import-apply-body"></tbody>
      </table>
      <table id="hub-import-table" hidden>
        <thead><tr><th>Skill</th><th>判断</th><th>建议</th><th>来源</th></tr></thead>
        <tbody id="hub-import-body"></tbody>
      </table>
      <div id="hub-import-empty" class="empty">暂无外部可导入项。</div>
    </div>
    <section class="grid">
      <div class="stack">
        <div class="panel">
          <h2>原始确认队列</h2>
          <div id="blocked-empty" class="empty">暂无需要确认项。</div>
          <table id="blocked-table" hidden>
            <thead><tr><th>Skill</th><th>状态</th><th>分类</th><th>版本指纹</th><th>建议 / 下一步</th></tr></thead>
            <tbody id="blocked-body"></tbody>
          </table>
        </div>
        <div class="panel">
          <h2>同步摘要</h2>
          <div id="summary" class="kv"></div>
        </div>
      </div>
      <div class="stack">
        <div class="panel">
          <h2>同步进程</h2>
          <div id="daemon" class="kv"></div>
        </div>
        <div class="panel">
          <h2>设备本地策略</h2>
        <div id="overrides" class="kv"></div>
      </div>
        <div class="panel">
          <h2>设备摘要</h2>
          <div id="operator-devices" class="device-lines"></div>
        </div>
        <div class="panel">
          <h2>产物路径</h2>
          <div id="artifacts" class="kv"></div>
        </div>
      </div>
    </section>
      </div>
    </details>
      </div>
    </details>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const EXECUTOR_URL = "http://127.0.0.1:18765";
    let currentGuideSkills = [];
    let executorAvailable = false;
    let executorAllowPublish = false;
    let executorAllowLocalWrites = false;
    let lastDryRunSafe = false;
    let lastPublishReceipt = null;
    let executorBusy = false;
    let localWorkspaceFromExecutor = null;
    let lastLocalSkillAnalysis = null;
    let currentReviewQueueItems = [];
    let currentReviewQueueIsMobile = window.matchMedia("(max-width: 560px)").matches;
    let reviewTaskResults = {};
    let staleRefreshTimer = null;
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
      if (status.health === "yellow") return "先处理待确认事项，再决定是否发布到共享仓库。";
      if (status.health === "red") return "先修复共享仓库、设备上报或后台服务异常。";
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
      $("operator-path").textContent = "本机可操作；共享仓库只接收确认后的发布；其他设备只读。";
      $("operator-snapshot").textContent = `当前共享仓库版本：${text(operator.snapshot_id)}`;
      $("blocked").textContent = text(dashboard.blocked ?? plan.blocked ?? blockedReport.total);
      $("allowed").textContent = text(plan.allowed);
      $("remote-total").textContent = text(snapshot.total);
      $("cycles").textContent = text(daemon.cycles_run);
      $("updated").textContent = `更新于 ${new Date().toLocaleTimeString()}`;
      renderDevices(Array.isArray(dashboard.devices) ? dashboard.devices : []);
      renderPlannedDevices(Array.isArray(dashboard.planned_devices) ? dashboard.planned_devices : []);
      renderTools(Array.isArray(dashboard.tools) ? dashboard.tools : []);
      renderDeviceTools(Array.isArray(dashboard.device_tools) ? dashboard.device_tools : []);
      renderHubImport(dashboard.hub_import || {});

      const blockedItems = Array.isArray(dashboard.blocked_items) ? dashboard.blocked_items : (Array.isArray(blockedReport.items) ? blockedReport.items : []);
      renderSimpleActionPanel(dashboard, blockedItems);
      renderReviewQueue(blockedItems);
      renderLastPublishReceipt(blockedItems);
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
      const deviceCount = otherDeviceItems(map.items).length;
      const blocked = Number(dashboard.blocked || 0);
      const blockedItems = Array.isArray(dashboard.blocked_items) ? dashboard.blocked_items : [];
      const breakdown = blockedBreakdown(blockedItems);
      const conflictOnly = blocked > 0 && breakdown.conflict === blocked;
      $("strip-health").textContent = blocked > 0
        ? (breakdown.sourceChanged > 0 ? "个源端新修改" : (breakdown.conflict === blocked ? "个需要确认" : "个需要处理"))
        : "同步完成";
      $("strip-blocked").textContent = blocked > 0 ? text(blocked) : "正常";
      $("strip-local").textContent = text(local.total_skills);
      $("strip-central").textContent = text(central.total_skills);
      $("strip-devices").textContent = text(deviceCount);
      const stripDryRun = $("strip-dry-run");
      if (stripDryRun) {
        stripDryRun.textContent = conflictOnly ? "先看差异" : (breakdown.sourceChanged > 0 ? "检查最新版本" : "检查同步");
        stripDryRun.onclick = conflictOnly ? runFirstConflictPackage : (() => runExecutorAction("dry_run"));
      }
      if (conflictOnly) {
        const names = compactSkillList(blockedItems.map((item) => item.skill_id));
        $("strip-focus-note").textContent = `只剩版本差异：${names}。先看报告，不会自动覆盖。`;
      } else if (breakdown.sourceChanged > 0) {
        const names = compactSkillList(blockedItems.filter((item) => reviewIsSourceChangedItem(item)).map((item) => item.skill_id));
        $("strip-focus-note").textContent = `OpenClaw 有新修改：${names}。改完后点“检查最新版本”；如果检查期间又变化，系统会自动拒绝写入。`;
      } else {
        $("strip-focus-note").textContent = blocked > 0
          ? `还有 ${blocked} 件事要你确认。上方会给出唯一推荐按钮。`
          : "同步正常。需要导入或更新本机 skill 时，再点扫描本机。";
      }
      const actionNote = $("strip-action-note");
      if (actionNote) {
        actionNote.textContent = conflictOnly
          ? "先看报告，再决定保留哪一版。"
          : (breakdown.sourceChanged > 0
            ? "这不是发布失败；源端还在变，先别反复发布。"
            : (blocked > 0
            ? "不会自动写入共享仓库；确认后才会发布。"
            : "只操作当前 Mac，本页不会跨设备乱改。"));
      }
    }

    function blockedBreakdown(items) {
      const allItems = Array.isArray(items) ? items : [];
      const sourceChanged = allItems.filter((item) => reviewIsSourceChangedItem(item)).length;
      const publish = reviewPublishItems(allItems).filter((item) => !reviewIsSourceChangedItem(item)).length;
      const conflict = allItems.filter((item) => item.category === "conflict" || item.status_action === "conflict").length;
      const deleteReview = reviewDeleteItems(allItems).length;
      const other = Math.max(allItems.length - publish - sourceChanged - conflict - deleteReview, 0);
      return { publish, sourceChanged, conflict, deleteReview, other };
    }

    function blockedBreakdownText(breakdown) {
      const parts = [];
      if (breakdown.sourceChanged) parts.push(`源端新修改 ${breakdown.sourceChanged} 个`);
      if (breakdown.publish) parts.push(`可发布更新 ${breakdown.publish} 个`);
      if (breakdown.conflict) parts.push(`版本差异 ${breakdown.conflict} 个`);
      if (breakdown.deleteReview) parts.push(`删除确认 ${breakdown.deleteReview} 个`);
      if (breakdown.other) parts.push(`其他 ${breakdown.other} 个`);
      return parts.length ? parts.join("，") : "没有确认项";
    }

    function conciseOperatorNext(dashboard, operator, status) {
      const blocked = Number(dashboard.blocked || 0);
      const items = Array.isArray(dashboard.blocked_items) ? dashboard.blocked_items : [];
      const breakdown = blockedBreakdown(items);
      if ((dashboard.health || status.health) === "yellow" && blocked > 0 && breakdown.conflict === blocked) {
        const names = compactSkillList(items.map((item) => item.skill_id));
        return `只剩版本差异：${names}。先看只读差异报告，报告会给出推荐动作。`;
      }
      if ((dashboard.health || status.health) === "yellow" && blocked > 0 && breakdown.sourceChanged > 0) {
        const names = compactSkillList(items.filter((item) => reviewIsSourceChangedItem(item)).map((item) => item.skill_id));
        return `OpenClaw 有新修改：${names}。还在改可以先放着；改完后点检查最新版本。`;
      }
      if ((dashboard.health || status.health) === "yellow" && blocked > 0) {
        return `先处理 ${blocked} 个待确认事项；检查只预览，确认后再发布到共享仓库。`;
      }
      return operator.next_action || nextAction(status);
    }

    function conciseGuideSummary(guide) {
      const skills = Array.isArray(guide.skills) ? guide.skills : [];
      if ((guide.title || "") === "OpenClaw 还有新修改") {
        return guide.summary || "OpenClaw 有新修改；改完后检查最新版本，变化中会自动拒绝写入。";
      }
      if ((guide.state || "") === "yellow" && skills.length > 0) {
        return `重点是 ${skills.length} 个待确认 skill。先看上方推荐动作，再检查。`;
      }
      return guide.summary || "";
    }

    function renderScopeSwitchboard(dashboard) {
      const local = dashboard.local_workspace || {};
      const central = dashboard.central_repository || {};
      const map = dashboard.device_map || {};
      const items = otherDeviceItems(map.items);
      $("scope-local-count").textContent = `${text(local.total_skills)} 个本机 skill`;
      $("scope-central-count").textContent = `${text(central.total_skills)} 个共享 skill`;
      $("scope-device-count").textContent = `${text(items.length)} 台其他设备`;
      $("scope-local-note").textContent = "授权扫描本机目录；操作只影响当前设备。";
      $("scope-central-note").textContent = `共享仓库保存共同版本；当前 ${text(central.blocked)} 个变更需要你确认。`;
      $("scope-device-note").textContent = "其他设备只展示各自 Agent 上报的实测状态，Gateway 不远程改设备。";
    }

    function statusLabel(value) {
      if (value === "green") return "正常";
      if (value === "yellow") return "需处理";
      if (value === "red") return "异常";
      if (value === "not_configured") return "未接入";
      if (value === "not_connected") return "未连接";
      if (value === "unknown") return "未知";
      if (value === "local") return "本机可操作";
      if (value === "read_only") return "只读";
      if (value === "remote_read_only") return "远端只读";
      if (value === "planned") return "待接入";
      return text(value || "未知");
    }

    function scopeLabel(value) {
      if (value === "local") return "本机可操作";
      if (value === "read_only") return "只读聚合";
      if (value === "read-only") return "只读";
      if (value === "pull-only") return "只下行";
      if (value === "push-pull") return "双向同步";
      if (value === "remote_read_only") return "远端只读";
      if (value === "planned") return "待接入";
      return text(value || "未知");
    }

    function healthLabel(value) {
      if (value === "green") return "正常";
      if (value === "yellow") return "需处理";
      if (value === "red") return "异常";
      if (value === "not_configured") return "未接入";
      if (value === "not_connected") return "未连接";
      return text(value || "未知");
    }

    function modeLabel(value) {
      if (value === "dry_run") return "检查";
      if (value === "apply") return "执行";
      if (value === "publish") return "发布";
      if (value === "update_available") return "可更新";
      if (value === "already_in_hub") return "已在 Hub";
      if (value === "importable") return "可导入";
      if (value === "not_compatible") return "暂不兼容";
      return text(value || "未知");
    }

    function statusPillLabel(value) {
      if (value === "online") return "在线";
      if (value === "checking") return "检查中";
      if (value === "analyzing") return "分析中";
      if (value === "ready") return "已就绪";
      if (value === "publishing") return "发布中";
      if (value === "published") return "已发布";
      if (value === "restoring") return "恢复中";
      if (value === "restored") return "已恢复";
      if (value === "installing") return "安装中";
      if (value === "installed") return "已安装";
      if (value === "cancelled") return "已取消";
      if (value === "failed") return "失败";
      if (value === "error") return "错误";
      if (value === "dry-run") return "检查中";
      if (value === "dry-run ok") return "检查通过";
      if (value === "publish ok") return "可发布";
      if (value === "restore check") return "检查恢复";
      if (value === "conflict publish check") return "检查发布";
      if (value === "conflict package") return "生成差异";
      if (value === "needs decision") return "需选择";
      if (value === "needs review") return "需复核";
      if (value === "no changes") return "无变更";
      return modeLabel(value);
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

    function renderSimpleActionPanel(dashboard, items) {
      const panel = $("simple-action-panel");
      if (!panel) return;
      hideConflictResolutionPanel();
      const allItems = Array.isArray(items) ? items : [];
      const sourceChangedItems = reviewSourceChangedItems(allItems);
      const publishItems = reviewPublishItems(allItems);
      const regularPublishItems = publishItems.filter((item) => !reviewIsSourceChangedItem(item));
      const deleteItems = reviewDeleteItems(allItems);
      const conflictItems = allItems.filter((item) => item.category === "conflict" || item.status_action === "conflict");
      const restoreItems = allItems.filter((item) => reviewCanRestoreFromCentral(item));
      const blocked = Number(dashboard.blocked || allItems.length || 0);
      const breakdown = blockedBreakdown(allItems);
      const conflictOnly = blocked > 0 && conflictItems.length === blocked;
      const kind = blocked === 0 ? "green" : (conflictOnly ? "version-difference" : "yellow");
      panel.className = `simple-action-panel panel ${kind}`;
      if (blocked === 0) {
        panel.innerHTML = `
          <div class="simple-action-hero">
            <div class="simple-action-plain">
              <div class="simple-action-eyebrow">现在状态</div>
              <div class="simple-action-title">现在不用做任何事</div>
              <div class="simple-action-summary">当前没有需要你处理的 skill。可以直接关闭页面；要新增或安装 skill 时，再打开“更多操作和详情”。</div>
            </div>
          </div>
          <div class="simple-action-done-line"><strong>放心：</strong>没有确认前，本页不会自动改其他设备。</div>
        `;
        setExecutorButtons(executorAvailable);
        return;
      }
      const publishNames = compactSkillList(publishItems.map((item) => item.skill_id));
      const regularPublishNames = compactSkillList(regularPublishItems.map((item) => item.skill_id));
      const sourceChangedNames = compactSkillList(sourceChangedItems.map((item) => item.skill_id));
      const deleteNames = compactSkillList(deleteItems.map((item) => item.skill_id));
      const conflictNames = compactSkillList(conflictItems.map((item) => item.skill_id));
      const judgmentCount = conflictItems.length + deleteItems.length;
      let title = `现在先处理 ${blocked} 件事`;
      let summary = `不用理解下面的状态数字。先点右侧按钮；真正写入、找回或删除前都会再次确认。`;
      let primaryActions = `<button type="button" class="primary" onclick="openAdvancedDetails()">看看要处理什么<span>只打开详情，不会写入或删除。</span></button>`;
      let facts = [
        ["不会自动覆盖", "有风险时会停下来等你确认。"],
        ["先看再执行", "检查只读，发布前还要确认。"],
        ["不懂也能用", "只需要按推荐按钮走。"],
      ];
      let taskCards = "";
      if (conflictItems.length > 0) {
        const item = conflictItems[0];
        const skill = text(item.skill_id || "unknown-skill");
        const peerId = text(item.peer_id || "");
        const reviewKey = reviewItemKey(item);
        title = conflictItems.length === 1 ? `先看看哪里不一样：${skill}` : `先看看 ${conflictItems.length} 个 skill 哪里不一样`;
        summary = "两边都改过，系统已经停下来等你决定。现在只生成报告，不会改任何文件。";
        primaryActions = `
          <div class="simple-choice-grid single-choice" aria-label="处理版本差异">
            <button type="button" class="primary conflict-package-button" data-skill-id="${escapeHtml(skill)}" data-peer-id="${escapeHtml(peerId)}" data-review-key="${escapeHtml(reviewKey)}" onclick="generateConflictPackage(this)">生成报告<span>只看差异，不会改文件。</span></button>
          </div>
        `;
        facts = [
          ["现在点这个", "生成只读报告。"],
          ["不会发生", "不会自动发布、覆盖或删除。"],
          ["报告出来后", "再选择恢复共享仓库版、发布 OpenClaw 版，或手动处理。"],
        ];
        taskCards = `
          <div class="simple-action-card">
            <div class="simple-action-card-title">为什么停下来</div>
            <div class="simple-action-summary">${escapeHtml(skill)} 在共享仓库和 ${escapeHtml(text(item.peer_name || item.peer_id || "设备"))} 上都被改过。需要你确认保留哪一版。</div>
          </div>
        `;
      } else if (restoreItems.length > 0) {
        const item = restoreItems[0];
        const skill = text(item.skill_id || "unknown-skill");
        const peerId = text(item.peer_id || "");
        const reviewKey = reviewItemKey(item);
        const restoreTarget = restoreDeviceLabel(item);
        title = restoreItems.length === 1 ? `建议找回：${skill}` : `建议先找回 ${restoreItems.length} 个 skill`;
        summary = `${restoreTarget} 上少了这个 skill，共享库里还在。建议先找回来，不删除共享库里的版本。`;
        primaryActions = `
          <button type="button" class="primary central-restore-button" data-skill-id="${escapeHtml(skill)}" data-peer-id="${escapeHtml(peerId)}" data-review-key="${escapeHtml(reviewKey)}" onclick="restoreCentralSkill(this)">找回到 ${escapeHtml(restoreTarget)}<span>会先检查，再要求确认。</span></button>
        `;
        facts = [
          ["推荐动作", `把 ${skill} 找回来。`],
          ["不会发生", "不会删除共享库，也不会影响其他设备。"],
          ["需要确认", "真正找回前会再次确认。"],
        ];
        taskCards = renderSimpleDecisionList([], restoreItems);
      } else if (sourceChangedItems.length > 0) {
        title = sourceChangedItems.length === 1 ? `OpenClaw 有新修改：${sourceChangedNames}` : `OpenClaw 有 ${sourceChangedItems.length} 个 skill 在更新`;
        summary = "如果还在改，可以先不管；如果这轮已经改完，点“检查最新版本”。检查只读，不会写入；检查期间又变化会自动拒绝发布。";
        primaryActions = `
          <button id="simple-dry-run" type="button" class="primary" onclick="runExecutorAction('dry_run')" disabled>检查最新版本<span>只读，不写入。</span></button>
          <button type="button" onclick="refresh(true)">刷新状态<span>只重新读取，不写入。</span></button>
        `;
        facts = [
          ["发生了什么", "OpenClaw 本地版本不同于共享库，说明有人刚改过。"],
          ["不会误发", "发布前会重新校验 hash，变化中会拒绝写入。"],
          ["下一步", "改完就检查；还在改就继续做你的事。"],
          ["完成标准", "顶部显示“现在不用做任何事”。"],
        ];
        taskCards = `
          <div class="simple-action-card">
            <div class="simple-action-card-title">正在变化的 skill</div>
            <div class="simple-action-summary">${escapeHtml(sourceChangedNames)}</div>
          </div>
        `;
      } else if (publishItems.length > 0) {
        const readyPublishItems = regularPublishItems.filter((item) => {
          const result = reviewTaskResults[reviewItemKey(item)];
          return result && result.publishReady;
        });
        const allPublishReady = regularPublishItems.length > 0 && readyPublishItems.length === regularPublishItems.length;
        title = allPublishReady ? `检查通过，可以保存 ${regularPublishItems.length} 个更新` : `先检查 ${publishItems.length} 个更新`;
        summary = allPublishReady
          ? "现在只剩最后一步：保存到共享库。保存后页面会自动回查；如果又出现新修改，顶部会继续告诉你下一步。"
          : "先检查会改哪些 skill。这一步只看结果，不会写入。检查通过后按钮会变成“保存到共享库”。";
        primaryActions = allPublishReady
          ? `
            <button id="simple-publish" type="button" class="primary" onclick="runExecutorAction('publish')" disabled>保存到共享库<span>会要求输入 PUBLISH。</span></button>
          `
          : `
            <button id="simple-dry-run" type="button" class="primary" onclick="runExecutorAction('dry_run')" disabled>检查一下<span>只读，不写入。</span></button>
          `;
        facts = allPublishReady
          ? [
            ["下一步", "点“保存到共享库”。"],
            ["确认词", "输入 PUBLISH 后才会写入。"],
            ["完成标准", "顶部显示“现在不用做任何事”。"],
          ]
          : [
            ["要检查", `${regularPublishNames || publishNames}。`],
            ["第一步", "检查只读，不写共享库。"],
            ["完成标准", "顶部显示“现在不用做任何事”。"],
          ];
        taskCards = `
          <div class="simple-action-card">
            <div class="simple-action-card-title">本次会处理</div>
            <div class="simple-action-summary">${escapeHtml(regularPublishNames || publishNames)}</div>
          </div>
        `;
      } else if (deleteItems.length > 0) {
        title = `先看看 ${deleteItems.length} 个少掉的 skill`;
        summary = "少掉不等于要删除。默认会保留共享库，先让你决定找回，还是以后单独删除共享库版本。";
        primaryActions = `<button type="button" class="primary" onclick="openAdvancedDetails()">看看少了什么<span>只打开详情，不会删除。</span></button>`;
        facts = [
          ["缺失项", `${deleteNames}。`],
          ["默认安全", "保留共享库。"],
          ["删除保护", "删除不会一键执行。"],
        ];
        taskCards = renderSimpleDecisionList([], deleteItems);
      }
      panel.innerHTML = `
        <div class="simple-action-hero">
          <div class="simple-action-plain">
            <div class="simple-action-eyebrow">推荐下一步</div>
            <div class="simple-action-title">${escapeHtml(title)}</div>
            <div class="simple-action-summary">${escapeHtml(summary)}</div>
          </div>
        <div class="simple-action-actions single-primary">
          ${primaryActions}
        </div>
      </div>
        ${renderSimpleActionMore(facts, taskCards)}
        <div id="simple-action-feedback" class="simple-action-feedback" hidden>
          <strong id="simple-action-feedback-title">等待操作</strong>
          <span id="simple-action-feedback-detail">选择一个按钮后，这里会显示进度。</span>
        </div>
        <div id="simple-action-note" class="simple-action-note">只需要按顶部推荐按钮走；下面的数字、设备、版本信息都是排查时才看的详情。</div>
      `;
      setExecutorButtons(executorAvailable);
    }

    function renderSimpleActionMore(facts, taskCards) {
      const safeFacts = Array.isArray(facts) ? facts : [];
      if (safeFacts.length === 0 && !taskCards) return "";
      const factHtml = safeFacts.length
        ? `<div class="simple-action-facts">${safeFacts.map(([label, value]) => `<div class="simple-action-fact"><strong>${escapeHtml(label)}</strong>${escapeHtml(value)}</div>`).join("")}</div>`
        : "";
      return `
        <details class="simple-action-more">
          <summary>为什么这样建议</summary>
          <div class="simple-action-more-body">
            ${factHtml}
            ${taskCards ? `<div class="simple-action-list">${taskCards}</div>` : ""}
          </div>
        </details>
      `;
    }

    function runFirstConflictPackage() {
      const button = document.querySelector(".conflict-package-button");
      if (!button) {
        setReviewFeedback("yellow", "还没有可查看的版本差异", "状态已刷新；如果仍有版本差异，上方会出现“推荐：我不确定，先看差异”。");
        refresh(true);
        return;
      }
      button.click();
    }

    function renderSimpleDecisionList(conflictItems, deleteItems) {
      const decisions = [
        ...(Array.isArray(conflictItems) ? conflictItems : []),
        ...(Array.isArray(deleteItems) ? deleteItems : []),
      ];
      if (decisions.length === 0) return "";
      return `
        <div class="simple-decision-list">
          ${decisions.map((item) => renderSimpleDecisionCard(item)).join("")}
        </div>
      `;
    }

    function renderSimpleDecisionCard(item) {
      const skill = text(item.skill_id || "unknown-skill");
      const peer = text(item.peer_name || item.peer_id || "未知设备");
      const isDelete = reviewIsDeleteItem(item);
      const isConflict = item.category === "conflict" || item.status_action === "conflict";
      const canRestore = reviewCanRestoreFromCentral(item);
      const reviewKey = reviewItemKey(item);
      const restoreTarget = restoreDeviceLabel(item);
      const title = isDelete ? `${skill}：${restoreTarget} 缺失` : `${skill}：版本需要确认`;
      const detail = canRestore
        ? `共享仓库里有完整版本，${restoreTarget} 当前缺失。推荐直接从共享仓库恢复；这不会删除共享仓库，也不会覆盖其他设备。`
        : (isDelete
          ? `推荐先保留共享仓库，不自动删除。确认这个 skill 还要用时，从共享仓库恢复到 ${restoreTarget}；确认废弃时，再单独走删除审批。`
          : "推荐先不要覆盖。打开详情看来源设备；如果 OpenClaw 是新版本，先发布 OpenClaw 更新；如果 Mac 是正确版本，再恢复/重装 Mac 版本。");
      const primaryLabel = canRestore ? `从共享仓库恢复到 ${restoreTarget}` : (isDelete ? "保留共享仓库，稍后恢复" : "生成差异报告");
      const secondaryLabel = isDelete ? "我确认要删除" : "查看高级详情";
      const secondaryDetail = isDelete
        ? "删除共享仓库属于高风险操作，当前面板不会一键执行。"
        : "版本差异不会自动覆盖，当前面板不会猜哪边正确。";
      return `
        <div class="simple-decision-card warning">
          <div class="simple-decision-head">
            <div>
              <div class="simple-decision-title">${escapeHtml(title)}</div>
              <div class="simple-decision-source">${escapeHtml(peer)}</div>
            </div>
            ${pill(isConflict ? "需选择" : "需确认", "yellow")}
          </div>
          <div class="simple-decision-copy">${escapeHtml(detail)}</div>
          <div class="simple-decision-actions">
            ${canRestore
              ? `<button type="button" class="primary central-restore-button" data-skill-id="${escapeHtml(skill)}" data-peer-id="${escapeHtml(text(item.peer_id || ""))}" data-review-key="${escapeHtml(reviewKey)}" onclick="restoreCentralSkill(this)">${escapeHtml(primaryLabel)}</button>`
              : (isConflict
                ? `<button type="button" class="primary conflict-package-button" data-skill-id="${escapeHtml(skill)}" data-peer-id="${escapeHtml(text(item.peer_id || ""))}" data-review-key="${escapeHtml(reviewKey)}" onclick="generateConflictPackage(this)">${escapeHtml(primaryLabel)}</button>`
                : `<button type="button" class="primary" onclick="openAdvancedDetails()">${escapeHtml(primaryLabel)}</button>`)}
            <button type="button" onclick="showDecisionExplanation('${escapeHtml(skill)}', '${escapeHtml(secondaryDetail)}')">${escapeHtml(secondaryLabel)}</button>
          </div>
        </div>
      `;
    }

    function reviewCanRestoreFromCentral(item) {
      if (!item) return false;
      const peerId = text(item.peer_id || "");
      const supportedPeer = peerId === "mac" || peerId === "oc-vps" || peerId === "openclaw";
      return supportedPeer && (
        item.status_action === "local_deleted" ||
        (!item.local_hash && Boolean(item.remote_hash))
      );
    }

    function restoreDeviceLabel(item) {
      const peerId = text((item || {}).peer_id || "");
      if (peerId === "mac") return "Mac";
      if (peerId === "oc-vps" || peerId === "openclaw") return "OpenClaw";
      return text((item || {}).peer_name || "本机");
    }

    function showDecisionExplanation(skillId, detail) {
      setReviewFeedback("yellow", `${skillId} 需要人工确认`, detail);
      openAdvancedDetails();
    }

    function hideConflictResolutionPanel() {
      const panel = $("conflict-resolution-panel");
      if (!panel) return;
      panel.hidden = true;
      panel.innerHTML = "";
    }

    function renderConflictResolutionPanel(skillId, packages) {
      const panel = $("conflict-resolution-panel");
      if (!panel) return;
      const list = Array.isArray(packages) ? packages : [];
      const firstPackage = list.length > 0 ? list[0] : {};
      const packagePath = text(firstPackage.path || "");
      const localHash = shortPlainHash(firstPackage.local_hash);
      const remoteHash = shortPlainHash(firstPackage.remote_hash);
      const baseHash = shortPlainHash(firstPackage.base_hash);
      const review = firstPackage.review || {};
      const localMissing = (review.local || {}).state === "absent";
      const remoteMissing = (review.remote || {}).state === "absent";
      const summary = localMissing && !remoteMissing
        ? "OpenClaw 当前缺失这个 skill，共享仓库仍有完整版本。推荐先恢复共享仓库版到 OpenClaw；面板不会一键删除共享仓库。"
        : (!localMissing && remoteMissing
          ? "共享仓库缺失这个 skill，OpenClaw 仍有版本。确认 OpenClaw 版正确后，再发布到共享仓库。"
          : "先看下面三块摘要。下一步不是继续检查，而是判断保留 OpenClaw 版、保留共享仓库版，还是手动合并。");
      panel.hidden = false;
      panel.innerHTML = `
        <div>
          <div class="simple-action-eyebrow">只读差异报告已生成</div>
          <div class="conflict-resolution-title">${escapeHtml(skillId)} 的报告已生成</div>
          <div class="conflict-resolution-summary">${escapeHtml(summary)}</div>
        </div>
        ${renderConflictRecommendedAction(skillId, review)}
        <div class="conflict-version-grid" aria-label="版本差异摘要">
          ${renderConflictVersionCard(review.local_label || "OpenClaw 版", review.local || {}, localHash)}
          ${renderConflictVersionCard(review.remote_label || "共享仓库版", review.remote || {}, remoteHash)}
          ${renderConflictVersionCard(review.base_label || "共同基线", review.base || {}, baseHash)}
        </div>
        ${renderConflictChoiceGrid(skillId, review)}
        <details class="conflict-diagnostic">
          <summary>查看诊断路径和版本指纹</summary>
          <div>报告路径：${escapeHtml(packagePath || "未返回路径")}</div>
          <div>OpenClaw 版：${escapeHtml(localHash)} · 共享仓库版：${escapeHtml(remoteHash)} · 共同基线：${escapeHtml(baseHash)}</div>
        </details>
      `;
      panel.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    function renderConflictRecommendedAction(skillId, review) {
      const localMissing = (review.local || {}).state === "absent";
      const remoteMissing = (review.remote || {}).state === "absent";
      const escapedSkill = escapeHtml(skillId);
      if (localMissing && !remoteMissing) {
        return `
          <div class="conflict-next-action">
            <div>
              <div class="simple-action-eyebrow">推荐下一步</div>
              <div class="conflict-next-title">恢复共享仓库版到 OpenClaw</div>
              <div class="conflict-next-copy">报告判断：OpenClaw 当前缺失，共享仓库仍有完整版本。这一步执行前会再次确认，并备份 OpenClaw 现状。</div>
            </div>
            <button type="button" class="primary central-conflict-restore-button" data-skill-id="${escapedSkill}" onclick="restoreCentralVersionForConflict(this)">恢复共享仓库版到 OpenClaw</button>
          </div>
        `;
      }
      if (!localMissing && remoteMissing) {
        return `
          <div class="conflict-next-action">
            <div>
              <div class="simple-action-eyebrow">推荐下一步</div>
              <div class="conflict-next-title">发布 OpenClaw 版到共享仓库</div>
              <div class="conflict-next-copy">报告判断：共享仓库缺失，OpenClaw 仍有版本。这一步只把这个 skill 发布到共享仓库，执行前会再次确认。</div>
            </div>
            <button type="button" class="primary openclaw-conflict-publish-button" data-skill-id="${escapedSkill}" onclick="publishOpenclawVersionForConflict(this)">发布 OpenClaw 版</button>
          </div>
        `;
      }
      return `
        <div class="conflict-next-action">
          <div>
            <div class="simple-action-eyebrow">推荐下一步</div>
            <div class="conflict-next-title">先比较三块摘要，再选择版本</div>
            <div class="conflict-next-copy">OpenClaw 和共享仓库都存在版本。sidecar 不猜哪边正确；先看摘要，再选发布、恢复或手动合并。</div>
          </div>
          <button type="button" onclick="openAdvancedDetails()">查看完整队列</button>
        </div>
      `;
    }

    function renderConflictChoiceGrid(skillId, review) {
      const localMissing = (review.local || {}).state === "absent";
      const remoteMissing = (review.remote || {}).state === "absent";
      const escapedSkill = escapeHtml(skillId);
      if (localMissing && !remoteMissing) {
        return `
          <details class="conflict-alternatives">
            <summary>其他选择和风险说明</summary>
            <div class="conflict-choice-grid">
            <div class="conflict-choice">
              <strong>我确认要删除共享仓库版</strong>
              <span>这是高风险操作。当前面板不会一键删除共享仓库，避免误删共享版本。</span>
              <button type="button" onclick="showDecisionExplanation('${escapedSkill}', '删除共享仓库属于高风险操作；请先确认这个 skill 已废弃，再走单独删除审批。')">查看删除说明</button>
            </div>
            <div class="conflict-choice">
              <strong>我手动处理</strong>
              <span>需要保留部分内容时，先看诊断路径里的只读差异报告，再整理最终版本。</span>
              <button type="button" onclick="explainConflictChoice('${escapedSkill}', 'manual')">打开手动处理说明</button>
            </div>
            </div>
          </details>
        `;
      }
      if (!localMissing && remoteMissing) {
        return `
          <details class="conflict-alternatives">
            <summary>其他选择和风险说明</summary>
            <div class="conflict-choice-grid">
            <div class="conflict-choice">
              <strong>我确认共享仓库缺失是正确的</strong>
              <span>这是删除/下架决策。当前面板不会自动删除 OpenClaw 本地版本。</span>
              <button type="button" onclick="showDecisionExplanation('${escapedSkill}', '共享仓库缺失可能代表下架；确认前不要自动删除 OpenClaw 本地版本。')">查看下架说明</button>
            </div>
            <div class="conflict-choice">
              <strong>我手动处理</strong>
              <span>需要保留部分内容时，先看诊断路径里的只读差异报告，再整理最终版本。</span>
              <button type="button" onclick="explainConflictChoice('${escapedSkill}', 'manual')">打开手动处理说明</button>
            </div>
            </div>
          </details>
        `;
      }
      return `
        <details class="conflict-alternatives" open>
          <summary>选择要保留的版本</summary>
          <div class="conflict-choice-grid">
          <div class="conflict-choice">
            <strong>保留 OpenClaw 版</strong>
            <span>OpenClaw 上的是你要的最新版。会写入共享仓库；执行前会再次确认。</span>
            <button type="button" class="openclaw-conflict-publish-button" data-skill-id="${escapedSkill}" onclick="publishOpenclawVersionForConflict(this)">发布 OpenClaw 版到共享仓库</button>
          </div>
          <div class="conflict-choice">
            <strong>保留共享仓库版</strong>
            <span>共享仓库里的是正确版本。会恢复到 OpenClaw；原 OpenClaw 版本会备份，执行前会再次确认。</span>
            <button type="button" class="central-conflict-restore-button" data-skill-id="${escapedSkill}" onclick="restoreCentralVersionForConflict(this)">恢复共享仓库版到 OpenClaw</button>
          </div>
          <div class="conflict-choice">
            <strong>我手动合并</strong>
            <span>两边都有内容要保留。先看诊断路径里的只读差异报告，手动合并后再发布最终版本。</span>
            <button type="button" onclick="explainConflictChoice('${escapedSkill}', 'manual')">打开手动合并说明</button>
          </div>
          </div>
        </details>
      `;
    }

    function renderConflictVersionCard(label, summary, hash) {
      const state = text(summary.state || "unknown");
      const title = state === "absent" ? "这个版本缺失" : text(summary.title || "未读取到标题");
      const description = text(summary.description || (state === "absent" ? "没有可对比的文件。" : "未读取到描述。"));
      const files = conflictFilesText(summary);
      return `
        <article class="conflict-version-card">
          <div class="conflict-version-label">${escapeHtml(label)}</div>
          <div class="conflict-version-title">${escapeHtml(title)}</div>
          <div class="conflict-version-desc">${escapeHtml(description)}</div>
          <div class="conflict-version-files">${escapeHtml(files)}<br>版本指纹：${escapeHtml(hash)}</div>
        </article>
      `;
    }

    function conflictFilesText(summary) {
      if (!summary || summary.state === "absent") return "文件：0 个";
      const count = Number(summary.file_count || 0);
      const files = Array.isArray(summary.files) ? summary.files : [];
      if (!files.length) return `文件：${count} 个`;
      const more = summary.has_more_files ? " 等" : "";
      return `文件：${count} 个；${files.slice(0, 4).join("、")}${more}`;
    }

    function shortPlainHash(value) {
      const raw = text(value || "");
      if (!raw) return "-";
      return raw.length > 12 ? raw.slice(0, 12) : raw;
    }

    function explainConflictChoice(skillId, choice) {
      if (choice === "openclaw") {
        setReviewFeedback(
          "yellow",
          `准备保留 OpenClaw 版：${skillId}`,
          "这是发布决策。为了安全，下一步需要确认该 skill 的 OpenClaw 版本；当前按钮不会直接写共享仓库。",
        );
        openAdvancedDetails();
        return;
      }
      if (choice === "central") {
        setReviewFeedback(
          "yellow",
          `准备保留共享仓库版：${skillId}`,
          "这是恢复决策。为了安全，下一步需要确认共享仓库版本正确，再恢复到 OpenClaw；当前按钮不会直接覆盖 OpenClaw。",
        );
        return;
      }
      setReviewFeedback(
        "yellow",
        `手动合并：${skillId}`,
        "打开诊断路径里的只读差异报告，对比 OpenClaw 版和共享仓库版；合并完成后，把最终版本作为一次明确变更发布。",
      );
    }

    function confirmProtectedWrite(options) {
      const word = text(options.word || "");
      const title = text(options.title || "确认写入");
      const will = Array.isArray(options.will) ? options.will : [];
      const willNot = Array.isArray(options.willNot) ? options.willNot : [];
      const message = [
        title,
        "",
        "将会：",
        ...will.map((line) => `- ${line}`),
        "",
        "不会：",
        ...willNot.map((line) => `- ${line}`),
        "",
        `确认继续请输入 ${word}`,
        "直接取消或输入其他内容，不会写入。",
      ].join("\n");
      return window.prompt(message) === word;
    }

    async function publishOpenclawVersionForConflict(button) {
      const skillId = button.dataset.skillId || "";
      if (!skillId) return;
      if (!executorAvailable || !executorAllowPublish) {
        setReviewFeedback("yellow", "发布未开启", "发布 OpenClaw 版需要 Mac 本机助手在线，并打开发布开关。");
        return;
      }
      setExecutorButtons(false);
      setExecutorStatus("conflict publish check", `正在检查发布 OpenClaw 版 ${skillId}。`, "yellow");
      setReviewFeedback("yellow", `正在检查 OpenClaw 版：${skillId}`, "检查只读，不会写共享仓库。");
      try {
        const dryRunResponse = await fetch(`${EXECUTOR_URL}/api/openclaw-approved-push-dry-run`, {
          method: "POST",
          cache: "no-store",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ skill_ids: [skillId], allow_conflict_local_wins: true }),
        });
        const dryRunPayload = await dryRunResponse.json();
        showExecutorOutput(formatExecutorResult(dryRunPayload));
        if (!dryRunResponse.ok || !dryRunPayload.ok || !dryRunPayload.safe_to_push || Number(dryRunPayload.approved || 0) === 0) {
          throw new Error(executorErrorDetail(dryRunPayload));
        }
        setReviewFeedback("green", `检查通过：${skillId}`, "下一步需要你确认写入。确认窗口会列出会发生什么、不会发生什么。");
        if (!confirmProtectedWrite({
          word: "PUBLISH",
          title: `确认发布 OpenClaw 版：${skillId}`,
          will: [
            `把 OpenClaw 上的 ${skillId} 发布为共享仓库版本。`,
            "只处理这一个 skill。",
            "完成后自动刷新状态，确认项是否清空。",
          ],
          willNot: [
            "不会删除共享仓库里的其他 skill。",
            "不会修改 Mac 本机工具目录。",
            "不会绕过发布权限。",
          ],
        })) {
          setExecutorStatus("cancelled", "发布 OpenClaw 版已取消。", "yellow");
          setReviewFeedback("yellow", "已取消", "没有写入共享仓库，版本差异仍保留。");
          return;
        }
        setExecutorStatus("publishing", `正在发布 OpenClaw 版：${skillId}。`, "yellow");
        setReviewFeedback("yellow", `正在发布 OpenClaw 版：${skillId}`, "正在写入共享仓库；完成后会刷新 OpenClaw 状态。");
        const publishResponse = await fetch(`${EXECUTOR_URL}/api/openclaw-approved-push-publish`, {
          method: "POST",
          cache: "no-store",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ skill_ids: [skillId], confirm: "PUBLISH", allow_conflict_local_wins: true }),
        });
        const publishPayload = await publishResponse.json();
        showExecutorOutput(formatExecutorResult(publishPayload));
        if (!publishResponse.ok || !publishPayload.ok || Number(publishPayload.approved || 0) === 0) {
          throw new Error(executorErrorDetail(publishPayload));
        }
        const resolution = await waitForSkillResolution(skillId, "发布 OpenClaw 版");
        if (resolution.done) {
          setExecutorStatus("published", `${skillId} 已按 OpenClaw 版发布到共享仓库。`, "green");
          setReviewFeedback("green", `${skillId} 已保留 OpenClaw 版`, `版本差异已清空，用了 ${resolution.attempts} 次状态确认。`);
          hideConflictResolutionPanel();
        } else {
          setExecutorStatus("needs decision", `${skillId} 已写入，仍在等待状态确认。`, "yellow");
          setReviewFeedback("yellow", `${skillId} 已写入，但确认项还没清空`, resolution.detail);
        }
      } catch (err) {
        setExecutorStatus("failed", "发布 OpenClaw 版失败，请查看输出。", "red");
        setReviewFeedback("red", "发布 OpenClaw 版失败", String(err));
      } finally {
        setExecutorButtons(executorAvailable);
      }
    }

    async function restoreCentralVersionForConflict(button) {
      const skillId = button.dataset.skillId || "";
      if (!skillId) return;
      if (!executorAvailable || !executorAllowLocalWrites) {
        setReviewFeedback("yellow", "恢复未开启", "恢复需要 Mac 本机助手在线，并打开本机写入开关。");
        return;
      }
      setExecutorButtons(false);
      setExecutorStatus("restore check", `正在检查恢复共享仓库版 ${skillId}。`, "yellow");
      setReviewFeedback("yellow", `正在检查共享仓库版：${skillId}`, "检查只读，不会写共享仓库，也不会改 OpenClaw。");
      try {
        const dryRunResponse = await fetch(`${EXECUTOR_URL}/api/openclaw-central-restore-dry-run`, {
          method: "POST",
          cache: "no-store",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ skill_ids: [skillId] }),
        });
        const dryRunPayload = await dryRunResponse.json();
        showExecutorOutput(formatExecutorResult(dryRunPayload));
        if (!dryRunResponse.ok || !dryRunPayload.ok || !dryRunPayload.safe_to_restore) {
          throw new Error(executorErrorDetail(dryRunPayload));
        }
        setReviewFeedback("green", `检查通过：${skillId}`, "下一步需要你确认写入。确认窗口会列出会发生什么、不会发生什么。");
        if (!confirmProtectedWrite({
          word: "RESTORE",
          title: `确认恢复共享仓库版：${skillId}`,
          will: [
            `把共享仓库版本恢复到 OpenClaw 的 ${skillId}。`,
            "执行前保留 OpenClaw 当前目录备份。",
            "完成后自动刷新状态，确认项是否清空。",
          ],
          willNot: [
            "不会删除共享仓库版本。",
            "不会修改 Mac 本机工具目录。",
            "不会处理其他 skill。",
          ],
        })) {
          setExecutorStatus("cancelled", "恢复共享仓库版已取消。", "yellow");
          setReviewFeedback("yellow", "已取消", "没有写入 OpenClaw，也没有写入共享仓库。");
          return;
        }
        setExecutorStatus("restoring", `正在把共享仓库版恢复到 OpenClaw：${skillId}。`, "yellow");
        setReviewFeedback("yellow", `正在恢复共享仓库版：${skillId}`, "正在写入 OpenClaw skill 目录；原 OpenClaw 版本会进入备份目录。");
        const restoreResponse = await fetch(`${EXECUTOR_URL}/api/openclaw-central-restore`, {
          method: "POST",
          cache: "no-store",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ skill_ids: [skillId], confirm: "RESTORE" }),
        });
        const restorePayload = await restoreResponse.json();
        showExecutorOutput(formatExecutorResult(restorePayload));
        if (!restoreResponse.ok || !restorePayload.ok) {
          throw new Error(executorErrorDetail(restorePayload));
        }
        const resolution = await waitForSkillResolution(skillId, "恢复共享仓库版");
        if (resolution.done) {
          setExecutorStatus("restored", `${skillId} 已恢复为共享仓库版本。`, "green");
          setReviewFeedback("green", `${skillId} 已恢复为共享仓库版`, `版本差异已清空，用了 ${resolution.attempts} 次状态确认。`);
          hideConflictResolutionPanel();
        } else {
          setExecutorStatus("needs decision", `${skillId} 已恢复，仍在等待状态确认。`, "yellow");
          setReviewFeedback("yellow", `${skillId} 已恢复，但确认项还没清空`, resolution.detail);
        }
      } catch (err) {
        setExecutorStatus("failed", "恢复共享仓库版失败，请查看输出。", "red");
        setReviewFeedback("red", "恢复共享仓库版失败", String(err));
      } finally {
        setExecutorButtons(executorAvailable);
      }
    }

    async function waitForSkillResolution(skillId, actionLabel) {
      const maxAttempts = 4;
      for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
        setExecutorStatus("checking", `${actionLabel}已提交，正在确认状态 ${attempt}/${maxAttempts}。`, "yellow");
        setReviewFeedback("yellow", "正在确认是否完成", `第 ${attempt}/${maxAttempts} 次读取 OpenClaw 上报和 NAS 状态；这一步只读，不会再写入。`);
        await refreshOpenclawPeerStatus();
        await refresh(true);
        const remaining = reviewItemsForSkill(skillId);
        if (remaining.length === 0) {
          return { done: true, attempts: attempt, detail: "确认项已清空。" };
        }
        if (attempt < maxAttempts) {
          await wait(4000);
        }
      }
      const remaining = reviewItemsForSkill(skillId);
      return {
        done: false,
        attempts: maxAttempts,
        detail: remaining.length > 0
          ? `已完成写入请求，但面板仍看到 ${remaining.length} 个相关确认项：${blockedBreakdownText(blockedBreakdown(remaining))}。通常是 OpenClaw 上报还没收敛；稍后点刷新再看。`
          : "已完成写入请求，但状态刷新结果暂时不确定；稍后点刷新再看。",
      };
    }

    async function waitForSkillsResolution(skillIds, actionLabel) {
      const uniqueSkillIds = Array.from(new Set((skillIds || []).filter(Boolean)));
      const maxAttempts = 4;
      for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
        setExecutorStatus("checking", `${actionLabel}已提交，正在确认状态 ${attempt}/${maxAttempts}。`, "yellow");
        setReviewFeedback("yellow", "正在确认结果是否收敛", `第 ${attempt}/${maxAttempts} 次读取 OpenClaw 上报和 NAS 状态；这一步只读，不会再写入。`);
        await refreshOpenclawPeerStatus();
        await refresh(true);
        const remaining = reviewItemsForSkills(uniqueSkillIds);
        if (remaining.length === 0) {
          return { done: true, attempts: attempt, detail: "相关确认项已清空。" };
        }
        if (attempt < maxAttempts) {
          await wait(4000);
        }
      }
      const remaining = reviewItemsForSkills(uniqueSkillIds);
      const names = compactSkillList(remaining.map((item) => item.skill_id));
      return {
        done: false,
        attempts: maxAttempts,
        detail: remaining.length > 0
          ? `已完成写入请求，但面板仍看到 ${remaining.length} 个相关确认项：${names}。`
          : "已完成写入请求，但状态刷新结果暂时不确定。",
      };
    }

    function reviewItemsForSkill(skillId) {
      return currentReviewQueueItems.filter((item) => item.skill_id === skillId);
    }

    function reviewItemsForSkills(skillIds) {
      const ids = new Set((skillIds || []).filter(Boolean));
      return currentReviewQueueItems.filter((item) => ids.has(item.skill_id));
    }

    function wait(ms) {
      return new Promise((resolve) => setTimeout(resolve, ms));
    }

    function simpleActionStep(index, value) {
      return `
        <li class="simple-action-step">
          <span class="simple-action-index">${escapeHtml(text(index))}</span>
          <span>${escapeHtml(text(value))}</span>
        </li>
      `;
    }

    function simpleActionItem(label, value, detail) {
      return `
        <div class="simple-action-item">
          <span>${escapeHtml(label)}：${escapeHtml(value)}</span>
          <span>${escapeHtml(text(detail))}</span>
        </div>
      `;
    }

    function simpleTaskCard(title, count, detail, actionLabel, action, kind, buttonId) {
      return `
        <div class="simple-task-card">
          <div class="simple-task-head">
            <div class="simple-task-title">${escapeHtml(title)}</div>
            ${pill(`${text(count)} 个`, kind || "")}
          </div>
          <div class="simple-task-detail">${escapeHtml(text(detail))}</div>
          <div class="simple-task-actions">
            <button ${buttonId ? `id="${escapeHtml(buttonId)}"` : ""} type="button" onclick="${escapeHtml(action)}">${escapeHtml(actionLabel)}</button>
          </div>
        </div>
      `;
    }

    function openAdvancedDetails() {
      openSupportDrawer();
      const target = document.querySelector(".advanced-workspace");
      if (!target) return;
      target.open = true;
      target.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    function openTechnicalWorkspace() {
      openSupportDrawer();
      const advanced = document.querySelector(".advanced-workspace");
      const technical = document.querySelector(".technical-workspace");
      if (advanced) advanced.open = true;
      if (technical) {
        technical.open = true;
        technical.scrollIntoView({ behavior: "smooth", block: "start" });
      } else if (advanced) {
        advanced.scrollIntoView({ behavior: "smooth", block: "start" });
      }
      const input = $("local-skill-path");
      if (input) {
        window.setTimeout(() => input.focus(), 250);
      }
    }

    function openSupportDrawer() {
      const drawer = document.querySelector(".support-drawer");
      if (drawer) drawer.open = true;
    }

    function renderReviewQueue(items) {
      const panel = $("review-queue-panel");
      if (!Array.isArray(items) || items.length === 0) {
        currentReviewQueueItems = [];
        panel.hidden = true;
        return;
      }
      currentReviewQueueItems = items;
      const deleteItems = reviewDeleteItems(items);
      const sourceChangedItems = reviewSourceChangedItems(items);
      const publishItems = reviewPublishItems(items);
      const regularPublishItems = publishItems.filter((item) => !reviewIsSourceChangedItem(item));
      const conflictItems = reviewConflictItems(items);
      const conflictOnly = conflictItems.length > 0 && conflictItems.length === items.length;
      panel.hidden = false;
      $("review-queue-count").outerHTML = pill(`${items.length} 项`, "yellow").replace("<span", "<span id=\"review-queue-count\"");
      const otherItems = items.filter((item) => !reviewIsDeleteItem(item) && !reviewIsSourceChangedItem(item) && !reviewIsPublishCandidate(item));
      const mobileReview = window.matchMedia("(max-width: 560px)").matches;
      currentReviewQueueIsMobile = mobileReview;
      const scope = reviewQueueScopeInfo(items, conflictOnly);
      $("review-queue-label").textContent = scope.label;
      $("review-queue-title").textContent = scope.title;
      $("review-progress").setAttribute("aria-label", conflictOnly ? "版本差异处理进度" : "确认处理进度");
      $("review-queue-summary").textContent = conflictOnly
        ? `${scope.peerText}：只剩 ${items.length} 个版本差异。这里不是批量发布队列；先生成只读报告，再按推荐处理。`
        : `${scope.peerText}：${items.length} 个待确认。${blockedBreakdownText(blockedBreakdown(items))}。`;
      renderReviewRecommendation(items);
      renderReviewProgress(items);
      if (conflictOnly) {
        $("review-queue").innerHTML = renderReviewGroup(
          "当前版本差异",
          conflictItems,
          "点“生成只读报告”，报告会把推荐动作放在最上方。"
        );
      } else {
        $("review-queue").innerHTML = [
          renderReviewGroup(
            "先处理缺失/删除确认",
            deleteItems,
            "这些不是发布按钮要处理的内容；当前面板不会删除共享仓库。"
          ),
          renderReviewGroup(
            "检查 OpenClaw 新修改",
            sourceChangedItems,
            "这些项表示源设备又产生了新版本。还在改可以先放着；改完后点检查最新版本。"
          ),
          renderReviewGroup(
            "再处理可发布更新",
            regularPublishItems,
            "逐项检查，结果显示可以发布后再显式发布到共享仓库。"
          ),
          renderReviewGroup(
            "最后处理版本差异/未知项",
            conflictItems.length ? conflictItems : otherItems,
            "版本差异或未知项先看只读报告，不进入一键发布。"
          ),
        ].filter(Boolean).join("");
      }
      setExecutorButtons(executorAvailable);
    }

    function reviewQueueScopeInfo(items, conflictOnly) {
      const peerNames = [...new Set(items.map((item) => text(item.peer_name || item.peer_id)).filter(Boolean))];
      const peerIds = [...new Set(items.map((item) => text(item.peer_id || item.peer_name)).filter(Boolean))];
      const allOpenClaw = peerIds.length > 0 && peerIds.every((peer) => /openclaw|oc-vps/i.test(peer));
      const allMac = peerIds.length > 0 && peerIds.every((peer) => /^mac$|mac 本机/i.test(peer));
      const peerText = peerNames.join("、") || "其他设备";
      if (allOpenClaw) {
        return {
          label: conflictOnly ? "OpenClaw 版本差异" : "OpenClaw 待确认",
          title: conflictOnly ? "OpenClaw 版本确认" : "OpenClaw 待确认清单",
          peerText,
        };
      }
      if (allMac) {
        return {
          label: conflictOnly ? "本机版本差异" : "本机待确认",
          title: conflictOnly ? "本机版本确认" : "本机待确认清单",
          peerText,
        };
      }
      return {
        label: conflictOnly ? "多设备版本差异" : "多设备待确认",
        title: conflictOnly ? "多设备版本确认" : "多设备待确认清单",
        peerText,
      };
    }

    function renderReviewRecommendation(items) {
      const target = $("review-recommendation");
      if (!target) return;
      const deleteItems = reviewDeleteItems(items);
      const sourceChangedItems = reviewSourceChangedItems(items);
      const publishItems = reviewPublishItems(items);
      const conflictItems = reviewConflictItems(items);
      const checkedCount = publishItems.filter((item) => reviewTaskResults[reviewItemKey(item)]).length;
      const readyCount = publishItems.filter((item) => {
        const result = reviewTaskResults[reviewItemKey(item)];
        return result && result.publishReady;
      }).length;
      const remainingPrecheck = Math.max(publishItems.length - checkedCount, 0);
      const remainingReady = Math.max(publishItems.length - readyCount, 0);
      const deleteNames = compactSkillList(deleteItems.map((item) => item.skill_id));
      const sourceChangedNames = compactSkillList(sourceChangedItems.map((item) => item.skill_id));
      const conflictNames = compactSkillList(conflictItems.map((item) => item.skill_id));
      const sourceChangedOnly = sourceChangedItems.length > 0 && sourceChangedItems.length === publishItems.length;
      const summary = conflictItems.length > 0
        ? `有 ${conflictItems.length} 个版本差异，不能一键发布。先生成只读差异报告，报告会给出恢复、发布或手动处理的推荐。`
        : (sourceChangedItems.length > 0
          ? `有 ${sourceChangedItems.length} 个 OpenClaw skill 出现新修改：${sourceChangedNames}。如果还在改，可以先放着；如果已经改完，直接检查最新版本。`
          : (publishItems.length > 0
          ? `有 ${publishItems.length} 个设备更新需要确认。先检查，全部显示可以发布后再确认发布；缺失/删除项不会被发布按钮处理。`
          : `没有可发布更新。先处理 ${deleteItems.length} 个缺失/删除确认项；默认保留共享仓库，不静默删除。`));
      const publishActionLabel = !executorAvailable
        ? "等待本机助手"
        : (!executorAllowPublish ? "发布开关未打开" : (sourceChangedItems.length > 0 && remainingReady > 0 ? `检查后再保存 ${publishItems.length} 个更新` : `确认发布 ${publishItems.length} 个 OpenClaw 更新`));
      const publishActionNote = publishItems.length === 0
        ? "当前没有东西可发布；如果点确认发布，也不会写入共享仓库。"
        : (!executorAvailable
          ? "本机助手未在线，先启动本机助手。"
          : (!executorAllowPublish
            ? "当前只能检查，不能写入共享仓库；需要重新安装本机助手并打开发布开关。"
            : (sourceChangedItems.length > 0 && remainingReady > 0
              ? "改完后先检查最新版本；检查期间又变化会自动拒绝写入。"
              : (remainingReady > 0 ? "发布按钮会在所有更新检查通过后解锁。" : "下一步就是点“确认发布”，确认后写入共享仓库。"))));
      target.innerHTML = `
        <div class="review-recommendation-title">推荐操作</div>
        <div class="review-recommendation-summary">
          ${escapeHtml(summary)}
        </div>
        <ol class="review-recommendation-steps">
          <li class="review-recommendation-step">
            <span class="review-recommendation-index">1</span>
            <span>${conflictItems.length ? `先生成只读差异报告：${escapeHtml(conflictNames)}。` : (deleteItems.length ? `确认缺失项是恢复还是删除：${escapeHtml(deleteNames)}。` : (sourceChangedItems.length ? `改完后检查最新版本：${escapeHtml(sourceChangedNames)}。` : "当前没有缺失/删除确认。"))}</span>
          </li>
          <li class="review-recommendation-step">
            <span class="review-recommendation-index">2</span>
            <span>${sourceChangedOnly ? "点“检查”只会读取最新版本，不会写入共享库。" : (remainingPrecheck > 0 ? `还有 ${remainingPrecheck} 个更新没检查。` : (publishItems.length ? "更新已完成检查。" : "当前没有可发布更新。"))}</span>
          </li>
          <li class="review-recommendation-step">
            <span class="review-recommendation-index">3</span>
            <span>${publishItems.length === 0 ? "不要点发布；先完成版本差异/缺失决策。" : (!executorAllowPublish ? "当前发布开关未打开；检查通过后也不会自动写入。" : (remainingReady > 0 ? `发布前还差 ${remainingReady} 个检查通过。` : `可以确认发布 ${publishItems.length} 个更新到共享仓库。`))}</span>
          </li>
        </ol>
        <div class="review-recommendation-actions">
          <button id="review-dry-run-all" type="button" onclick="runExecutorAction('dry_run')" disabled>${checkedCount > 0 ? `重新检查 ${publishItems.length} 个更新` : `检查 ${publishItems.length} 个更新`}</button>
          <button id="review-publish-all" type="button" class="primary" onclick="runExecutorAction('publish')" disabled>${escapeHtml(publishActionLabel)}</button>
        </div>
        <div id="review-recommendation-note" class="review-recommendation-note">
          ${escapeHtml(publishActionNote)}
        </div>
      `;
    }

    function renderReviewGroup(title, groupItems, note) {
      if (!Array.isArray(groupItems) || groupItems.length === 0) return "";
      return `
        <section class="review-group">
          <div class="review-group-title">${escapeHtml(title)} (${groupItems.length})</div>
          <div class="review-group-note">${escapeHtml(note)}</div>
          ${groupItems.map((item) => renderReviewItem(item)).join("")}
        </section>
      `;
    }

    function renderReviewItem(item) {
      const command = item.operator_command || "";
      const reviewKey = reviewItemKey(item);
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
            <div class="review-decision">${reviewDecisionHtml(item)}</div>
            <div class="review-result">${pill(reviewResultText(item), reviewResultKind(item))}</div>
            ${command ? `
              <details class="review-command">
                <summary>查看检查命令</summary>
                <div class="command-row">
                  <pre class="guide-command mono"><code>${escapeHtml(command)}</code></pre>
                  <button type="button" class="copy-button" data-command="${escapeHtml(command)}" onclick="copyCommand(this)">复制</button>
                </div>
              </details>
            ` : ""}
          </div>
          <div class="review-controls">
            ${pill(reviewStatusText(item), "yellow")}
            <button
              type="button"
              class="review-dry-run-button"
              data-skill-id="${escapeHtml(text(item.skill_id))}"
              data-review-key="${escapeHtml(reviewKey)}"
              data-review-action="${escapeHtml(reviewControlAction(item))}"
              onclick="runExecutorActionForSkill(this.dataset.skillId, this.dataset.reviewKey)"
              disabled>${escapeHtml(reviewControlLabel(item))}</button>
          </div>
        </div>
      `;
    }

    function renderReviewProgress(items) {
      const publishableItems = Array.isArray(items) ? reviewPublishItems(items) : [];
      const publishableTotal = publishableItems.length;
      const checked = publishableItems.filter((item) => reviewTaskResults[reviewItemKey(item)]).length;
      const publishReady = publishableItems.filter((item) => {
        const result = reviewTaskResults[reviewItemKey(item)];
        return result && result.publishReady;
      }).length;
      const deleteTotal = Array.isArray(items) ? items.filter((item) => reviewIsDeleteItem(item)).length : 0;
      const conflictTotal = Array.isArray(items) ? reviewConflictItems(items).length : 0;
      const executorState = executorAvailable ? "已连接" : "未连接";
      const executorKind = executorAvailable ? "green" : "yellow";
      if (conflictTotal > 0) {
        $("review-progress").innerHTML = [
          reviewStage("1", "生成只读报告", `${conflictTotal} 个版本差异`, "yellow", "只读取 OpenClaw 和共享仓库，不写入。"),
          reviewStage("2", "按推荐处理", "报告给出建议", "yellow", "推荐动作会显示在报告最上方。"),
        reviewStage("3", "自动确认结果", "写入后回查", executorKind, executorAvailable ? "完成后自动刷新状态，确认项是否清空。" : "需要 Mac 本机助手在线。"),
        ].join("");
        return;
      }
      const dryRunKind = checked > 0 ? "green" : "yellow";
      const publishKind = publishReady > 0 ? "green" : "yellow";
      const publishNote = deleteTotal > 0
        ? `${deleteTotal} 个删除项不会自动发布；需恢复缺失设备或单独确认删除。`
        : "发布需要再次确认。";
      $("review-progress").innerHTML = [
        reviewStage("1", "连接本机助手", executorState, executorKind, executorAvailable ? "可以直接在面板检查。" : "先确认 Mac 本机助手在线。"),
        reviewStage("2", "检查可发布更新", `${checked}/${publishableTotal} 已检查`, dryRunKind, publishableTotal > 0 ? "检查只读，不会写共享仓库。" : "当前没有可发布项；不要反复点发布。"),
        reviewStage("3", conflictTotal > 0 ? "版本确认" : "确认发布", conflictTotal > 0 ? `${conflictTotal} 个需选择` : `${publishReady}/${publishableTotal} 可发布`, conflictTotal > 0 ? "yellow" : publishKind, conflictTotal > 0 ? "先生成只读差异报告，再按推荐处理。" : publishNote),
      ].join("");
    }

    function allReviewPublishCandidatesReady() {
      const publishableItems = reviewPublishItems(currentReviewQueueItems);
      if (publishableItems.length === 0) return false;
      return publishableItems.every((item) => {
        const result = reviewTaskResults[reviewItemKey(item)];
        return result && result.publishReady;
      });
    }

    function publishCandidateSkillIds() {
      const skillIds = reviewPublishItems(currentReviewQueueItems)
        .map((item) => text(item.skill_id))
        .filter(Boolean);
      return [...new Set(skillIds)];
    }

    function currentActionSkillIds() {
      const queueSkillIds = publishCandidateSkillIds();
      if (queueSkillIds.length > 0) return queueSkillIds;
      return [...new Set((currentGuideSkills || []).map((skillId) => text(skillId)).filter(Boolean))];
    }

    function renderLastPublishReceipt(items) {
      if (!lastPublishReceipt || executorBusy) return;
      const publishedSkills = Array.isArray(lastPublishReceipt.skill_ids) ? lastPublishReceipt.skill_ids : [];
      if (publishedSkills.length === 0) return;
      const allItems = Array.isArray(items) ? items : [];
      const relatedRemaining = allItems.filter((item) => publishedSkills.includes(text(item.skill_id)));
      const unrelatedRemaining = allItems.filter((item) => !publishedSkills.includes(text(item.skill_id)));
      const publishedNames = compactSkillList(publishedSkills);
      if (relatedRemaining.length === 0 && allItems.length === 0) {
        setReviewFeedback("green", "刚刚发布完成", `已发布 ${publishedNames}；当前没有确认项。`);
      } else if (relatedRemaining.length === 0) {
        setReviewFeedback(
          "yellow",
          "刚刚发布完成，还有新的确认项",
          `已发布 ${publishedNames}；剩余 ${unrelatedRemaining.length} 个是其他或新检测到的确认项：${compactSkillList(unrelatedRemaining.map((item) => item.skill_id))}。`,
        );
      } else {
        setReviewFeedback(
          "yellow",
          "发布已提交，等待状态收敛",
          `已请求发布 ${publishedNames}；仍看到 ${relatedRemaining.length} 个相关确认项：${blockedBreakdownText(blockedBreakdown(relatedRemaining))}。稍后刷新，或继续按当前队列处理。`,
        );
      }
    }

    function reviewDeleteItems(items) {
      return Array.isArray(items) ? items.filter((item) => reviewIsDeleteItem(item)) : [];
    }

    function reviewSourceChangedItems(items) {
      return Array.isArray(items) ? items.filter((item) => reviewIsSourceChangedItem(item)) : [];
    }

    function reviewPublishItems(items) {
      return Array.isArray(items) ? items.filter((item) => reviewIsPublishCandidate(item)) : [];
    }

    function reviewConflictItems(items) {
      return Array.isArray(items) ? items.filter((item) => item.category === "conflict" || item.status_action === "conflict") : [];
    }

    function compactSkillList(names) {
      const cleanNames = Array.isArray(names) ? names.map((name) => text(name)).filter(Boolean) : [];
      if (cleanNames.length === 0) return "无";
      const visible = cleanNames.slice(0, 3);
      const hidden = cleanNames.length - visible.length;
      return hidden > 0 ? `${visible.join("、")} 等 ${cleanNames.length} 个` : visible.join("、");
    }

    function reviewStage(index, title, status, kind, note) {
      return `
        <div class="review-stage">
          <div class="review-stage-title">${escapeHtml(index)}. ${escapeHtml(title)} ${pill(status, kind)}</div>
          <div class="review-stage-note">${escapeHtml(note)}</div>
        </div>
      `;
    }

    function setReviewFeedback(kind, title, detail) {
      const feedback = $("review-feedback");
      if (feedback) {
        feedback.hidden = false;
        feedback.className = `review-feedback ${kind || ""}`;
        $("review-feedback-title").textContent = title;
        $("review-feedback-detail").textContent = detail;
      }
      const simpleFeedback = $("simple-action-feedback");
      if (simpleFeedback) {
        simpleFeedback.hidden = false;
        simpleFeedback.className = `simple-action-feedback ${kind || ""}`;
        $("simple-action-feedback-title").textContent = title;
        $("simple-action-feedback-detail").textContent = detail;
      }
    }

    function updateReviewTaskResult(itemOrSkillId, result) {
      const key = typeof itemOrSkillId === "object" ? reviewItemKey(itemOrSkillId) : String(itemOrSkillId || "");
      if (!key) return;
      reviewTaskResults = { ...reviewTaskResults, [key]: result };
      renderReviewQueue(currentReviewQueueItems);
      if (window.lastDashboard) {
        renderSimpleActionPanel(window.lastDashboard, currentReviewQueueItems);
      }
    }

    function reviewResultText(item) {
      const result = reviewTaskResults[reviewItemKey(item)];
      return result ? result.label : "等待检查";
    }

    function reviewResultKind(item) {
      const result = reviewTaskResults[reviewItemKey(item)];
      return result ? result.kind : "yellow";
    }

    function rerenderReviewQueueIfViewportModeChanged() {
      const mobileReview = window.matchMedia("(max-width: 560px)").matches;
      if (mobileReview === currentReviewQueueIsMobile) return;
      renderReviewQueue(currentReviewQueueItems);
    }

    function reviewActionText(item) {
      if (reviewIsDeleteItem(item) && item.status_action === "local_deleted") return `${restoreDeviceLabel(item)} 缺失，共享仓库仍保留。`;
      if (reviewIsDeleteItem(item) && item.status_action === "remote_deleted") return "共享仓库已删除，本机仍保留。";
      if (reviewIsSourceChangedItem(item)) return "源端又产生了新版本。";
      if (item.category === "conflict") return "版本不一致，先看只读报告。";
      if (item.status_action === "local_new") return "远端新增，先检查。";
      if (item.status_action === "push_new") return "新 skill 待发布。";
      if (item.status_action === "push") return "已有 skill 待更新。";
      return item.operator_action || item.recommendation || item.reason || "查看高级诊断里的建议动作。";
    }

    function reviewSourceText(item) {
      const peer = item.peer_name || item.peer_id || "未知设备";
      return `来源 ${text(peer)}`;
    }

    function reviewCategoryText(item) {
      if (reviewIsSourceChangedItem(item)) return "源端新修改";
      if (item.category === "writer_policy") return "需要显式发布";
      if (item.category === "conflict") return "版本差异";
      if (reviewIsDeleteItem(item)) return "删除确认";
      return text(item.category || item.status_action || "需确认");
    }

    function reviewRiskText(item) {
      if (reviewIsSourceChangedItem(item)) return "变更中";
      if (item.category === "conflict") return "高风险";
      if (reviewIsDeleteItem(item)) return "高风险";
      if (item.status_action === "push_new" || item.status_action === "local_new") return "中风险";
      if (item.status_action === "push") return "低风险";
      return "需确认";
    }

    function reviewNextStepText(item) {
      if (reviewIsSourceChangedItem(item)) return "下一步：如果 OpenClaw 还在改，可以先放着；改完后检查最新版本。";
      if (item.category === "conflict") return "下一步：先生成只读差异报告，再按推荐恢复、发布或手动处理。";
      if (item.status_action === "local_deleted") return `下一步：决定是恢复到 ${restoreDeviceLabel(item)}，还是单独确认删除共享仓库里的这个 skill。`;
      if (item.status_action === "remote_deleted") return "下一步：决定是保留本机并重新发布，还是接受共享仓库删除。";
      if (reviewIsDeleteItem(item)) return "下一步：确认删除意图；当前面板不会自动删除共享仓库。";
      if (item.status_action === "push_new" || item.status_action === "local_new") return "下一步：检查内容和目标工具，确认后再发布。";
      if (item.status_action === "push") return "下一步：检查差异，通过后再发布到共享仓库。";
      return "下一步：查看检查输出和高级诊断。";
    }

    function reviewStatusText(item) {
      if (reviewIsSourceChangedItem(item)) return "源端新修改";
      if (item.status_action === "local_deleted") return `${restoreDeviceLabel(item)} 缺失`;
      if (item.status_action === "remote_deleted") return "共享仓库缺失";
      if (item.status_action === "local_new") return "新增";
      if (item.status_action === "push_new") return "新发布";
      if (item.status_action === "push") return "更新";
      if (item.category === "conflict") return "版本差异";
      return statusLabel(item.status_action || item.category || "需确认");
    }

    function reviewIsDeleteItem(item) {
      return item && (
        item.category === "delete" ||
        item.category === "delete_review" ||
        item.status_action === "local_deleted" ||
        item.status_action === "remote_deleted"
      );
    }

    function reviewIsSourceChangedItem(item) {
      return item && item.operator_state === "source_changed";
    }

    function reviewIsPublishCandidate(item) {
      return item && !reviewIsDeleteItem(item) && item.category !== "conflict" && item.status_action !== "conflict";
    }

    function reviewItemKey(item) {
      if (!item) return "";
      return [
        text(item.peer_id || item.peer_name || "unknown-peer"),
        text(item.skill_id || "unknown-skill"),
        text(item.category || "unknown-category"),
        text(item.status_action || item.plan_action || "unknown-action"),
      ].join("::");
    }

    function reviewDecisionHtml(item) {
      if (item.status_action === "local_deleted") {
        return `<strong>需要你决定</strong>如果这是误删，先从共享仓库/备份恢复到 ${escapeHtml(restoreDeviceLabel(item))}；如果确实废弃，走单独的删除审批。当前按钮不会删除共享仓库。`;
      }
      if (item.status_action === "remote_deleted") {
        return `<strong>需要你决定</strong>如果本机版本还要保留，把它作为本机变更重新发布；如果共享仓库删除是正确的，再接受删除。`;
      }
      if (item.category === "conflict") {
        return `<strong>需要确认版本</strong>不能一键发布；先查看只读差异报告，再按推荐恢复、发布或手动处理。`;
      }
      if (reviewIsSourceChangedItem(item)) {
        const result = reviewTaskResults[reviewItemKey(item)];
        if (result && result.publishReady) {
          return `<strong>已通过检查</strong>如果 OpenClaw 已停止修改，可以确认发布。`;
        }
        return `<strong>可重新检查</strong>这不是上次发布失败；OpenClaw 又产生了新版本。改完后点检查最新版本。`;
      }
      if (item.status_action === "push" || item.status_action === "push_new" || item.status_action === "local_new") {
        const result = reviewTaskResults[reviewItemKey(item)];
        if (result && result.publishReady) {
          return `<strong>已通过检查</strong>等待上方“确认发布”写入共享仓库。`;
        }
        return `<strong>可走发布流程</strong>先点检查；只有结果显示可以发布后，才会解锁显式发布。`;
      }
      return `<strong>待判断</strong>先查看高级诊断里的状态、原因和建议动作。`;
    }

    function reviewControlAction(item) {
      if (reviewIsDeleteItem(item)) return "delete-review";
      if (item.category === "conflict") return "conflict-review";
      return "检查";
    }

    function reviewControlLabel(item) {
      if (reviewIsDeleteItem(item) && reviewCanRestoreFromCentral(item)) return `找回到 ${restoreDeviceLabel(item)}`;
      if (reviewIsDeleteItem(item)) return "看处理方式";
      if (item.category === "conflict") return "看差异";
      const result = reviewTaskResults[reviewItemKey(item)];
      if (result && result.publishReady) return "重新检查";
      return "检查";
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
      const more = hidden > 0 ? `<span class="skill-more">另 ${hidden} 个，见确认清单</span>` : "";
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
      setExecutorStatus("checking", "正在检查 Mac 本机助手。", "yellow");
      checkExecutor();
    }

    async function checkExecutor() {
      setExecutorButtons(false);
      try {
        const response = await fetch(`${EXECUTOR_URL}/healthz`, { method: "GET", cache: "no-store" });
        const payload = await response.json();
        executorAvailable = response.ok && payload.ok;
        executorAllowPublish = Boolean(payload.allow_publish);
        executorAllowLocalWrites = Boolean(payload.allow_local_writes);
        if (executorAvailable) {
          setExecutorStatus(
            "online",
            executorAllowLocalWrites
              ? "Mac 本机助手在线：可以扫描、分析并安装本机 skill。"
              : "Mac 本机助手在线：可以扫描和分析本机 skill；本机写入未开启。",
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
      executorAllowLocalWrites = false;
      setExecutorStatus(
        "offline",
        "本机助手未启动。请先启动本机助手；技术命令在高级日志里查看。",
        "yellow",
      );
      setExecutorButtons(false);
    }

    function setExecutorStatus(label, detail, kind) {
      $("executor-pill").outerHTML = pill(statusPillLabel(label), kind).replace("<span", "<span id=\"executor-pill\"");
      $("executor-status").textContent = detail;
      const localNote = $("local-workspace-action-note");
      if (localNote) localNote.textContent = detail;
      if (currentReviewQueueItems.length > 0) renderReviewProgress(currentReviewQueueItems);
    }

    function setExecutorButtons(available) {
      if (currentReviewQueueItems.length > 0) {
        renderReviewRecommendation(currentReviewQueueItems);
      }
      const actionSkills = currentActionSkillIds();
      const reviewReady = allReviewPublishCandidatesReady();
      const sourceChangedCount = reviewSourceChangedItems(currentReviewQueueItems).length;
      const canPublishApprovedPush = Boolean(available && executorAllowPublish && (lastDryRunSafe || reviewReady));
      $("executor-dry-run").disabled = !available || actionSkills.length === 0;
      $("executor-publish").disabled = !canPublishApprovedPush;
      $("strip-dry-run").disabled = !available || actionSkills.length === 0;
      $("scope-dry-run").disabled = !available || actionSkills.length === 0;
      $("scope-publish").disabled = !canPublishApprovedPush;
      $("easy-dry-run").disabled = !available || actionSkills.length === 0;
      $("easy-publish").disabled = !canPublishApprovedPush;
      const easySyncActions = $("easy-sync-actions");
      const easySyncEmpty = $("easy-sync-empty");
      const easySyncSteps = $("easy-sync-steps");
      if (easySyncActions && easySyncEmpty) {
        const showSyncActions = actionSkills.length > 0;
        easySyncActions.hidden = !showSyncActions;
        easySyncActions.classList.toggle("ready", showSyncActions);
        easySyncEmpty.classList.toggle("has-work", showSyncActions);
        if (easySyncSteps) easySyncSteps.hidden = !showSyncActions;
        easySyncEmpty.textContent = showSyncActions
          ? (sourceChangedCount > 0
            ? "OpenClaw 有新修改。还在改可以先放着；改完后点检查最新版本。"
            : "检测到待确认更新。先检查，确认安全后再同步到其他设备。")
          : "当前没有待同步更新。顶部显示“现在不用做任何事”时，可以关闭页面或继续工作。";
      }
      $("local-workspace-dry-run").disabled = !available || actionSkills.length === 0;
      $("local-workspace-publish").disabled = !canPublishApprovedPush;
      const reviewDryRunAll = $("review-dry-run-all");
      const reviewPublishAll = $("review-publish-all");
      const simpleDryRun = $("simple-dry-run");
      const simplePublish = $("simple-publish");
      if (reviewDryRunAll) reviewDryRunAll.disabled = !available || actionSkills.length === 0;
      if (reviewPublishAll) {
        reviewPublishAll.disabled = !canPublishApprovedPush;
        reviewPublishAll.title = !available
          ? "本机助手未在线"
          : (!executorAllowPublish
            ? "发布开关未打开；当前只能检查，不能写入共享仓库"
            : (!reviewReady && !lastDryRunSafe
              ? "请先完成检查，确认结果显示可以发布"
              : "写入共享仓库"));
      }
      $("easy-publish").title = !available
        ? "本机助手未在线"
        : (!executorAllowPublish
          ? "发布开关未打开；当前只能检查，不能写入共享仓库"
          : (!reviewReady && !lastDryRunSafe
            ? "没有已检查通过的待发布更新"
            : "发布到共享仓库"));
      if (simpleDryRun) simpleDryRun.disabled = !available || actionSkills.length === 0;
      if (simplePublish) {
        simplePublish.textContent = !available
          ? "等待本机助手"
          : (!executorAllowPublish ? "保存开关未打开" : (sourceChangedCount > 0 && !reviewReady && !lastDryRunSafe ? "先检查最新版本" : "保存到共享库"));
        simplePublish.disabled = !canPublishApprovedPush;
        simplePublish.title = !available
          ? "本机助手未在线"
          : (!executorAllowPublish
            ? "保存开关未打开"
            : (sourceChangedCount > 0 && !reviewReady && !lastDryRunSafe
              ? "改完后先检查最新版本；检查期间又变化会自动拒绝写入"
              : (!reviewReady && !lastDryRunSafe
              ? "先检查，确认结果显示可以发布"
              : "保存到共享库")));
      }
      const localSkillAnalyze = $("local-skill-analyze");
      const localSkillInstall = $("local-skill-install");
      const localSkillPublishCheck = $("local-skill-publish-check");
      const localSkillPublish = $("local-skill-publish");
      const localSkillFollowup = $("local-skill-followup");
      if (localSkillFollowup) {
        const showFollowup = Boolean(lastLocalSkillAnalysis);
        localSkillFollowup.hidden = !showFollowup;
        localSkillFollowup.classList.toggle("ready", showFollowup);
      }
      if (localSkillAnalyze) localSkillAnalyze.disabled = !available;
      if (localSkillInstall) {
        const willWrite = Number(((lastLocalSkillAnalysis || {}).summary || {}).will_write || 0);
        localSkillInstall.disabled = !available || !executorAllowLocalWrites || !lastLocalSkillAnalysis || willWrite === 0;
      }
      if (localSkillPublishCheck) localSkillPublishCheck.disabled = !available || !lastLocalSkillAnalysis;
      if (localSkillPublish) {
        localSkillPublish.disabled = !available || !executorAllowPublish || !lastLocalSkillAnalysis;
        localSkillPublish.title = !available
          ? "本机助手未在线"
          : (!lastLocalSkillAnalysis
            ? "请先分析一个本地 skill"
            : (!executorAllowPublish
              ? "发布开关未打开；当前只能检查"
              : "发布到共享仓库"));
      }
      document.querySelectorAll(".review-dry-run-button").forEach((button) => {
        button.disabled = !available || !button.dataset.skillId;
      });
      document.querySelectorAll(".central-restore-button").forEach((button) => {
        button.disabled = !available || !executorAllowLocalWrites || !button.dataset.skillId;
        button.title = !available
          ? "本机助手未在线"
          : (!executorAllowLocalWrites
            ? "恢复需要打开本机写入开关"
            : "从共享仓库恢复到缺失设备");
      });
      document.querySelectorAll(".conflict-package-button").forEach((button) => {
        const endpoint = conflictPackageEndpoint(button.dataset.peerId || "");
        button.disabled = !available || !endpoint || !button.dataset.skillId;
        button.title = !available
          ? "本机助手未在线"
          : (!endpoint
            ? "这个设备还没有接入差异报告生成"
            : "生成只读差异报告，不写共享仓库或设备 skill 目录");
      });
      document.querySelectorAll(".central-conflict-restore-button").forEach((button) => {
        button.disabled = !available || !executorAllowLocalWrites || !button.dataset.skillId;
        button.title = !available
          ? "本机助手未在线"
          : (!executorAllowLocalWrites
            ? "恢复需要打开本机写入开关"
            : "先检查，再确认，把共享仓库版本恢复到 OpenClaw");
      });
      document.querySelectorAll(".openclaw-conflict-publish-button").forEach((button) => {
        button.disabled = !available || !executorAllowPublish || !button.dataset.skillId;
        button.title = !available
          ? "本机助手未在线"
          : (!executorAllowPublish
            ? "发布需要打开发布开关"
            : "先检查，再确认，把 OpenClaw 版本发布到共享仓库");
      });
      if (currentReviewQueueItems.length > 0) renderReviewProgress(currentReviewQueueItems);
    }

    async function runExecutorAction(mode) {
      const actionSkills = currentActionSkillIds();
      const requestedSkillsLabel = compactSkillList(actionSkills);
      if (!executorAvailable) {
        showExecutorOutput("本机助手未连接，无法执行检查或发布。按钮没有真正执行，请先确认本机助手在线。");
        setReviewFeedback("yellow", "本机助手未连接", "请先让 Mac 本机助手在线；状态已重新刷新。");
        await refresh(true);
        checkExecutor();
        return;
      }
      if (actionSkills.length === 0) {
        showExecutorOutput("当前没有可发布更新。版本差异和删除确认不会通过这个按钮自动处理。");
        setReviewFeedback("yellow", "当前没有可发布更新", "状态已重新刷新；如果只剩版本差异，请点击上方推荐按钮先看差异。");
        await refresh(true);
        return;
      }
      const isPublish = mode === "publish";
      if (isPublish) {
        if (!executorAllowPublish) {
          showExecutorOutput("当前没有打开发布开关，所以只能检查，不能写入共享仓库。");
          setReviewFeedback("yellow", "发布开关未打开", "当前只能检查，不能写入共享仓库。打开发布开关后，按钮会变成可发布。");
          setExecutorButtons(executorAvailable);
          return;
        }
        if (!lastDryRunSafe && !allReviewPublishCandidatesReady()) {
          showExecutorOutput("请先运行检查，并确认结果显示可以发布。");
          setReviewFeedback("yellow", "还不能发布", "请先运行检查，确认结果显示可以发布后再写入共享仓库。");
          return;
        }
        const typed = window.prompt("发布会写入共享仓库。请输入 PUBLISH 确认：");
        if (typed !== "PUBLISH") {
          showExecutorOutput("已取消发布。");
          setReviewFeedback("yellow", "发布已取消", "没有写入共享仓库，待确认项仍保留。");
          return;
        }
      }
      executorBusy = true;
      setExecutorButtons(false);
      setExecutorStatus(isPublish ? "publishing" : "dry-run", isPublish ? "正在发布，请不要关闭页面。" : "正在运行检查，请稍等。", "yellow");
      setReviewFeedback("yellow", isPublish ? "正在发布" : "正在检查", isPublish ? "正在写入共享仓库，请等待完成。" : "检查只读，不会写入共享仓库。");
      try {
        const endpoint = isPublish ? "/api/openclaw-approved-push-publish" : "/api/openclaw-approved-push-dry-run";
        const response = await fetch(`${EXECUTOR_URL}${endpoint}`, {
          method: "POST",
          cache: "no-store",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            skill_ids: actionSkills,
            confirm: isPublish ? "PUBLISH" : undefined,
          }),
        });
        const payload = await response.json();
        lastDryRunSafe = !isPublish && Boolean(payload.ok && payload.safe_to_push);
        showExecutorOutput(formatExecutorResult(payload));
        if (payload.ok) {
          if (isPublish && Number(payload.approved || 0) === 0) {
            lastDryRunSafe = false;
            await refreshOpenclawPeerStatus("正在刷新 OpenClaw 状态", "发布已被拒绝；这里只重新读取 OpenClaw 最新队列。");
            await refresh(true);
            setExecutorStatus("no changes", "没有发布任何 skill；队列已变化或当前项已不再是可发布更新。", "yellow");
            setReviewFeedback(
              "yellow",
              "没有写入共享仓库",
              "确认发布返回 approved=0。通常表示检查后状态变了：该项已发布、已恢复，或变成需要确认的版本差异。请看当前确认分类。",
            );
            return;
          }
          setExecutorStatus(isPublish ? "published" : "检查通过", isPublish ? "发布已写入，正在确认状态。" : "检查通过：可以继续确认发布。", "green");
          setReviewFeedback(
            "green",
            isPublish ? "发布完成" : "检查通过",
            isPublish ? "共享库已更新；正在重新读取 OpenClaw 和 NAS 状态。" : "检查通过，可以继续保存到共享库。",
          );
          if (!isPublish) {
            actionSkills.forEach((skillId) => {
              currentReviewQueueItems
                .filter((item) => item.skill_id === skillId && reviewIsPublishCandidate(item))
                .forEach((item) => {
                  reviewTaskResults[reviewItemKey(item)] = { label: "检查通过", kind: "green", publishReady: true };
                });
            });
            renderReviewQueue(currentReviewQueueItems);
          }
          if (isPublish) {
            lastPublishReceipt = {
              skill_ids: actionSkills,
              approved: payload.approved,
              approved_skill_ids: payload.approved_skill_ids,
              published_at: new Date().toISOString(),
            };
            const resolution = await waitForSkillsResolution(actionSkills, "确认发布");
            lastDryRunSafe = false;
            reviewTaskResults = {};
            const remaining = currentReviewQueueItems.length;
            const relatedRemaining = reviewItemsForSkills(actionSkills);
            const unrelatedRemaining = currentReviewQueueItems.filter((item) => !actionSkills.includes(text(item.skill_id)));
            const remainingNames = compactSkillList(currentReviewQueueItems.map((item) => item.skill_id));
            const relatedNames = compactSkillList(relatedRemaining.map((item) => item.skill_id));
            const unrelatedNames = compactSkillList(unrelatedRemaining.map((item) => item.skill_id));
            const publishedCleanly = resolution.done && relatedRemaining.length === 0;
            const detail = publishedCleanly && remaining === 0
              ? `已发布 ${requestedSkillsLabel}，当前没有确认项。`
              : (publishedCleanly
                ? `已发布 ${requestedSkillsLabel}；剩余 ${remaining} 个是其他或新检测到的确认项：${unrelatedNames}。这不是同一批发布失败。`
                : `${resolution.detail} 当前剩余 ${remaining} 个：${remainingNames}；其中相关确认项：${relatedNames}。`);
            setReviewFeedback(
              publishedCleanly && remaining === 0 ? "green" : "yellow",
              publishedCleanly ? "本次发布已完成" : "发布已提交，仍需确认状态",
              detail,
            );
            setExecutorStatus(
              publishedCleanly && remaining === 0 ? "done" : "published",
              publishedCleanly
                ? (remaining === 0 ? "本次发布已完成，当前没有确认项。" : `本次发布已完成；还有 ${remaining} 个其他确认项。`)
                : `发布已写入；仍看到 ${relatedRemaining.length} 个相关确认项。`,
              publishedCleanly && remaining === 0 ? "green" : "yellow",
            );
          }
        } else {
          if (executorPayloadIsStaleSourceChange(payload)) {
            lastDryRunSafe = false;
            await refreshOpenclawPeerStatus();
            await refresh(true);
            const detail = staleSourceChangeDetail(payload);
            setExecutorStatus("needs review", detail, "yellow");
            setReviewFeedback("yellow", "OpenClaw 仍在修改，已拒绝发布", detail);
            return;
          }
          setExecutorStatus("failed", payload.error || "执行失败，请查看输出。", "red");
          setReviewFeedback("red", "执行失败", executorErrorDetail(payload));
        }
      } catch (err) {
        showExecutorOutput(String(err));
        setExecutorStatus("failed", "本机助手调用失败，请确认本机服务仍在线。", "red");
        setReviewFeedback("red", "本机助手调用失败", "请确认 Mac 本机助手仍在线。");
      } finally {
        executorBusy = false;
        setExecutorButtons(executorAvailable);
      }
    }

    async function refreshOpenclawPeerStatus(
      title = "发布完成，正在刷新状态",
      detail = "正在刷新 OpenClaw 状态，让 NAS 面板看到最新队列。",
    ) {
      setReviewFeedback("yellow", title, detail);
      try {
        const response = await fetch(`${EXECUTOR_URL}/api/openclaw-peer-status-refresh`, {
          method: "POST",
          cache: "no-store",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) {
          setReviewFeedback("yellow", "发布已完成，状态刷新失败", executorErrorDetail(payload));
          showExecutorOutput(formatExecutorResult(payload));
          return false;
        }
        setReviewFeedback("green", "状态已刷新", "OpenClaw peer status 已重新发布；NAS 缓存刷新后确认项会下降。");
        return true;
      } catch (err) {
        setReviewFeedback("yellow", "发布已完成，状态刷新失败", String(err));
        return false;
      }
    }

    async function restoreCentralSkill(button) {
      const skillId = button.dataset.skillId || "";
      const peerId = button.dataset.peerId || "";
      const reviewKey = button.dataset.reviewKey || "";
      const endpointBase = centralRestoreEndpointBase(peerId);
      if (!endpointBase) {
        setReviewFeedback("yellow", `${skillId} 暂不能在此面板恢复`, "这个设备还没有接入本机恢复执行器。");
        return;
      }
      if (!executorAvailable || !executorAllowLocalWrites) {
        setReviewFeedback("yellow", "还不能找回", "本机助手在线后，还需要打开“允许写入本机/设备”的开关；未打开前只会检查，不会写入。");
        return;
      }
      setExecutorButtons(false);
      setReviewFeedback("yellow", `正在检查恢复 ${skillId}`, "检查只读；先确认共享仓库里有可恢复版本。");
      setExecutorStatus("restore check", `正在检查从共享仓库恢复 ${skillId}。`, "yellow");
      try {
        const dryRunResponse = await fetch(`${EXECUTOR_URL}${endpointBase}-dry-run`, {
          method: "POST",
          cache: "no-store",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ skill_ids: [skillId] }),
        });
        const dryRunPayload = await dryRunResponse.json();
        showExecutorOutput(formatExecutorResult(dryRunPayload));
        if (!dryRunResponse.ok || !dryRunPayload.ok || !dryRunPayload.safe_to_restore) {
          throw new Error(executorErrorDetail(dryRunPayload));
        }
        updateReviewTaskResult(reviewKey || skillId, { label: "可恢复", kind: "green", publishReady: false });
        const typed = window.prompt(`将从共享仓库恢复 ${skillId}。请输入 RESTORE 确认：`);
        if (typed !== "RESTORE") {
          setReviewFeedback("yellow", "恢复已取消", "没有写入设备目录，共享仓库也没有变化。");
          setExecutorStatus("cancelled", "恢复已取消。", "yellow");
          return;
        }
        setReviewFeedback("yellow", `正在恢复 ${skillId}`, "正在写入缺失设备目录，并保留备份记录。");
        setExecutorStatus("restoring", `正在从共享仓库恢复 ${skillId}。`, "yellow");
        const restoreResponse = await fetch(`${EXECUTOR_URL}${endpointBase}`, {
          method: "POST",
          cache: "no-store",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ skill_ids: [skillId], confirm: "RESTORE" }),
        });
        const restorePayload = await restoreResponse.json();
        showExecutorOutput(formatExecutorResult(restorePayload));
        if (!restoreResponse.ok || !restorePayload.ok) {
          throw new Error(executorErrorDetail(restorePayload));
        }
        await refresh(true);
        const stillBlocked = currentReviewQueueItems.some((item) => item.skill_id === skillId && reviewItemKey(item) === reviewKey);
        setReviewFeedback(
          stillBlocked ? "yellow" : "green",
          stillBlocked ? `${skillId} 已恢复，等待状态收敛` : `${skillId} 已恢复`,
          stillBlocked ? "设备状态可能还在刷新中，稍后再刷新一次。" : "确认项已刷新；如果数字下降，说明闭环完成。",
        );
        setExecutorStatus("restored", `${skillId} 已从共享仓库恢复。`, "green");
      } catch (err) {
        setReviewFeedback("red", "恢复失败", String(err));
        setExecutorStatus("failed", "恢复失败，请查看执行输出。", "red");
      } finally {
        setExecutorButtons(executorAvailable);
      }
    }

    async function generateConflictPackage(button) {
      const skillId = button.dataset.skillId || "";
      const peerId = button.dataset.peerId || "";
      const reviewKey = button.dataset.reviewKey || "";
      const endpoint = conflictPackageEndpoint(peerId);
      if (!endpoint) {
        setReviewFeedback("yellow", `${skillId} 暂不能生成差异报告`, "这个设备还没有接入差异报告执行器。");
        return;
      }
      if (!executorAvailable) {
        setReviewFeedback("yellow", "本机助手未连接", "请先让 Mac 本机助手在线。");
        return;
      }
      setExecutorButtons(false);
      setExecutorStatus("conflict package", `正在生成 ${skillId} 的差异报告。`, "yellow");
      setReviewFeedback("yellow", `正在生成 ${skillId} 差异报告`, "这是只读诊断，不会写共享仓库，也不会改设备 skill 目录。");
      try {
        const response = await fetch(`${EXECUTOR_URL}${endpoint}`, {
          method: "POST",
          cache: "no-store",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ skill_ids: [skillId] }),
        });
        const payload = await response.json();
        showExecutorOutput(formatExecutorResult(payload));
        if (!response.ok || !payload.ok) throw new Error(executorErrorDetail(payload));
        const packages = Array.isArray(payload.packages) ? payload.packages : [];
        const packagePath = packages.length > 0 ? text(packages[0].path) : text((payload.result || {}).out || payload.out);
        updateReviewTaskResult(reviewKey || skillId, { label: "差异报告已生成", kind: "yellow", publishReady: false });
        setExecutorStatus("needs decision", `${skillId} 差异报告已生成。`, "yellow");
        renderConflictResolutionPanel(skillId, packages);
        setReviewFeedback(
          "yellow",
          `${skillId} 差异报告已生成`,
          packagePath
            ? "下一步在上方选择：保留 OpenClaw 版、保留共享仓库版，或手动合并。路径已折叠在诊断里。"
            : "下一步在上方选择：保留 OpenClaw 版、保留共享仓库版，或手动合并。",
        );
      } catch (err) {
        setExecutorStatus("failed", "差异报告生成失败，请查看输出。", "red");
        setReviewFeedback("red", "差异报告生成失败", String(err));
      } finally {
        setExecutorButtons(executorAvailable);
      }
    }

    function conflictPackageEndpoint(peerId) {
      if (peerId === "oc-vps" || peerId === "openclaw") return "/api/openclaw-conflict-package";
      return "";
    }

    function centralRestoreEndpointBase(peerId) {
      if (peerId === "mac") return "/api/mac-central-restore";
      if (peerId === "oc-vps" || peerId === "openclaw") return "/api/openclaw-central-restore";
      return "";
    }

    function executorErrorDetail(payload) {
      if (!payload) return "请查看下方执行输出。";
      if (executorPayloadIsStaleSourceChange(payload)) return staleSourceChangeDetail(payload);
      if (payload.error) return text(payload.error);
      const stderr = text(payload.stderr_tail || "").trim();
      if (stderr) return stderr.split("\n").slice(-2).join(" / ");
      const stdout = text(payload.stdout_tail || "").trim();
      if (stdout) return stdout.split("\n").slice(-2).join(" / ");
      return "请查看下方执行输出。";
    }

    function executorPayloadIsStaleSourceChange(payload) {
      if (!payload) return false;
      const detail = [
        payload.error,
        payload.stderr_tail,
        payload.stdout_tail,
      ].map((value) => text(value || "")).join("\n");
      return detail.includes("skill changed since blocked report was generated")
        || detail.includes("stale_or_non_publish_skills_skipped=true");
    }

    function staleSourceChangeDetail(payload) {
      const detail = [
        payload && payload.error,
        payload && payload.stderr_tail,
        payload && payload.stdout_tail,
      ].map((value) => text(value || "")).join("\n");
      const match = detail.match(/skill changed since blocked report was generated: ([^\s]+) \(([^)]+)\)/);
      const skill = match ? match[1] : "这个 skill";
      return `${skill} 在检查/发布期间又发生变化，sidecar 已保护性拒绝写入共享仓库。等 OpenClaw 这轮修改结束后，刷新状态并重新检查即可。`;
    }

    async function runExecutorActionForSkill(skillId, reviewKey) {
      if (!executorAvailable || !skillId) return;
      const reviewItem = currentReviewQueueItems.find((item) => reviewItemKey(item) === reviewKey)
        || currentReviewQueueItems.find((item) => item.skill_id === skillId);
      if (reviewIsDeleteItem(reviewItem)) {
        const restoreTarget = restoreDeviceLabel(reviewItem);
        if (reviewCanRestoreFromCentral(reviewItem)) {
          updateReviewTaskResult(reviewItem, { label: "准备找回", kind: "yellow", publishReady: false });
          await restoreCentralSkill({
            dataset: {
              skillId,
              peerId: text(reviewItem.peer_id || ""),
              reviewKey: reviewItemKey(reviewItem),
            },
          });
          return;
        }
        updateReviewTaskResult(reviewItem, { label: "删除待决策", kind: "yellow", publishReady: false });
        setReviewFeedback(
          "yellow",
          `${skillId} 是删除确认项`,
          reviewItem.status_action === "local_deleted"
            ? `${restoreTarget} 缺失但共享仓库仍保留。下一步不是发布；请决定恢复到 ${restoreTarget}，或单独确认删除共享仓库。`
            : "共享仓库已删除但本机仍保留。请决定重新发布本机版本，或接受共享仓库删除。",
        );
        showExecutorOutput(
          [
            `skill=${skillId}`,
            `status_action=${text(reviewItem.status_action)}`,
            `category=${text(reviewItem.category)}`,
            "safe_to_push=false",
            "next_action=restore_local_or_confirm_delete",
            "",
            "说明：删除类待审不会走发布按钮。sidecar 当前不会通过这个按钮删除共享仓库。",
          ].join("\n"),
        );
        return;
      }
      if (reviewItem && reviewItem.category === "conflict") {
        updateReviewTaskResult(reviewItem, { label: "版本待确认", kind: "yellow", publishReady: false });
        setReviewFeedback("yellow", `${skillId} 是版本差异项`, "不能一键发布；先查看只读差异报告，再按推荐恢复、发布或手动处理。");
        return;
      }
      setExecutorButtons(false);
      setExecutorStatus("dry-run", `正在检查 ${skillId}，请稍等。`, "yellow");
      setReviewFeedback("yellow", `正在检查 ${skillId}`, "检查只读，不会写入共享仓库。");
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
          setExecutorStatus("检查通过", `${skillId} 检查通过：可以发布。`, "green");
          updateReviewTaskResult(reviewItem || skillId, { label: "检查通过", kind: "green", publishReady: true });
          setReviewFeedback("green", `${skillId} 检查通过`, "可以继续保存到共享库。");
        } else if (payload.ok) {
          setExecutorStatus("needs review", `${skillId} 检查完成，但还不能发布，请看输出。`, "yellow");
          updateReviewTaskResult(reviewItem || skillId, { label: "需复核", kind: "yellow", publishReady: false });
          setReviewFeedback("yellow", `${skillId} 需要复核`, "检查完成但还不能发布，请查看执行输出。");
        } else {
          if (executorPayloadIsStaleSourceChange(payload)) {
            const detail = staleSourceChangeDetail(payload);
            setExecutorStatus("needs review", detail, "yellow");
            updateReviewTaskResult(reviewItem || skillId, { label: "源端仍在修改", kind: "yellow", publishReady: false });
            setReviewFeedback("yellow", `${skillId} 仍在修改，已拒绝发布`, detail);
            await refreshOpenclawPeerStatus("正在刷新 OpenClaw 状态", "发布已被拒绝；这里只重新读取 OpenClaw 最新队列。");
            await refresh(true);
            return;
          }
          setExecutorStatus("failed", payload.error || `${skillId} 检查失败，请查看输出。`, "red");
          updateReviewTaskResult(reviewItem || skillId, { label: "检查失败", kind: "red", publishReady: false });
          setReviewFeedback("red", `${skillId} 检查失败`, payload.error || "请查看执行输出。");
        }
      } catch (err) {
        showExecutorOutput(String(err));
        setExecutorStatus("failed", "本机助手调用失败，请确认本机服务仍在线。", "red");
        updateReviewTaskResult(reviewItem || skillId, { label: "调用失败", kind: "red", publishReady: false });
        setReviewFeedback("red", "本机助手调用失败", "请确认 Mac 本机助手仍在线。");
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
        `safe_to_restore=${text(payload.safe_to_restore)}`,
        `approved=${text(payload.approved)}`,
        `restored=${text(payload.restored)}`,
        `conflicts=${text(payload.total_conflicts)}`,
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
      renderPlainDetails(dashboard);
      renderLocalWorkspace(dashboard.local_workspace || {});
      renderCentralRepository(dashboard.central_repository || {});
      renderDeviceMap(dashboard.device_map || {});
      renderSkillInventory(dashboard.skill_inventory || {});
      renderWorkspaceOverviewSummary(dashboard);
      if (!executorAvailable) {
        checkExecutor();
      }
    }

    function renderWorkspaceOverviewSummary(dashboard) {
      const local = dashboard.local_workspace || {};
      const central = dashboard.central_repository || {};
      const map = dashboard.device_map || {};
      const deviceCount = otherDeviceItems(map.items).length;
      $("workspace-overview-summary").textContent = `这里只是高级明细；日常操作请回到页面顶部两个入口。共享仓库和 ${text(deviceCount)} 台其他设备只读展示。`;
    }

    function renderPlainDetails(dashboard) {
      const local = dashboard.local_workspace || {};
      const central = dashboard.central_repository || {};
      const deviceMap = dashboard.device_map || {};
      const devices = Array.isArray(deviceMap.items) ? deviceMap.items : [];
      const openclaw = devices.find((device) => device.id === "oc-vps" || device.id === "openclaw") || {};
      const blockedItems = Array.isArray(dashboard.blocked_items) ? dashboard.blocked_items : [];
      const conflictItems = blockedItems.filter((item) => item.category === "conflict" || item.status_action === "conflict");
      const openclawAction = conflictItems.length > 0
        ? `有 ${conflictItems.length} 个版本差异，回到上方任务卡处理。`
        : (Number(openclaw.blocked || 0) > 0 ? "有确认项，回到上方任务卡处理。" : "不用操作。");
      const cards = [
        {
          title: "Mac 本机",
          state: local.health || "green",
          line: "本机可以扫描、安装和显式发布 skill。",
          action: Number(local.blocked || 0) > 0 ? "有需要你确认的本机事项。" : "当前不用处理本机。",
        },
        {
          title: "共享仓库",
          state: central.health || "green",
          line: `共享仓库收录 ${text(central.total_skills)} 个 skill。`,
          action: "不要直接编辑；只接受确认后的发布。",
        },
        {
          title: "OpenClaw",
          state: openclaw.health || "unknown",
          line: openclaw.freshness && openclaw.freshness.label
            ? `状态 ${text(openclaw.freshness.label)} 更新。`
            : "等待 OpenClaw Agent 上报。",
          action: openclawAction,
        },
      ];
      $("plain-detail-grid").innerHTML = cards.map((card) => `
        <article class="plain-detail-card">
          <div class="plain-detail-head">
            <div class="plain-detail-title">${escapeHtml(card.title)}</div>
            ${pill(plainHealthLabel(card.state), deviceKind(card.state))}
          </div>
          <div class="plain-detail-line">${escapeHtml(card.line)}</div>
          <div class="plain-detail-action">${escapeHtml(card.action)}</div>
        </article>
      `).join("");
    }

    function plainHealthLabel(value) {
      if (value === "green") return "正常";
      if (value === "yellow") return "需处理";
      if (value === "red") return "异常";
      if (value === "not_configured") return "未接入";
      return "未知";
    }

    function renderSkillInventory(inventory) {
      const model = inventoryWithLiveLocal(inventory || {});
      const items = Array.isArray(model.items) ? model.items : [];
      $("skill-inventory-summary").textContent = model.total > 0
        ? `${text(model.total)} 个 skill；点开只看安装矩阵，实际操作仍在当前设备客户端完成。`
        : "等待中央仓库或本机客户端上报 skill 清单。";
      $("skill-inventory-total").textContent = text(model.total);
      $("skill-inventory-published").textContent = text(model.published);
      $("skill-inventory-unpublished").textContent = text(model.unpublished);
      $("skill-inventory-project").textContent = text(model.project);
      $("skill-inventory-list").innerHTML = items.length > 0
        ? items.slice(0, 40).map((item) => renderSkillInventoryRow(item)).join("")
        : `<div class="empty">暂无可展示 skill。先点“扫描本机”，或等待设备 Agent 上报。</div>`;
    }

    function inventoryWithLiveLocal(inventory) {
      const baseItems = Array.isArray(inventory.items) ? inventory.items : [];
      const bySkill = {};
      baseItems.forEach((item) => {
        const skillId = text(item.skill_id);
        if (!skillId) return;
        bySkill[skillId] = {
          ...item,
          central: item.central || { state: "unpublished" },
          installations: Array.isArray(item.installations) ? [...item.installations] : [],
        };
      });
      const liveTools = localWorkspaceFromExecutor && Array.isArray(localWorkspaceFromExecutor.tools)
        ? localWorkspaceFromExecutor.tools
        : [];
      liveTools.forEach((tool) => {
        const toolId = text(tool.id || "unknown");
        const toolName = text(tool.name || toolId);
        const skills = Array.isArray(tool.skill_items) ? tool.skill_items : [];
        skills.forEach((skill) => {
          const skillId = text(skill.skill_id);
          if (!skillId) return;
          const entry = bySkill[skillId] || {
            skill_id: skillId,
            name: skill.name || skillId,
            description: skill.description || "",
            scope: skill.scope || "global",
            central: { state: "unpublished" },
            sync_state: "ok",
            pending: 0,
            installations: [],
          };
          const installKey = `mac::${toolId}`;
          const exists = entry.installations.some((installed) => `${text(installed.device_id)}::${text(installed.tool_id)}` === installKey);
          if (!exists) {
            entry.installations.push({
              device_id: "mac",
              device_name: "Mac 本机",
              tool_id: toolId,
              tool_name: toolName,
              state: "installed",
              path: skill.path,
              content_hash: skill.content_hash,
              risk_level: skill.risk_level,
            });
          }
          entry.name = entry.name || skill.name || skillId;
          entry.scope = entry.scope === "project" || skill.scope === "project" ? "project" : (entry.scope || skill.scope || "global");
          bySkill[skillId] = entry;
        });
      });
      const items = Object.values(bySkill).map((item) => {
        const installations = Array.isArray(item.installations) ? item.installations : [];
        const installedTools = [...new Set(installations.map((installed) => text(installed.tool_id)).filter(Boolean))].sort();
        const installedDevices = [...new Set(installations.map((installed) => text(installed.device_id)).filter(Boolean))].sort();
        return {
          ...item,
          installed_tools: installedTools,
          installed_devices: installedDevices,
          tool_count: installedTools.length,
          device_count: installedDevices.length,
        };
      }).sort((a, b) => {
        const pendingA = Number(a.pending || 0) > 0 ? 0 : 1;
        const pendingB = Number(b.pending || 0) > 0 ? 0 : 1;
        return pendingA - pendingB || text(a.skill_id).localeCompare(text(b.skill_id));
      });
      return {
        total: items.length,
        published: items.filter((item) => (item.central || {}).state === "published").length,
        unpublished: items.filter((item) => (item.central || {}).state === "unpublished").length,
        project: items.filter((item) => item.scope === "project").length,
        items,
      };
    }

    function renderSkillInventoryRow(item) {
      const installed = new Set(Array.isArray(item.installed_tools) ? item.installed_tools : []);
      const pending = Number(item.pending || 0) > 0;
      const centralState = text((item.central || {}).state || "unpublished");
      const toolChecks = skillInventoryTools().map((tool) => {
        const active = installed.has(tool.id);
        const cls = active ? "installed" : "";
        const mark = active ? "✓" : "□";
        return `<span class="skill-tool-check ${cls}" title="${escapeHtml(active ? "已安装" : "未安装")}">${mark} ${escapeHtml(tool.label)}</span>`;
      }).join("");
      const stateClass = pending ? "pending" : (centralState === "published" ? "installed" : "");
      return `
        <article class="skill-inventory-row">
          <div>
            <div class="skill-inventory-name">${escapeHtml(text(item.skill_id))}</div>
            <div class="skill-inventory-meta">${escapeHtml(skillScopeLabel(item.scope))} · ${escapeHtml(centralLabel(centralState))}</div>
          </div>
          <div class="skill-tool-checks">${toolChecks}</div>
          <div>
            <div class="skill-inventory-action">${escapeHtml(item.action || inventoryActionText(item))}</div>
            <div class="skill-tool-check ${stateClass}">${escapeHtml(pending ? `${item.pending} 项待确认` : centralLabel(centralState))}</div>
          </div>
        </article>
      `;
    }

    function skillInventoryTools() {
      return [
        { id: "codex", label: "Codex" },
        { id: "claude-code", label: "Claude" },
        { id: "cursor", label: "Cursor" },
        { id: "cc-switch", label: "cc-switch" },
        { id: "skillshub", label: "skillshub" },
        { id: "openclaw", label: "OpenClaw" },
      ];
    }

    function skillScopeLabel(scope) {
      if (scope === "project") return "项目级";
      if (scope === "device-private") return "设备私有";
      return "公用";
    }

    function centralLabel(state) {
      if (state === "published") return "已发布中央仓库";
      if (state === "deprecated") return "已废弃";
      return "未发布中央仓库";
    }

    function inventoryActionText(item) {
      if (item.sync_state === "source_changed") return "改完后检查最新版本。";
      if (item.sync_state === "pending_publish") return "检查通过后可发布中央仓库。";
      if ((item.central || {}).state === "unpublished") return "可选择发布到中央仓库。";
      return "可选择安装到本机工具。";
    }

    function renderLocalWorkspace(workspace) {
      const live = localWorkspaceFromExecutor || {};
      const tools = Array.isArray(live.tools) ? live.tools : (Array.isArray(workspace.tools) ? workspace.tools : []);
      const total = live.total_skills ?? workspace.total_skills;
      const blocked = live.blocked ?? workspace.blocked;
      const source = localWorkspaceFromExecutor ? "本机实时扫描" : (workspace.reported ? "最近一次 Mac 上报" : "等待本机授权");
      const deviceName = text(workspace.device_name || live.device_name || "Mac 本机");
      $("local-workspace-pill").outerHTML = pill(source, localWorkspaceFromExecutor ? "green" : deviceKind(workspace.health)).replace("<span", "<span id=\"local-workspace-pill\"");
      $("local-workspace-summary").textContent = `${deviceName} 是当前页面唯一能直接操作的设备。日常操作请用页面顶部两个入口。`;
      $("local-workspace-total").textContent = text(total);
      $("local-workspace-blocked").textContent = text(blocked);
      $("local-workspace-source").textContent = localWorkspaceFromExecutor ? "实时" : (workspace.reported ? "上报" : "未授权");
      renderLocalToolSummary(tools);
      $("local-workspace-tools").innerHTML = tools.map((tool) => `
        <div class="workspace-tool">
          <div class="workspace-tool-row">
            <div class="workspace-tool-name">${escapeHtml(text(tool.name))}</div>
            <div class="workspace-tool-count">${escapeHtml(text(tool.skills))}</div>
            ${toolStatePill(tool)}
          </div>
        </div>
      `).join("");
      const remoteNote = workspace.remote_blocked_note && Number(workspace.remote_blocked || 0) > 0
        ? ` ${workspace.remote_blocked_note}`
        : "";
      $("local-workspace-boundary").textContent = (workspace.boundary || "这里不会跨设备改 OpenClaw 或 Windows；其他设备只看状态。") + remoteNote;
    }

    function renderLocalToolSummary(tools) {
      const items = Array.isArray(tools) ? tools : [];
      const detected = items.filter((tool) => tool.state === "detected" || tool.installed).length;
      const warnings = items.reduce((sum, tool) => sum + Number((tool.risk || {}).warning || 0), 0);
      const errors = items.reduce((sum, tool) => sum + Number((tool.risk || {}).error || 0), 0);
      $("local-workspace-tool-summary").innerHTML = [
        toolSummaryItem(detected, "已检测工具"),
        toolSummaryItem(warnings, "需整理提示"),
        toolSummaryItem(errors, "错误"),
      ].join("");
    }

    function toolSummaryItem(value, label) {
      return `
        <div class="workspace-tool-summary-item">
          <div class="workspace-tool-summary-value">${escapeHtml(text(value))}</div>
          <div class="workspace-tool-summary-label">${escapeHtml(label)}</div>
        </div>
      `;
    }

    function renderCentralRepository(repo) {
      $("central-repository-pill").outerHTML = pill("共享仓库", "green").replace("<span", "<span id=\"central-repository-pill\"");
      $("central-repository-summary").textContent = `共享仓库收录 ${text(repo.total_skills)} 个 skill。这里是只读明细，不能直接编辑。`;
      $("central-repository-kv").innerHTML = [
        row("当前版本", repo.snapshot_id),
        row("更新时间", repo.created_at),
        row("协议版本", repo.protocol_version),
        row("目标覆盖", repo.targeted_projection_total),
      ].join("");
      $("central-repository-boundary").textContent = repo.boundary || "共享仓库只接受你确认后的发布。";
    }

    function renderDeviceMap(map) {
      const items = otherDeviceItems(map.items);
      $("device-map-summary").textContent = items.length > 0
        ? (map.boundary || "其他设备默认只读。")
        : "暂无其他设备上报；本机操作在左侧完成。";
      $("device-map").innerHTML = items.length > 0 ? items.map((device) => `
        <div class="device-map-item">
          <div class="card-head">
            <div>
              <div class="card-name">${escapeHtml(text(device.name))}</div>
              <div class="card-kind">${escapeHtml(text(device.capability))}</div>
            </div>
            ${pill(statusLabel(device.health || device.operation_scope), deviceKind(device.health))}
          </div>
          <div class="device-map-meta">
            <div>技能 ${escapeHtml(text(device.skills))} · 需确认 ${escapeHtml(text(device.blocked))}</div>
            <div>权限 ${escapeHtml(scopeLabel(device.operation_scope))} · ${freshnessPill(device.freshness)}</div>
          </div>
        </div>
      `).join("") : `<div class="empty">暂无其他设备状态。</div>`;
    }

    function otherDeviceItems(items) {
      return (Array.isArray(items) ? items : []).filter((device) => device && device.operation_scope !== "local");
    }

    async function refreshLocalWorkspace() {
      try {
        const response = await fetch(`${EXECUTOR_URL}/api/local-workspace`, { method: "GET", cache: "no-store" });
        const payload = await response.json();
        if (response.ok && payload.ok) {
          localWorkspaceFromExecutor = payload;
          executorAvailable = true;
          executorAllowPublish = Boolean(payload.allow_publish);
          executorAllowLocalWrites = Boolean(payload.allow_local_writes);
          renderLocalWorkspace(window.lastDashboard ? window.lastDashboard.local_workspace || {} : {});
          renderSkillInventory(window.lastDashboard ? window.lastDashboard.skill_inventory || {} : {});
          setExecutorStatus("online", payload.allow_publish ? "Mac 本机助手在线：本机扫描可用，发布已开启。" : "Mac 本机助手在线：本机扫描和检查可用，发布未开启。", "green");
          setExecutorButtons(true);
        } else {
          throw new Error(payload.error || "local workspace scan failed");
        }
      } catch (err) {
        localWorkspaceFromExecutor = null;
        setExecutorOffline();
      }
    }

    async function analyzeLocalSkill() {
      if (!executorAvailable) return;
      const path = $("local-skill-path").value.trim();
      if (!path) {
        renderLocalSkillError("请先填写 skill 目录或 SKILL.md 路径。");
        return;
      }
      lastLocalSkillAnalysis = null;
      setLocalSkillStatus("analyzing", "正在分析本地 skill。", "yellow");
      setExecutorButtons(false);
      try {
        const response = await fetch(`${EXECUTOR_URL}/api/local-skill/analyze`, {
          method: "POST",
          cache: "no-store",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path }),
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) throw new Error(payload.error || "analyze failed");
        lastLocalSkillAnalysis = payload;
        setLocalSkillStatus("ready", payload.operator_action || "分析完成。", payload.risk && payload.risk.level === "ok" ? "green" : "yellow");
        renderLocalSkillAnalysis(payload);
        renderLocalSkillPublishHint();
      } catch (err) {
        renderLocalSkillError(String(err));
      } finally {
        setExecutorButtons(executorAvailable);
      }
    }

    async function installLocalSkill() {
      if (!executorAvailable || !lastLocalSkillAnalysis) return;
      if (!executorAllowLocalWrites) {
        renderLocalSkillError("本机写入未授权：请打开本机写入开关后再安装。");
        return;
      }
      const writes = Number((lastLocalSkillAnalysis.summary || {}).will_write || 0);
      if (writes <= 0) return;
      const typed = window.prompt(`将安装 ${lastLocalSkillAnalysis.skill_id} 到 ${writes} 个本机工具。请输入 INSTALL 确认：`);
      if (typed !== "INSTALL") {
        setLocalSkillStatus("cancelled", "已取消安装，没有写入本机工具目录。", "yellow");
        return;
      }
      setLocalSkillStatus("installing", "正在安装到本机工具目录。", "yellow");
      setExecutorButtons(false);
      try {
        const response = await fetch(`${EXECUTOR_URL}/api/local-skill/install`, {
          method: "POST",
          cache: "no-store",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: lastLocalSkillAnalysis.source_path, confirm: "INSTALL" }),
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) throw new Error(payload.error || "install failed");
        setLocalSkillStatus("installed", "安装完成；已自动写入配置和备份记录。", "green");
        renderLocalSkillInstall(payload);
        renderLocalSkillPublishHint();
        await refreshLocalWorkspace();
      } catch (err) {
        renderLocalSkillError(String(err));
      } finally {
        setExecutorButtons(executorAvailable);
      }
    }

    async function publishLocalSkill(realPublish) {
      if (!executorAvailable || !lastLocalSkillAnalysis) return;
      if (realPublish && !executorAllowPublish) {
        renderLocalSkillError("发布未授权：请打开发布开关后再发布到共享仓库。");
        return;
      }
      if (realPublish) {
        const typed = window.prompt(`将 ${lastLocalSkillAnalysis.skill_id} 发布到共享仓库。请输入 PUBLISH 确认：`);
        if (typed !== "PUBLISH") {
          setLocalSkillStatus("cancelled", "已取消发布，没有写入共享仓库。", "yellow");
          return;
        }
      }
      setLocalSkillStatus(realPublish ? "publishing" : "checking", realPublish ? "正在发布到共享仓库。" : "正在检查发布。", "yellow");
      setExecutorButtons(false);
      try {
        const endpoint = realPublish ? "/api/local-skill/publish" : "/api/local-skill/publish-dry-run";
        const response = await fetch(`${EXECUTOR_URL}${endpoint}`, {
          method: "POST",
          cache: "no-store",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: lastLocalSkillAnalysis.source_path, confirm: realPublish ? "PUBLISH" : undefined }),
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) throw new Error(payload.error || "publish failed");
        setLocalSkillStatus(realPublish ? "published" : "publish ok", realPublish ? "共享仓库已更新。" : "检查通过，可以发布到共享仓库。", "green");
        $("local-skill-result").textContent = `${payload.skill_id} · ${modeLabel(payload.mode)} · ${payload.safe_to_push ? "可以发布" : "需要复核"} · 文件 ${text(payload.uploaded_files)} · 共享仓库版本 ${text(payload.snapshot_id)}`;
      } catch (err) {
        renderLocalSkillError(String(err));
      } finally {
        setExecutorButtons(executorAvailable);
      }
    }

    function renderLocalSkillAnalysis(payload) {
      const summary = payload.summary || {};
      const writes = Number(summary.will_write || 0);
      $("local-skill-result").textContent = `${payload.skill_id} · ${payload.scope} · ${payload.manifest_source === "generated" ? "sidecar 自动生成元数据" : "读取配置文件"} · 可写入 ${writes} 个工具`;
      renderLocalSkillTools(payload.tools || []);
    }

    function renderLocalSkillInstall(payload) {
      const summary = payload.summary || {};
      $("local-skill-result").textContent = `${payload.skill_id} 安装完成 · 写入 ${text(summary.will_write)} 个工具 · 记录 ${text(payload.record_path)}`;
      renderLocalSkillTools(payload.items || []);
    }

    function renderLocalSkillPublishHint() {
      if (!lastLocalSkillAnalysis) return;
      if (!executorAllowPublish) {
        $("local-skill-result").textContent += " · 发布开关未打开，可先检查发布；真实发布需启用发布权限";
      }
    }

    function renderLocalSkillTools(items) {
      $("local-skill-tools").innerHTML = (Array.isArray(items) ? items : []).map((item) => `
        <div class="local-skill-tool">
          <strong>${escapeHtml(text(item.tool_name || item.tool_id))}</strong>
          <span>${escapeHtml(text(item.action))}${item.reason ? " · " + escapeHtml(text(item.reason)) : ""}</span>
        </div>
      `).join("");
    }

    function renderLocalSkillError(message) {
      lastLocalSkillAnalysis = null;
      $("local-skill-result").textContent = message;
      $("local-skill-tools").innerHTML = "";
      setLocalSkillStatus("error", message, "red");
      setExecutorButtons(executorAvailable);
    }

    function setLocalSkillStatus(label, detail, kind) {
      $("local-skill-pill").outerHTML = pill(statusPillLabel(label), kind).replace("<span", "<span id=\"local-skill-pill\"");
      $("local-skill-result").textContent = detail;
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
            ${pill(healthLabel(device.health), deviceKind(device.health))}
          </div>
          <div class="card-note">${escapeHtml(device.note)}</div>
          <div class="card-stats">
            <div class="mini-stat"><div class="mini-label">技能数</div><div class="mini-value">${escapeHtml(text(device.skills))}</div></div>
            <div class="mini-stat"><div class="mini-label">需确认</div><div class="mini-value">${escapeHtml(text(device.blocked))}</div></div>
            <div class="mini-stat"><div class="mini-label">策略</div><div class="mini-value">${escapeHtml(scopeLabel(device.policy))}</div></div>
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
            ${pill(scopeLabel(device.policy || device.health), "")}
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
      return pill(statusLabel(tool.state || "unknown"), "");
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
        planCell("模式", modeLabel(actionPlan.mode || "dry_run")),
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
      $("hub-import-preview-status").textContent = "预览包已生成，当前只做检查，不执行写入。";
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

    async function refresh(force) {
      try {
        const endpoint = force ? `/api/summary?refresh=1&_=${Date.now()}` : `/api/summary?_=${Date.now()}`;
        const response = await fetch(endpoint, { cache: "no-store" });
        const status = await response.json();
        if (!response.ok) throw new Error(status.error || `HTTP ${response.status}`);
        render(status);
        const cache = status.summary_cache || (status.dashboard || {}).summary_cache || {};
        if (!force && cache.state === "stale") {
          $("updated").textContent = "状态缓存偏旧，正在重新读取实时状态...";
          if (staleRefreshTimer) clearTimeout(staleRefreshTimer);
          staleRefreshTimer = setTimeout(() => refresh(true), 800);
        }
      } catch (error) {
        $("error").textContent = error.message;
        $("error").style.display = "block";
        $("updated").textContent = "更新失败";
      }
    }

    $("refresh").addEventListener("click", () => refresh(true));
    $("hub-import-preview-button").addEventListener("click", generateHubImportPreview);
    window.addEventListener("resize", rerenderReviewQueueIfViewportModeChanged);
    refresh(true);
    setInterval(() => refresh(false), 30000);
  </script>
</body>
</html>
"""
