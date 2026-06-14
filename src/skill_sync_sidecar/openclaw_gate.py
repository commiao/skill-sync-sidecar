from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


JsonDict = Dict[str, Any]


def select_latest_reconcile_report(root: Path) -> Optional[Path]:
    root = root.expanduser()
    if not root.exists():
        return None
    candidates = [path for path in root.rglob("reconcile-report.json") if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=_report_sort_key)


def build_openclaw_gate(report_path: Optional[Path] = None, report_root: Optional[Path] = None) -> JsonDict:
    selected_path = report_path.expanduser() if report_path else None
    selected_by = "explicit" if selected_path else None
    if selected_path is None and report_root is not None:
        selected_path = select_latest_reconcile_report(report_root)
        selected_by = "latest"
    if selected_path is None:
        return {"ok": True, "available": False, "reason": "no OpenClaw reconcile report found"}

    report, error = _load_json_file(selected_path)
    if error:
        return {"ok": False, "available": False, "path": str(selected_path), "error": error}
    assert report is not None

    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    changed = report.get("changed_since_previous")
    changed_count = changed.get("changed_count") if isinstance(changed, dict) else None
    blockers = _blockers(report, summary, changed_count)

    return {
        "ok": not blockers,
        "available": True,
        "path": str(selected_path),
        "selected_by": selected_by,
        "label": report.get("label"),
        "created_at": report.get("created_at"),
        "local_total": report.get("local_total"),
        "remote_total": report.get("remote_total"),
        "safe_to_auto_apply": report.get("safe_to_auto_apply"),
        "summary": summary,
        "changed_since_previous": changed,
        "changed_count": changed_count,
        "blockers": blockers,
    }


def render_openclaw_gate_text(gate: JsonDict) -> str:
    if not gate.get("available"):
        if gate.get("error"):
            return f"openclaw_gate: unavailable ({gate['error']})"
        return f"openclaw_gate: unavailable ({gate.get('reason')})"
    lines = [
        f"openclaw_gate: ok={gate.get('ok')} safe_to_auto_apply={gate.get('safe_to_auto_apply')}",
        f"report: {gate.get('path')}",
        f"summary: {gate.get('summary')}",
        f"changed_since_previous: {gate.get('changed_count')}",
    ]
    blockers = gate.get("blockers") or []
    if blockers:
        lines.append(f"blockers: {', '.join(str(item) for item in blockers)}")
    return "\n".join(lines)


def _blockers(report: JsonDict, summary: JsonDict, changed_count: Optional[int]) -> list[str]:
    blockers: list[str] = []
    if not report.get("safe_to_auto_apply"):
        blockers.append("safe_to_auto_apply=false")
    for key in ("conflict", "local_new"):
        count = int(summary.get(key, 0) or 0)
        if count:
            blockers.append(f"{key}={count}")
    if changed_count:
        blockers.append(f"changed_since_previous={changed_count}")
    return blockers


def _report_sort_key(path: Path) -> Tuple[str, float, str]:
    report, _ = _load_json_file(path)
    created_at = str(report.get("created_at") or "") if report else ""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return created_at, mtime, str(path)


def _load_json_file(path: Path) -> Tuple[Optional[JsonDict], Optional[str]]:
    if not path.exists():
        return None, f"not found: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, str(exc)
    except json.JSONDecodeError as exc:
        return None, f"invalid json: {exc}"
    if not isinstance(data, dict):
        return None, "json root is not an object"
    return data, None
