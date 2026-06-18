from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Dict, List, Optional, Set

from .apply import execute_apply_plan
from .remote import Remote, join_remote_path, upload_snapshot
from .scanner import scan_roots
from .stage import stage_snapshot
from .snapshot import write_snapshot
from .sync_plan import build_sync_plan
from .sync_state import build_sync_status


class SyncApplyError(RuntimeError):
    pass


PULL_ACTIONS = {"pull", "pull_new"}
PUSH_ACTIONS = {"push", "push_new"}
EXECUTABLE_ACTIONS = {*PULL_ACTIONS, *PUSH_ACTIONS}
SUPPORTED_ACTIONS = {"noop", *EXECUTABLE_ACTIONS}
TARGET_SCOPES = {
    "cc-switch-global": "global",
    "codex-project": "project",
    "mixed-scope-root": None,
}
ALLOWED_SCOPES = {"global", "project"}


def build_sync_apply_preview(
    local_root: Path,
    remote_snapshot_dir: Path,
    last_applied_record: Optional[Path] = None,
    allow_new: bool = False,
    allow_delete: bool = False,
    writer_policy: str = "push-pull",
    target: str = "cc-switch-global",
) -> Dict[str, object]:
    expected_scope = _expected_scope(target)
    allowed_scopes = _allowed_scopes(target)
    status = build_sync_status(local_root, remote_snapshot_dir, last_applied_record)
    plan = build_sync_plan(status, allow_new=allow_new, allow_delete=allow_delete, writer_policy=writer_policy)
    remote_entries = _remote_entries_by_skill_id(remote_snapshot_dir)
    items: List[Dict[str, object]] = []
    executable = 0
    unsupported = 0

    for item in plan["items"]:
        enriched = dict(item)
        skill_id = str(item["skill_id"])
        action = str(item["plan_action"])
        remote_entry = remote_entries.get(skill_id, {})
        scope = str(remote_entry.get("scope") or "global")
        enriched["remote_scope"] = scope
        enriched["sync_apply_action"] = "none"
        enriched["sync_apply_supported"] = True
        enriched["sync_apply_reason"] = item["reason"]

        if not item["allowed"]:
            enriched["sync_apply_supported"] = False
        elif action in PULL_ACTIONS:
            if scope not in allowed_scopes:
                enriched["sync_apply_supported"] = False
                enriched["sync_apply_reason"] = f"{scope}-scoped skills are not installed into {target}"
                unsupported += 1
            else:
                enriched["sync_apply_action"] = "install_or_replace"
                executable += 1
        elif action in PUSH_ACTIONS:
            enriched["sync_apply_action"] = "upload_snapshot"
            executable += 1
        elif action not in SUPPORTED_ACTIONS:
            enriched["sync_apply_supported"] = False
            enriched["sync_apply_reason"] = "sync-apply currently supports pull/pull_new/push/push_new/noop only"
            unsupported += 1

        items.append(enriched)

    return {
        **plan,
        "dry_run": True,
        "mode": "two-way-safe",
        "target": target,
        "expected_scope": expected_scope or "global,project",
        "executable": executable,
        "unsupported": unsupported,
        "supported_to_apply": plan["blocked"] == 0 and unsupported == 0,
        "items": items,
    }


