from __future__ import annotations

import json
import time
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
    status["dashboard"] = {
        "health": _aggregate_health([status.get("health")] + [device.get("health") for device in devices]),
        "blocked": len(blocked_items),
        "operator": operator,
        "blocked_items": blocked_items,
        "devices": devices,
        "tools": _merge_tool_projection(local_tools, projection),
        "device_tools": _device_tool_overview(devices, {"mac": {"tools": local_tools, "published_at": _status_last_seen_at(status)}, **peers}),
        "tool_projection": projection,
        "hub_import": hub_import,
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
    status["dashboard"] = {
        "health": _aggregate_health([status.get("health")] + [device.get("health") for device in devices]),
        "blocked": len(blocked_items),
        "operator": operator,
        "blocked_items": blocked_items,
        "devices": devices,
        "tools": _gateway_tool_overview(projection),
        "device_tools": _device_tool_overview(devices, peers),
        "tool_projection": projection,
        "hub_import": hub_import,
    }
    return status


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
            "blocked": blocked,
            "policy": _policy_label(status.get("writer_policy"), daemon.get("writer_policy")),
            "note": current_note,
            "local_policy": local_overrides.get("skills", []),
            "last_seen_at": last_seen_at,
            "freshness": _freshness_info(last_seen_at),
        },
        openclaw_device,
        {
            "id": "win",
            "name": "Windows",
            "kind": "计划设备",
            "health": "not_configured",
            "skills": None,
            "blocked": None,
            "policy": "未接入",
            "note": "等待安装 sidecar 后纳入同一面板",
            "local_policy": [],
            "last_seen_at": None,
            "freshness": _freshness_info(None),
        },
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
        {
            "id": "win",
            "name": "Windows",
            "kind": "计划设备",
            "health": "not_configured",
            "skills": None,
            "blocked": None,
            "policy": "未接入",
            "note": "等待安装 sidecar 后纳入同一面板",
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
    win = _find_device(devices, "win")
    if health == "green":
        next_action = "同步链路正常；继续观察自动周期，或接入 Windows。"
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
            "windows": _operator_device_line(win),
        },
        "blocked_count": len(blocked_items),
    }


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
    items = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        copied = dict(item)
        copied["peer_id"] = peer_id
        copied["peer_name"] = peer_name
        items.append(copied)
    return items


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


