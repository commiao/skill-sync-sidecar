from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .openclaw_gate import build_openclaw_gate
from .sync_plan import build_sync_plan
from .sync_state import SyncStateError, build_sync_status


JsonDict = Dict[str, Any]


def build_ops_status(
    local_root: Path,
    remote_snapshot: Path,
    base_record: Optional[Path] = None,
    state_file: Optional[Path] = None,
    blocked_report: Optional[Path] = None,
    openclaw_reconcile_report: Optional[Path] = None,
    openclaw_reconcile_root: Optional[Path] = None,
    allow_new: bool = False,
    allow_delete: bool = False,
    writer_policy: str = "push-pull",
) -> JsonDict:
    local_root = local_root.expanduser()
    remote_snapshot = remote_snapshot.expanduser()
    base_record = base_record.expanduser() if base_record else None
    state_file = state_file.expanduser() if state_file else None
    blocked_report = blocked_report.expanduser() if blocked_report else None
    openclaw_reconcile_report = openclaw_reconcile_report.expanduser() if openclaw_reconcile_report else None
    openclaw_reconcile_root = openclaw_reconcile_root.expanduser() if openclaw_reconcile_root else None

    sync_plan = _sync_plan_summary(local_root, remote_snapshot, base_record, allow_new=allow_new, allow_delete=allow_delete, writer_policy=writer_policy)
    blocked = blocked_report_summary(blocked_report)
    openclaw_gate = build_openclaw_gate(openclaw_reconcile_report, openclaw_reconcile_root)
    artifact_sections = [
        snapshot_summary(remote_snapshot),
        base_record_summary(base_record),
        daemon_state_summary(state_file),
        blocked,
        sync_plan,
    ]
    artifact_errors = [section for section in artifact_sections if section is not None and not section.get("ok")]
    gate_error = not openclaw_gate.get("available", False) and bool(openclaw_gate.get("error"))
    gate_blocked = openclaw_gate.get("available", False) and not openclaw_gate.get("ok", True)
    error_count = len(artifact_errors) + (1 if gate_error else 0)
    health = _health(error_count, sync_plan, blocked, gate_blocked)

    return {
        "ok": health == "green",
        "health": health,
        "local_root": str(local_root.resolve()),
        "remote_snapshot": snapshot_summary(remote_snapshot),
        "base_record": base_record_summary(base_record),
        "daemon_state": daemon_state_summary(state_file),
        "blocked_report": blocked,
        "sync_plan": sync_plan,
        "openclaw_reconcile": reconcile_summary(Path(openclaw_gate["path"])) if openclaw_gate.get("available") else None,
        "openclaw_gate": openclaw_gate,
        "allow_new": allow_new,
        "allow_delete": allow_delete,
        "writer_policy": writer_policy,
        "error_count": error_count,
    }


def snapshot_summary(snapshot_dir: Path) -> JsonDict:
    index, error = _load_json_file(snapshot_dir / "index.json")
    payload: JsonDict = {"ok": error is None, "path": str(snapshot_dir.expanduser())}
    if error:
        payload["error"] = error
        return payload
    assert index is not None
    payload.update(
        {
            "snapshot_id": index.get("snapshot_id"),
            "created_at": index.get("created_at"),
            "total": index.get("total", len(index.get("skills", []))),
            "protocol_version": index.get("protocol_version"),
        }
    )
    return payload


def base_record_summary(record_path: Optional[Path]) -> Optional[JsonDict]:
    if record_path is None:
        return None
    record, error = _load_json_file(record_path)
    payload: JsonDict = {"ok": error is None, "path": str(record_path.expanduser())}
    if error:
        payload["error"] = error
        return payload
    assert record is not None
    applied = record.get("applied", [])
    payload.update(
        {
            "record_type": record.get("record_type"),
            "sync_id": record.get("sync_id"),
            "snapshot_id": record.get("snapshot_id"),
            "created_at": record.get("created_at"),
            "applied_count": len(applied) if isinstance(applied, list) else 0,
        }
    )
    return payload