def execute_sync_apply(
    local_root: Path,
    remote_snapshot_dir: Path,
    last_applied_record: Optional[Path] = None,
    allow_new: bool = False,
    allow_delete: bool = False,
    writer_policy: str = "push-pull",
    remote: Optional[Remote] = None,
    remote_prefix: str = "",
    target: str = "cc-switch-global",
    backup_root: Optional[Path] = None,
) -> Dict[str, object]:
    preview = build_sync_apply_preview(
        local_root,
        remote_snapshot_dir,
        last_applied_record=last_applied_record,
        allow_new=allow_new,
        allow_delete=allow_delete,
        writer_policy=writer_policy,
        target=target,
    )
    if preview["blocked"]:
        raise SyncApplyError(f"sync plan has {preview['blocked']} blocked item(s); inspect sync-plan first")
    if preview["unsupported"]:
        raise SyncApplyError("sync-apply can currently execute only pull/pull_new/push/push_new/noop actions")

    pull_count = _count_actions(preview, PULL_ACTIONS)
    push_count = _count_actions(preview, PUSH_ACTIONS)
    push_skill_ids = _action_skill_ids(preview, PUSH_ACTIONS)
    if push_count and remote is None:
        raise SyncApplyError("push actions require a remote destination")
    if push_count and remote is not None:
        _assert_remote_matches_cache(remote, remote_prefix, remote_snapshot_dir)

    apply_result = None
    if pull_count:
        with TemporaryDirectory(prefix="skill-sync-apply-stage-") as tmp:
            stage_index = stage_snapshot(remote_snapshot_dir, Path(tmp), clean=True)
            staged_by_skill_id = {str(skill["skill_id"]): dict(skill) for skill in stage_index.get("skills", [])}
            apply_plan = _build_pull_apply_plan(local_root, remote_snapshot_dir, stage_index, staged_by_skill_id, preview, target, backup_root)
            apply_result = execute_apply_plan(apply_plan)

    upload_result = None
    base_record_path = None
    if push_count and remote is not None:
        upload_result = _push_local_snapshot(local_root, remote, remote_prefix, push_skill_ids)
        base_record_path = _write_sync_base_record(local_root, upload_result["snapshot_index"], remote_prefix)

    if not pull_count and not push_count:
        return {
            "status": "complete",
            "dry_run": False,
            "mode": "two-way-safe",
            "applied": 0,
            "uploaded": 0,
            "sync_plan": preview,
            "apply_result": None,
            "upload_result": None,
            "base_record_path": None,
        }

    return {
        "status": "complete",
        "dry_run": False,
        "mode": "two-way-safe",
        "applied": apply_result.get("total_applied", 0) if apply_result else 0,
        "uploaded": upload_result.get("files", 0) if upload_result else 0,
        "sync_plan": preview,
        "apply_result": apply_result,
        "upload_result": upload_result,
        "base_record_path": base_record_path,
    }


def _build_pull_apply_plan(
    local_root: Path,
    remote_snapshot_dir: Path,
    stage_index: Dict[str, object],
    staged_by_skill_id: Dict[str, dict],
    preview: Dict[str, object],
    target: str,
    backup_root_override: Optional[Path] = None,
) -> Dict[str, object]:
    allowed_scopes = _allowed_scopes(target)
    apply_id = _timestamp_id()
    backup_root = (backup_root_override or local_root / ".skill-sync-backups") / apply_id
    items: List[Dict[str, object]] = []

    for item in preview["items"]:
        if item["plan_action"] not in PULL_ACTIONS:
            continue
        skill_id = str(item["skill_id"])
        staged = staged_by_skill_id.get(skill_id)
        if staged is None:
            raise SyncApplyError(f"remote skill was not present after staging: {skill_id}")
        staged_scope = str(staged.get("scope") or "global")
        if staged_scope not in allowed_scopes:
            raise SyncApplyError(f"refusing to install {staged.get('scope')}-scoped skill into {target}: {skill_id}")
        items.append(
            {
                "key": staged.get("key"),
                "skill_id": skill_id,
                "content_hash": staged.get("content_hash") or item.get("remote_hash") or "",
                "source_path": staged.get("output_path"),
                "target_path": str(local_root / skill_id),
                "backup_path": str(backup_root / skill_id),
                "action": "install_or_replace",
                "scope": staged_scope,
                "allowed": True,
                "reason": item.get("reason"),
            }
        )

    return {
        "apply_id": apply_id,
        "target": target,
        "target_root": str(local_root.resolve()),
        "backup_root": str(backup_root.resolve()),
        "snapshot_id": stage_index.get("snapshot_id"),
        "staged_snapshot": str((Path(str(stage_index["skills"][0]["output_path"])).parents[1]).resolve()) if items else str(remote_snapshot_dir.resolve()),
        "dry_run": False,
        "total": len(items),
        "allowed": len(items),
        "skipped": 0,
        "items": items,
    }


def _remote_entries_by_skill_id(snapshot_dir: Path) -> Dict[str, dict]:
    index_path = snapshot_dir / "index.json"
    if not index_path.exists():
        return {}
    index = json.loads(index_path.read_text(encoding="utf-8"))
    entries = {}
    for skill in index.get("skills", []):
        skill_id = skill.get("skill_id")
        if skill_id:
            entries[str(skill_id)] = dict(skill)
    return entries


