from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .sync_plan import build_sync_plan
from .sync_state import build_sync_status


def build_blocked_report(
    local_root: Path,
    remote_snapshot_dir: Path,
    out_dir: Path,
    last_applied_record: Optional[Path] = None,
    allow_new: bool = False,
    allow_delete: bool = False,
    writer_policy: str = "push-pull",
) -> Dict[str, object]:
    status = build_sync_status(local_root, remote_snapshot_dir, last_applied_record)
    plan = build_sync_plan(status, allow_new=allow_new, allow_delete=allow_delete, writer_policy=writer_policy)
    enriched = enrich_blocked_items(plan, writer_policy)
    summary: Dict[str, int] = {}
    for item in enriched:
        category = str(item["category"])
        summary[category] = summary.get(category, 0) + 1

    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "record_type": "skill-sync-blocked-report",
        "local_root": str(local_root.resolve()),
        "remote_snapshot": str(remote_snapshot_dir.resolve()),
        "last_applied_record": str(last_applied_record.resolve()) if last_applied_record else None,
        "writer_policy": writer_policy,
        "allow_new": allow_new,
        "allow_delete": allow_delete,
        "total": len(enriched),
        "summary": dict(sorted(summary.items())),
        "out": str(out_dir.resolve()),
        "items": enriched,
    }
    (out_dir / "blocked-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "blocked-report.md").write_text(_render_markdown(report), encoding="utf-8")
    return report


def enrich_blocked_items(plan: Dict[str, object], writer_policy: str) -> list[Dict[str, object]]:
    blocked_items = [dict(item) for item in plan.get("items", []) if not item.get("allowed")]
    return [_enrich_item(item, writer_policy) for item in blocked_items]


def _enrich_item(item: Dict[str, object], writer_policy: str) -> Dict[str, object]:
    status_action = str(item.get("status_action"))
    reason = str(item.get("reason"))
    plan_action = str(item.get("plan_action"))
    category = _category(status_action, reason)
    enriched = dict(item)
    enriched["category"] = category
    enriched["recommendation"] = _recommendation(category, plan_action, status_action, writer_policy)
    return enriched


def _category(status_action: str, reason: str) -> str:
    if reason.startswith("writer policy "):
        return "writer_policy"
    if status_action == "conflict":
        return "conflict"
    if status_action in {"local_deleted", "remote_deleted"}:
        return "delete_review"
    if status_action in {"local_new", "remote_new"}:
        return "new_skill_review"
    return "manual_review"


def _recommendation(category: str, plan_action: str, status_action: str, writer_policy: str) -> str:
    if category == "writer_policy":
        if writer_policy == "pull-only":
            return "Review the local change. If it should be saved to the shared library, run an explicit approved-push path instead of changing the unattended OpenClaw policy."
        if writer_policy == "push-only":
            return "Review the remote change. If it should install locally, run an explicit approved pull path instead of changing the unattended writer policy."
        return f"Review whether policy {writer_policy} should allow {plan_action} for this skill."
    if category == "conflict":
        return "Materialize a conflict package and resolve local, remote, and base contents manually."
    if category == "delete_review":
        return "Materialize a tombstone and require explicit retention or delete approval before propagating deletion."
    if category == "new_skill_review":
        if status_action == "local_new":
            return "Review the local new skill before saving it to the shared library with --allow-new."
        return "Review the remote new skill before installing it locally with --allow-new."
    return "Inspect this blocked item and choose an explicit pull, push, or defer action."


def _render_markdown(report: Dict[str, object]) -> str:
    lines = [
        "# Skill Sync Blocked Report",
        "",
        f"- Created: `{report['created_at']}`",
        f"- Writer policy: `{report['writer_policy']}`",
        f"- Local root: `{report['local_root']}`",
        f"- Remote snapshot: `{report['remote_snapshot']}`",
        f"- Last applied record: `{report['last_applied_record']}`",
        f"- Total blocked: `{report['total']}`",
        "",
        "## Summary",
        "",
    ]
    summary = dict(report.get("summary", {}))
    if summary:
        for category, count in summary.items():
            lines.append(f"- `{category}`: `{count}`")
    else:
        lines.append("- No blocked items.")

    items = list(report.get("items", []))
    if items:
        lines.extend(["", "## Items", ""])
        for item in items:
            lines.extend(
                [
                    f"### {item['skill_id']}",
                    "",
                    f"- Category: `{item['category']}`",
                    f"- Status action: `{item['status_action']}`",
                    f"- Plan action: `{item['plan_action']}`",
                    f"- Reason: {item['reason']}",
                    f"- Base hash: `{item.get('base_hash')}`",
                    f"- Local hash: `{item.get('local_hash')}`",
                    f"- Remote hash: `{item.get('remote_hash')}`",
                    f"- Recommendation: {item['recommendation']}",
                    "",
                ]
            )

    return "\n".join(lines).rstrip() + "\n"