def blocked_report_summary(report_path: Optional[Path]) -> Optional[JsonDict]:
    if report_path is None:
        return None
    report, error = _load_json_file(report_path)
    payload: JsonDict = {"ok": error is None, "path": str(report_path.expanduser())}
    if error:
        payload["error"] = error
        return payload
    assert report is not None
    raw_items = report.get("items", [])
    items = raw_items if isinstance(raw_items, list) else []
    blocked_items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        blocked_items.append(
            {
                "skill_id": item.get("skill_id"),
                "category": item.get("category"),
                "status_action": item.get("status_action"),
                "plan_action": item.get("plan_action"),
                "reason": item.get("reason"),
                "recommendation": item.get("recommendation"),
            }
        )
    payload.update(
        {
            "record_type": report.get("record_type"),
            "created_at": report.get("created_at"),
            "writer_policy": report.get("writer_policy"),
            "total": report.get("total", len(blocked_items)),
            "summary": report.get("summary", {}),
            "items": blocked_items,
        }
    )
    return payload


def daemon_state_summary(state_file: Optional[Path]) -> Optional[JsonDict]:
    if state_file is None:
        return None
    state, error = _load_json_file(state_file)
    payload: JsonDict = {"ok": error is None, "path": str(state_file.expanduser())}
    if error:
        payload["error"] = error
        return payload
    assert state is not None
    cycles = state.get("cycles", [])
    last_cycle = cycles[-1] if isinstance(cycles, list) and cycles else None
    payload.update(
        {
            "status": state.get("status"),
            "daemon_status": state.get("daemon_status"),
            "updated_at": state.get("updated_at"),
            "cycles_run": state.get("cycles_run"),
            "current_base_record": state.get("current_base_record"),
            "last_cycle": last_cycle,
            "active_cycle": state.get("active_cycle"),
        }
    )
    return payload


def reconcile_summary(report_path: Optional[Path]) -> Optional[JsonDict]:
    if report_path is None:
        return None
    report, error = _load_json_file(report_path)
    payload: JsonDict = {"ok": error is None, "path": str(report_path.expanduser())}
    if error:
        payload["error"] = error
        return payload
    assert report is not None
    changed = report.get("changed_since_previous")
    payload.update(
        {
            "label": report.get("label"),
            "local_total": report.get("local_total"),
            "remote_total": report.get("remote_total"),
            "safe_to_auto_apply": report.get("safe_to_auto_apply"),
            "summary": report.get("summary", {}),
            "changed_since_previous": changed,
        }
    )
    return payload


def render_ops_status_text(status: JsonDict) -> str:
    lines = ["skill-sync ops status"]
    lines.append(f"health: {status.get('health', 'unknown')}")
    lines.append(f"local_root: {status['local_root']}")
    lines.extend(_render_snapshot(status.get("remote_snapshot")))
    lines.extend(_render_base_record(status.get("base_record")))
    lines.extend(_render_daemon_state(status.get("daemon_state")))
    lines.extend(_render_blocked_report(status.get("blocked_report")))
    lines.extend(_render_sync_plan(status.get("sync_plan")))
    lines.extend(_render_reconcile(status.get("openclaw_reconcile")))
    lines.extend(_render_openclaw_gate(status.get("openclaw_gate")))
    lines.append(f"overall_ok: {status.get('ok')}")
    return "\n".join(lines)


def _sync_plan_summary(
    local_root: Path,
    remote_snapshot: Path,
    base_record: Optional[Path],
    allow_new: bool,
    allow_delete: bool,
    writer_policy: str,
) -> JsonDict:
    try:
        status = build_sync_status(local_root, remote_snapshot, base_record)
        plan = build_sync_plan(status, allow_new=allow_new, allow_delete=allow_delete, writer_policy=writer_policy)
    except (SyncStateError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "writer_policy": plan["writer_policy"],
        "total": plan["total"],
        "summary": plan["summary"],
        "allowed": plan["allowed"],
        "blocked": plan["blocked"],
        "safe_to_apply": plan["safe_to_apply"],
        "status_summary": status["summary"],
        "has_conflicts": status["has_conflicts"],
    }


def _health(error_count: int, sync_plan: Optional[JsonDict], blocked_report: Optional[JsonDict], gate_blocked: bool) -> str:
    if error_count:
        return "red"
    if sync_plan and sync_plan.get("ok") is False:
        return "red"
    if blocked_report and blocked_report.get("ok") is False:
        return "red"
    blocked_count = 0
    if sync_plan and isinstance(sync_plan.get("blocked"), int):
        blocked_count += int(sync_plan["blocked"])
    if blocked_report and isinstance(blocked_report.get("total"), int):
        blocked_count += int(blocked_report["total"])
    if blocked_count or gate_blocked:
        return "yellow"
    return "green"


