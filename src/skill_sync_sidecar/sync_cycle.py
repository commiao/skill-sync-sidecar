from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from .conflicts import build_conflict_packages
from .remote import Remote, download_snapshot
from .sync_apply import execute_sync_apply
from .sync_plan import build_sync_plan
from .sync_state import build_sync_status
from .tombstones import build_tombstones


class SyncCycleError(RuntimeError):
    pass


DELETE_PLAN_ACTIONS = {"delete_local", "delete_remote"}


def run_sync_cycle(
    local_root: Path,
    remote: Remote,
    remote_prefix: str,
    cache_dir: Path,
    work_dir: Path,
    last_applied_record: Optional[Path] = None,
    allow_new: bool = False,
    allow_delete: bool = False,
    dry_run: bool = True,
    target: str = "cc-switch-global",
    backup_root: Optional[Path] = None,
) -> Dict[str, object]:
    snapshot_index = download_snapshot(remote, cache_dir, remote_prefix)
    status = build_sync_status(local_root, cache_dir, last_applied_record)
    plan = build_sync_plan(status, allow_new=allow_new, allow_delete=allow_delete)
    conflicts = _build_conflicts_if_needed(status, local_root, cache_dir, work_dir, last_applied_record)
    tombstones = _build_tombstones_if_needed(status, local_root, cache_dir, work_dir, last_applied_record)
    delete_actions = _count_delete_plan_actions(plan)

    apply_result = None
    cycle_status = "dry_run" if dry_run else "blocked"
    if not dry_run and plan["blocked"] == 0 and delete_actions == 0 and not conflicts:
        apply_result = execute_sync_apply(
            local_root,
            cache_dir,
            last_applied_record=last_applied_record,
            allow_new=allow_new,
            allow_delete=allow_delete,
            remote=remote,
            remote_prefix=remote_prefix,
            target=target,
            backup_root=backup_root,
        )
        cycle_status = "complete"

    return {
        "status": cycle_status,
        "dry_run": dry_run,
        "local_root": str(local_root.resolve()),
        "remote_prefix": remote_prefix,
        "cache_dir": str(cache_dir.resolve()),
        "work_dir": str(work_dir.resolve()),
        "snapshot_id": snapshot_index.get("snapshot_id"),
        "snapshot_total": snapshot_index.get("total"),
        "sync_status": status,
        "sync_plan": plan,
        "conflicts": conflicts,
        "tombstones": tombstones,
        "delete_actions": delete_actions,
        "apply_result": apply_result,
        "reason": _reason(dry_run, plan, conflicts, tombstones, delete_actions),
    }


def _build_conflicts_if_needed(
    status: Dict[str, object],
    local_root: Path,
    cache_dir: Path,
    work_dir: Path,
    last_applied_record: Optional[Path],
) -> Optional[Dict[str, object]]:
    if not status.get("has_conflicts"):
        return None
    return build_conflict_packages(local_root, cache_dir, work_dir / "conflicts", last_applied_record)


def _build_tombstones_if_needed(
    status: Dict[str, object],
    local_root: Path,
    cache_dir: Path,
    work_dir: Path,
    last_applied_record: Optional[Path],
) -> Optional[Dict[str, object]]:
    if not any(item["action"] in {"local_deleted", "remote_deleted"} for item in status.get("items", [])):
        return None
    return build_tombstones(local_root, cache_dir, work_dir / "tombstones", last_applied_record)


def _count_delete_plan_actions(plan: Dict[str, object]) -> int:
    return sum(1 for item in plan.get("items", []) if item.get("plan_action") in DELETE_PLAN_ACTIONS and item.get("allowed"))


def _reason(
    dry_run: bool,
    plan: Dict[str, object],
    conflicts: Optional[Dict[str, object]],
    tombstones: Optional[Dict[str, object]],
    delete_actions: int,
) -> str:
    if dry_run:
        return "dry-run only; no files changed"
    if conflicts and conflicts.get("total_conflicts", 0):
        return "conflicts were materialized for manual review"
    if delete_actions:
        return "delete propagation is staged as tombstones and not executed automatically"
    if plan.get("blocked"):
        return "sync plan has blocked items"
    if tombstones and tombstones.get("total_tombstones", 0):
        return "delete tombstones were materialized for manual review"
    return "sync actions applied"


__all__ = ["SyncCycleError", "run_sync_cycle"]
