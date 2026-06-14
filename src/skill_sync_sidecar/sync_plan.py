from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class SyncPlanItem:
    skill_id: str
    status_action: str
    plan_action: str
    allowed: bool
    reason: str
    base_hash: str | None
    local_hash: str | None
    remote_hash: str | None


def build_sync_plan(
    status: Dict[str, object],
    allow_new: bool = False,
    allow_delete: bool = False,
) -> Dict[str, object]:
    items = [_plan_item(item, allow_new=allow_new, allow_delete=allow_delete) for item in status.get("items", [])]
    summary: Dict[str, int] = {}
    for item in items:
        summary[item.plan_action] = summary.get(item.plan_action, 0) + 1

    blocked = [item for item in items if not item.allowed]
    return {
        "dry_run": True,
        "local_root": status.get("local_root"),
        "remote_snapshot": status.get("remote_snapshot"),
        "last_applied_record": status.get("last_applied_record"),
        "total": len(items),
        "summary": dict(sorted(summary.items())),
        "allowed": sum(1 for item in items if item.allowed),
        "blocked": len(blocked),
        "safe_to_apply": not blocked,
        "items": [item.__dict__ for item in items],
    }


def _plan_item(item: dict, allow_new: bool, allow_delete: bool) -> SyncPlanItem:
    status_action = str(item.get("action"))
    skill_id = str(item.get("skill_id"))
    base_hash = item.get("base_hash")
    local_hash = item.get("local_hash")
    remote_hash = item.get("remote_hash")

    if status_action in {"unchanged", "already_converged", "deleted_both", "same_without_base"}:
        return _item(skill_id, status_action, "noop", True, "no sync action needed", base_hash, local_hash, remote_hash)
    if status_action == "pull":
        return _item(skill_id, status_action, "pull", True, "remote changed and local is still at base", base_hash, local_hash, remote_hash)
    if status_action == "push":
        return _item(skill_id, status_action, "push", True, "local changed and remote is still at base", base_hash, local_hash, remote_hash)
    if status_action == "remote_new":
        return _item(
            skill_id,
            status_action,
            "pull_new" if allow_new else "blocked",
            allow_new,
            "remote new skill requires --allow-new before auto-pull",
            base_hash,
            local_hash,
            remote_hash,
        )
    if status_action == "local_new":
        return _item(
            skill_id,
            status_action,
            "push_new" if allow_new else "blocked",
            allow_new,
            "local new skill requires --allow-new before auto-push",
            base_hash,
            local_hash,
            remote_hash,
        )
    if status_action == "remote_deleted":
        return _item(
            skill_id,
            status_action,
            "delete_local" if allow_delete else "blocked",
            allow_delete,
            "remote deletion requires --allow-delete before local deletion",
            base_hash,
            local_hash,
            remote_hash,
        )
    if status_action == "local_deleted":
        return _item(
            skill_id,
            status_action,
            "delete_remote" if allow_delete else "blocked",
            allow_delete,
            "local deletion requires --allow-delete before remote deletion",
            base_hash,
            local_hash,
            remote_hash,
        )
    return _item(skill_id, status_action, "blocked", False, "conflict or unknown state requires manual resolution", base_hash, local_hash, remote_hash)


def _item(
    skill_id: str,
    status_action: str,
    plan_action: str,
    allowed: bool,
    reason: str,
    base_hash: str | None,
    local_hash: str | None,
    remote_hash: str | None,
) -> SyncPlanItem:
    return SyncPlanItem(skill_id, status_action, plan_action, allowed, reason, base_hash, local_hash, remote_hash)
