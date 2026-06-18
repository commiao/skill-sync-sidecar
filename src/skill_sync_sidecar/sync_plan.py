from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


WRITER_POLICIES = ("push-pull", "pull-only", "push-only", "no-writes")
POLICY_ALLOWED_ACTIONS = {
    "push-pull": {"noop", "pull", "pull_new", "push", "push_new", "delete_local", "delete_remote", "blocked"},
    "pull-only": {"noop", "pull", "pull_new", "blocked"},
    "push-only": {"noop", "push", "push_new", "blocked"},
    "no-writes": {"noop", "blocked"},
}


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
    writer_policy: str = "push-pull",
) -> Dict[str, object]:
    if writer_policy not in POLICY_ALLOWED_ACTIONS:
        raise ValueError(f"unsupported writer policy: {writer_policy}")
    raw_items = [_plan_item(item, allow_new=allow_new, allow_delete=allow_delete) for item in status.get("items", [])]
    items = [_apply_writer_policy(item, writer_policy) for item in raw_items]
    summary: Dict[str, int] = {}
    for item in items:
        summary[item.plan_action] = summary.get(item.plan_action, 0) + 1

    blocked = [item for item in items if not item.allowed]
    return {
        "dry_run": True,
        "local_root": status.get("local_root"),
        "remote_snapshot": status.get("remote_snapshot"),
        "last_applied_record": status.get("last_applied_record"),
        "writer_policy": writer_policy,
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


def _apply_writer_policy(item: SyncPlanItem, writer_policy: str) -> SyncPlanItem:
    if not item.allowed:
        return item
    if item.plan_action in POLICY_ALLOWED_ACTIONS[writer_policy]:
        return item
    return SyncPlanItem(
        item.skill_id,
        item.status_action,
        "blocked",
        False,
        f"writer policy {writer_policy} blocks {item.plan_action}",
        item.base_hash,
        item.local_hash,
        item.remote_hash,
    )


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