def _handler_factory(status_provider: Callable[[], dict], hub_import_preview_provider: Optional[Callable[[], dict]] = None):
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
            if path == "/healthz":
                self._send(200, "application/json; charset=utf-8", b'{"ok":true}\n')
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
            if path == "/api/status":
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
      --bg: #f7f8fa;
      --panel: #ffffff;
      --ink: #1d2433;
      --muted: #647084;
      --line: #d8dde6;
      --green: #147d50;
      --yellow: #9a6700;
      --red: #c63232;
      --blue: #2557a7;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: #fff;
    }
    h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 650;
      letter-spacing: 0;
    }
    main {
      max-width: 1180px;
      margin: 0 auto;
      padding: 20px 24px 32px;
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
      padding: 7px 11px;
      font: inherit;
      cursor: pointer;
    }
    button:disabled {
      cursor: default;
      color: var(--muted);
      background: #f3f5f8;
    }
    button:hover { border-color: #aeb7c6; }
    .operator-band {
      display: grid;
      grid-template-columns: minmax(280px, 1fr) minmax(280px, 1fr);
      gap: 12px;
      margin-bottom: 18px;
    }
    .operator-title {
      font-size: 18px;
      font-weight: 720;
      margin-bottom: 6px;
    }
    .operator-text {
      color: var(--muted);
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
      padding: 14px;
      min-width: 0;
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
      .status-band { grid-template-columns: 1fr 1fr; }
      .status-band .panel { grid-column: 1 / -1; }
      .cards { grid-template-columns: 1fr; }
      .device-tool-grid { grid-template-columns: 1fr; }
      .grid { grid-template-columns: 1fr; }
      .plan-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 560px) {
      main { padding: 14px; }
      .status-band { grid-template-columns: 1fr; }
      .kv { grid-template-columns: 1fr; }
      .plan-strip { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <a href="http://100.123.208.32:17172/portal" style="display:inline-block;margin:.6rem 0;font-size:13px;color:var(--muted,#647084);text-decoration:none">← 报表门户</a>
  <header>
    <h1>Skill Sync Sidecar</h1>
    <div class="toolbar">
      <span id="updated">Loading</span>
      <button id="refresh" type="button" title="Refresh status">Refresh</button>
    </div>
  </header>
  <main>
    <div id="error" class="error"></div>
    <section class="operator-band">
      <div class="panel">
        <div id="operator-headline" class="operator-title">读取同步状态中</div>
        <div id="operator-next" class="operator-text">等待 sidecar 返回状态。</div>
      </div>
      <div class="panel">
        <h2>同步路径</h2>
        <div id="operator-path" class="operator-text mono">-</div>
        <div id="operator-snapshot" class="operator-text mono">-</div>
      </div>
    </section>
    <section class="status-band">
      <div id="health-card" class="panel health">
        <span class="dot"></span>
        <div>
          <div id="health" class="health-title">Unknown</div>
          <div id="next-action" class="health-subtitle">Waiting for status</div>
        </div>
      </div>
      <div class="metric">
        <div class="metric-label">Blocked</div>
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
      <span class="section-help">区分 Mac、OpenClaw、Windows 的接入状态</span>
    </div>
    <section id="devices" class="cards"></section>
    <div class="section-title">
      <h2>工具</h2>
      <span class="section-help">WebDAV canonical snapshot 对各工具的目标覆盖，不代表某台设备已安装</span>
    </div>
    <section id="tools" class="cards"></section>
    <div class="section-title">
      <h2>设备工具实测</h2>
      <span class="section-help">由每台设备 Agent 上报，区分 Mac、OpenClaw、Windows 的真实工具目录</span>
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
          <h2>Blocked Queue</h2>
          <div id="blocked-empty" class="empty">No blocked items.</div>
          <table id="blocked-table" hidden>
            <thead><tr><th>Skill</th><th>Status</th><th>Category</th><th>Reason</th></tr></thead>
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
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
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
      if (status.health === "green") return "No review-required sync work.";
      if (status.health === "yellow") return "Review blocked queue or OpenClaw gate before publishing.";
      if (status.health === "red") return "Fix unreadable artifacts or status errors.";
      return "Status unavailable.";
    }

    function render(status) {
      $("error").style.display = "none";
      const dashboard = status.dashboard || {};
      const operator = dashboard.operator || {};
      const health = dashboard.health || status.health || "unknown";
      $("health-card").className = `panel health ${health}`;
      $("health").textContent = health;
      $("next-action").textContent = operator.next_action || nextAction({ ...status, health });
      $("operator-headline").textContent = operator.headline || "同步状态未知";
      $("operator-next").textContent = operator.next_action || nextAction({ ...status, health });
      $("operator-path").textContent = operator.sync_path || "-";
      $("operator-snapshot").textContent = `snapshot: ${text(operator.snapshot_id)}`;
      const plan = status.sync_plan || {};
      const snapshot = status.remote_snapshot || {};
      const daemon = status.daemon_state || {};
      const blockedReport = status.blocked_report || {};
      $("blocked").textContent = text(dashboard.blocked ?? plan.blocked ?? blockedReport.total);
      $("allowed").textContent = text(plan.allowed);
      $("remote-total").textContent = text(snapshot.total);
      $("cycles").textContent = text(daemon.cycles_run);
      $("updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
      renderDevices(Array.isArray(dashboard.devices) ? dashboard.devices : []);
      renderTools(Array.isArray(dashboard.tools) ? dashboard.tools : []);
      renderDeviceTools(Array.isArray(dashboard.device_tools) ? dashboard.device_tools : []);
      renderHubImport(dashboard.hub_import || {});

      const blockedItems = Array.isArray(dashboard.blocked_items) ? dashboard.blocked_items : (Array.isArray(blockedReport.items) ? blockedReport.items : []);
      $("blocked-empty").hidden = blockedItems.length > 0;
      $("blocked-table").hidden = blockedItems.length === 0;
      $("blocked-body").innerHTML = blockedItems.map((item) => `
        <tr>
          <td class="mono">${escapeHtml(text(item.peer_name || item.peer_id))} / ${escapeHtml(text(item.skill_id))}</td>
          <td>${escapeHtml(text(item.status_action))}</td>
          <td>${escapeHtml(text(item.category))}</td>
          <td>${escapeHtml(text(item.reason))}</td>
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
      if (tool.state === "observer") return pill("observed", "green");
      if (tool.state === "error") return pill("error", "red");
      if (tool.state === "detected" || tool.installed === true) return pill("detected", "green");
      if (tool.state === "unsupported") return pill("unsupported", "");
      if (tool.installed === false) return pill("not found", "");
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

    function renderOperatorDevices(devices) {
      const rows = [
        ["Mac", devices.mac],
        ["OpenClaw", devices.openclaw],
        ["Windows", devices.windows],
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
        const response = await fetch("/api/status", { cache: "no-store" });
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
