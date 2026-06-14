from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .scanner import scan_roots


class SyncStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class SyncStateItem:
    skill_id: str
    action: str
    base_hash: Optional[str]
    local_hash: Optional[str]
    remote_hash: Optional[str]
    reason: str


def build_sync_status(
    local_root: Path,
    remote_snapshot_dir: Path,
    last_applied_record: Optional[Path] = None,
    source_name: str = "local",
) -> Dict[str, object]:
    local = _local_entries_by_skill_id(local_root, source_name)
    remote = _snapshot_entries_by_skill_id(remote_snapshot_dir)
    base = _base_entries_by_skill_id(last_applied_record) if last_applied_record else {}

    items = [
        _classify_skill(skill_id, base.get(skill_id), local.get(skill_id), remote.get(skill_id))
        for skill_id in sorted(set(base) | set(local) | set(remote))
    ]
    summary: Dict[str, int] = {}
    for item in items:
        summary[item.action] = summary.get(item.action, 0) + 1

    return {
        "local_root": str(local_root.resolve()),
        "remote_snapshot": str(remote_snapshot_dir.resolve()),
        "last_applied_record": str(last_applied_record.resolve()) if last_applied_record else None,
        "total": len(items),
        "summary": dict(sorted(summary.items())),
        "has_conflicts": any(item.action == "conflict" for item in items),
        "items": [item.__dict__ for item in items],
    }


def _classify_skill(
    skill_id: str,
    base: Optional[dict],
    local: Optional[dict],
    remote: Optional[dict],
) -> SyncStateItem:
    base_hash = _hash(base)
    local_hash = _hash(local)
    remote_hash = _hash(remote)

    if base_hash is None:
        if local_hash is None and remote_hash is not None:
            return _item(skill_id, "remote_new", base_hash, local_hash, remote_hash, "remote has a skill that has not been applied locally")
        if local_hash is not None and remote_hash is None:
            return _item(skill_id, "local_new", base_hash, local_hash, remote_hash, "local has a skill that is absent from the remote snapshot")
        if local_hash == remote_hash:
            return _item(skill_id, "same_without_base", base_hash, local_hash, remote_hash, "local and remote match, but no last-applied base is recorded")
        return _item(skill_id, "conflict", base_hash, local_hash, remote_hash, "local and remote differ without a base version")

    if local_hash == base_hash and remote_hash == base_hash:
        return _item(skill_id, "unchanged", base_hash, local_hash, remote_hash, "local and remote both match the last-applied base")
    if local_hash == remote_hash and local_hash != base_hash:
        return _item(skill_id, "already_converged", base_hash, local_hash, remote_hash, "local and remote already match each other")
    if local_hash == base_hash and remote_hash != base_hash:
        if remote_hash is None:
            return _item(skill_id, "remote_deleted", base_hash, local_hash, remote_hash, "remote deleted a skill that local has not changed")
        return _item(skill_id, "pull", base_hash, local_hash, remote_hash, "remote changed and local still matches base")
    if remote_hash == base_hash and local_hash != base_hash:
        if local_hash is None:
            return _item(skill_id, "local_deleted", base_hash, local_hash, remote_hash, "local deleted a skill that remote has not changed")
        return _item(skill_id, "push", base_hash, local_hash, remote_hash, "local changed and remote still matches base")
    if local_hash is None and remote_hash is None:
        return _item(skill_id, "deleted_both", base_hash, local_hash, remote_hash, "local and remote both deleted the base skill")
    return _item(skill_id, "conflict", base_hash, local_hash, remote_hash, "local and remote both changed away from base differently")


def _item(
    skill_id: str,
    action: str,
    base_hash: Optional[str],
    local_hash: Optional[str],
    remote_hash: Optional[str],
    reason: str,
) -> SyncStateItem:
    return SyncStateItem(skill_id, action, base_hash, local_hash, remote_hash, reason)


def _local_entries_by_skill_id(local_root: Path, source_name: str) -> Dict[str, dict]:
    summary = scan_roots([f"{source_name}={local_root}"])
    return _unique_by_skill_id(
        (
            {
                "skill_id": skill.skill_id,
                "content_hash": skill.content_hash,
                "risk_level": skill.risk_level,
                "path": str(skill.path),
            }
            for skill in summary.skills
        ),
        "local",
    )


def _snapshot_entries_by_skill_id(snapshot_dir: Path) -> Dict[str, dict]:
    index_path = snapshot_dir / "index.json"
    if not index_path.exists():
        raise SyncStateError(f"remote snapshot has no index.json: {snapshot_dir}")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    return _unique_by_skill_id(index.get("skills", []), "remote")


def _base_entries_by_skill_id(record_path: Path) -> Dict[str, dict]:
    if not record_path.exists():
        raise SyncStateError(f"last-applied record not found: {record_path}")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    applied = record.get("applied", [])
    entries = []
    for item in applied:
        content_hash = item.get("content_hash")
        if not content_hash:
            continue
        entries.append(
            {
                "skill_id": item.get("skill_id"),
                "content_hash": content_hash,
            }
        )
    if applied and not entries:
        raise SyncStateError("last-applied record does not contain content hashes; re-apply with the current sidecar first")
    return _unique_by_skill_id(entries, "base")


def _unique_by_skill_id(entries: Iterable[dict], label: str) -> Dict[str, dict]:
    result: Dict[str, dict] = {}
    duplicates: List[str] = []
    for entry in entries:
        skill_id = entry.get("skill_id")
        if not skill_id:
            continue
        skill_id = str(skill_id)
        if skill_id in result:
            duplicates.append(skill_id)
        result[skill_id] = dict(entry)
    if duplicates:
        joined = ", ".join(sorted(set(duplicates)))
        raise SyncStateError(f"duplicate {label} skill ids: {joined}")
    return result


def _hash(entry: Optional[dict]) -> Optional[str]:
    if not entry:
        return None
    value = entry.get("content_hash")
    return str(value) if value else None