def _render_snapshot(snapshot: Optional[JsonDict]) -> list[str]:
    if not snapshot:
        return []
    if not snapshot.get("ok"):
        return [f"remote_snapshot: unavailable ({snapshot.get('error')})"]
    return [
        f"remote_snapshot: {snapshot.get('snapshot_id')} total={snapshot.get('total')}",
        f"remote_created_at: {snapshot.get('created_at')}",
    ]


def _render_base_record(record: Optional[JsonDict]) -> list[str]:
    if record is None:
        return ["base_record: none"]
    if not record.get("ok"):
        return [f"base_record: unavailable ({record.get('error')})"]
    return [
        f"base_record: {record.get('sync_id') or record.get('record_type')} applied={record.get('applied_count')}",
        f"base_snapshot: {record.get('snapshot_id')}",
    ]


def _render_daemon_state(state: Optional[JsonDict]) -> list[str]:
    if state is None:
        return ["daemon_state: none"]
    if not state.get("ok"):
        return [f"daemon_state: unavailable ({state.get('error')})"]
    lines = [
        f"daemon_state: {state.get('daemon_status')} cycles_run={state.get('cycles_run')}",
        f"daemon_updated_at: {state.get('updated_at')}",
    ]
    last_cycle = state.get("last_cycle")
    if isinstance(last_cycle, dict):
        lines.append(
            "last_cycle: "
            f"{last_cycle.get('status')} snapshot={last_cycle.get('snapshot_id')} "
            f"blocked={last_cycle.get('blocked')} summary={last_cycle.get('summary')}"
        )
    return lines


def _render_blocked_report(report: Optional[JsonDict]) -> list[str]:
    if report is None:
        return ["blocked_report: none"]
    if not report.get("ok"):
        return [f"blocked_report: unavailable ({report.get('error')})"]
    lines = [
        f"blocked_report: total={report.get('total')} writer_policy={report.get('writer_policy')}",
        f"blocked_summary: {report.get('summary')}",
    ]
    for item in report.get("items", []):
        lines.append(
            "blocked_item: "
            f"{item.get('skill_id')} "
            f"category={item.get('category')} "
            f"status={item.get('status_action')} "
            f"plan={item.get('plan_action')} "
            f"reason={item.get('reason')}"
        )
    return lines


def _render_sync_plan(plan: Optional[JsonDict]) -> list[str]:
    if not plan:
        return []
    if not plan.get("ok"):
        return [f"sync_plan: unavailable ({plan.get('error')})"]
    return [
        f"sync_plan: safe_to_apply={plan.get('safe_to_apply')} blocked={plan.get('blocked')} allowed={plan.get('allowed')}",
        f"writer_policy: {plan.get('writer_policy')}",
        f"sync_summary: {plan.get('summary')}",
        f"status_summary: {plan.get('status_summary')}",
    ]


def _render_reconcile(reconcile: Optional[JsonDict]) -> list[str]:
    if reconcile is None:
        return ["openclaw_reconcile: none"]
    if not reconcile.get("ok"):
        return [f"openclaw_reconcile: unavailable ({reconcile.get('error')})"]
    changed = reconcile.get("changed_since_previous")
    changed_count = changed.get("changed_count") if isinstance(changed, dict) else None
    return [
        f"openclaw_reconcile: safe_to_auto_apply={reconcile.get('safe_to_auto_apply')} local={reconcile.get('local_total')} remote={reconcile.get('remote_total')}",
        f"openclaw_summary: {reconcile.get('summary')}",
        f"openclaw_changed_since_previous: {changed_count}",
    ]


def _render_openclaw_gate(gate: Optional[JsonDict]) -> list[str]:
    if not gate:
        return []
    if not gate.get("available"):
        return [f"openclaw_gate: unavailable ({gate.get('reason') or gate.get('error')})"]
    lines = [f"openclaw_gate: ok={gate.get('ok')} blockers={gate.get('blockers')}"]
    if gate.get("selected_by"):
        lines.append(f"openclaw_report_selected_by: {gate.get('selected_by')}")
    return lines


def _load_json_file(path: Path) -> Tuple[Optional[JsonDict], Optional[str]]:
    expanded = path.expanduser()
    if not expanded.exists():
        return None, f"not found: {expanded}"
    try:
        data = json.loads(expanded.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, str(exc)
    except json.JSONDecodeError as exc:
        return None, f"invalid json: {exc}"
    if not isinstance(data, dict):
        return None, "json root is not an object"
    return data, None