def _count_actions(preview: Dict[str, object], actions: set[str]) -> int:
    return sum(1 for item in preview["items"] if item["plan_action"] in actions and item["allowed"])


def _action_skill_ids(preview: Dict[str, object], actions: set[str]) -> Set[str]:
    return {str(item["skill_id"]) for item in preview["items"] if item["plan_action"] in actions and item["allowed"]}


def _assert_remote_matches_cache(remote: Remote, remote_prefix: str, remote_snapshot_dir: Path) -> None:
    cache_index_path = remote_snapshot_dir / "index.json"
    if not cache_index_path.exists():
        raise SyncApplyError(f"remote snapshot cache has no index.json: {remote_snapshot_dir}")
    cache_index = json.loads(cache_index_path.read_text(encoding="utf-8"))
    remote_index = json.loads(remote.get_bytes(join_remote_path(remote_prefix, "index.json")).decode("utf-8"))
    if _hashes_by_skill_id(remote_index) != _hashes_by_skill_id(cache_index):
        raise SyncApplyError("remote changed since the local cache was pulled; refresh pull-cache before pushing")


def _push_local_snapshot(local_root: Path, remote: Remote, remote_prefix: str, push_skill_ids: Optional[Set[str]] = None) -> Dict[str, object]:
    with TemporaryDirectory(prefix="skill-sync-push-snapshot-") as tmp:
        snapshot_dir = Path(tmp) / "snapshot"
        index = write_snapshot(scan_roots([f"cc-switch={local_root}"]), snapshot_dir, _timestamp_id())
        include_paths = _archive_paths_for_skill_ids(index, push_skill_ids) if push_skill_ids else None
        plan = upload_snapshot(
            snapshot_dir,
            remote,
            remote_prefix,
            include_paths=include_paths,
            skip_existing_archives=push_skill_ids is None,
        )
        return {
            "snapshot_id": index.get("snapshot_id"),
            "total": index.get("total"),
            "files": len(plan.files),
            "bytes": plan.total_bytes,
            "remote_prefix": remote_prefix,
            "snapshot_index": index,
        }


def _archive_paths_for_skill_ids(index: Dict[str, object], skill_ids: Optional[Set[str]]) -> Set[str]:
    if not skill_ids:
        return set()
    paths = set()
    for skill in index.get("skills", []):
        if not isinstance(skill, dict):
            continue
        if str(skill.get("skill_id")) in skill_ids and skill.get("archive"):
            paths.add(str(skill["archive"]))
    return paths


def _write_sync_base_record(local_root: Path, snapshot_index: Dict[str, object], remote_prefix: str) -> str:
    sync_id = _timestamp_id()
    record_dir = local_root / ".skill-sync-bases"
    record_dir.mkdir(parents=True, exist_ok=True)
    record_path = record_dir / f"{sync_id}.json"
    record = {
        "protocol_version": 0,
        "record_type": "skill-sync-base",
        "sync_id": sync_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target_root": str(local_root.resolve()),
        "remote_prefix": remote_prefix,
        "snapshot_id": snapshot_index.get("snapshot_id"),
        "applied": [
            {
                "skill_id": skill.get("skill_id"),
                "content_hash": skill.get("content_hash"),
            }
            for skill in snapshot_index.get("skills", [])
            if skill.get("skill_id") and skill.get("content_hash")
        ],
    }
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(record_path)


def _hashes_by_skill_id(index: Dict[str, object]) -> Dict[str, str]:
    return {
        str(skill["skill_id"]): str(skill["content_hash"])
        for skill in index.get("skills", [])
        if skill.get("skill_id") and skill.get("content_hash")
    }


def _expected_scope(target: str) -> Optional[str]:
    try:
        return TARGET_SCOPES[target]
    except KeyError as exc:
        raise SyncApplyError(f"unsupported sync-apply target: {target}") from exc


def _allowed_scopes(target: str) -> Set[str]:
    expected = _expected_scope(target)
    if expected is None:
        return set(ALLOWED_SCOPES)
    return {expected}


def _timestamp_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
