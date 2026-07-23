from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from .remote import Remote, RemoteError, join_remote_path


class CentralLifecycleError(RuntimeError):
    pass


def build_central_deprecate_preview(
    snapshot_dir: Path,
    skill_ids: Sequence[str],
    *,
    actor: str = "mac",
    reason: str = "",
) -> dict:
    normalized = _normalize_skill_ids(skill_ids)
    index = _load_index(snapshot_dir)
    by_id = _skills_by_id(index)
    missing = [skill_id for skill_id in normalized if skill_id not in by_id]
    if missing:
        raise CentralLifecycleError(f"skill is not present in central snapshot: {', '.join(missing)}")

    deprecated_at = datetime.now(timezone.utc).isoformat()
    items = []
    planned = 0
    for skill_id in normalized:
        skill = by_id[skill_id]
        current_state = _skill_lifecycle_state(skill)
        action = "noop" if current_state == "deprecated" else "mark_deprecated"
        if action == "mark_deprecated":
            planned += 1
        items.append(
            {
                "skill_id": skill_id,
                "current_state": current_state,
                "next_state": "deprecated",
                "action": action,
                "content_hash": skill.get("content_hash"),
                "archive": skill.get("archive"),
                "allowed": True,
            }
        )

    new_index = _deprecate_index(index, set(normalized), actor=actor, reason=reason, deprecated_at=deprecated_at)
    return {
        "ok": True,
        "record_type": "skill-sync-central-deprecate",
        "mode": "dry_run",
        "dry_run": True,
        "safe_to_deprecate": True,
        "skill_ids": normalized,
        "actor": actor,
        "reason": reason,
        "snapshot_id": index.get("snapshot_id"),
        "new_snapshot_id": new_index.get("snapshot_id"),
        "planned": planned,
        "items": items,
        "index_sha256": _index_sha256(index),
        "new_index_sha256": _index_sha256(new_index),
    }


def execute_central_deprecate(
    snapshot_dir: Path,
    skill_ids: Sequence[str],
    remote: Remote,
    *,
    remote_prefix: str = "",
    actor: str = "mac",
    reason: str = "",
    require_remote_match: bool = True,
) -> dict:
    preview = build_central_deprecate_preview(snapshot_dir, skill_ids, actor=actor, reason=reason)
    index = _load_index(snapshot_dir)
    if int(preview.get("planned") or 0) == 0:
        return {
            **preview,
            "mode": "deprecate",
            "dry_run": False,
            "uploaded_files": 0,
            "noop_reason": "selected skills are already deprecated",
        }
    if require_remote_match:
        _assert_remote_index_matches_cache(remote, remote_prefix, index)
    new_index = _deprecate_index(
        index,
        set(preview["skill_ids"]),
        actor=actor,
        reason=reason,
        deprecated_at=datetime.now(timezone.utc).isoformat(),
    )
    data = json.dumps(new_index, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    remote.put_bytes(join_remote_path(remote_prefix, "index.json"), data)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "index.json").write_bytes(data)
    return {
        **preview,
        "mode": "deprecate",
        "dry_run": False,
        "snapshot_id": index.get("snapshot_id"),
        "new_snapshot_id": new_index.get("snapshot_id"),
        "new_index_sha256": _index_sha256(new_index),
        "uploaded_files": 1,
    }


def _load_index(snapshot_dir: Path) -> dict:
    index_path = snapshot_dir.expanduser() / "index.json"
    if not index_path.exists():
        raise CentralLifecycleError(f"central snapshot cache has no index.json: {index_path}")
    try:
        value = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CentralLifecycleError(f"cannot read central snapshot index: {exc}") from exc
    if not isinstance(value, dict):
        raise CentralLifecycleError("central snapshot index must be a JSON object")
    return value


def _skills_by_id(index: dict) -> dict[str, dict]:
    result = {}
    for skill in index.get("skills", []):
        if isinstance(skill, dict) and skill.get("skill_id"):
            result[str(skill["skill_id"])] = skill
    return result


def _deprecate_index(index: dict, skill_ids: set[str], *, actor: str, reason: str, deprecated_at: str) -> dict:
    skills = []
    for skill in index.get("skills", []):
        if not isinstance(skill, dict):
            continue
        updated = dict(skill)
        if str(updated.get("skill_id")) in skill_ids:
            if _skill_lifecycle_state(updated) != "deprecated":
                lifecycle = dict(updated.get("lifecycle") or {})
                lifecycle.update(
                    {
                        "state": "deprecated",
                        "deprecated_at": deprecated_at,
                        "deprecated_by": actor,
                    }
                )
                if reason:
                    lifecycle["reason"] = reason
                updated["lifecycle"] = lifecycle
        skills.append(updated)
    return {
        **index,
        "snapshot_id": f"central-deprecate-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "total": len(skills),
        "skills": skills,
    }


def _assert_remote_index_matches_cache(remote: Remote, remote_prefix: str, cache_index: dict) -> None:
    try:
        remote_index = json.loads(remote.get_bytes(join_remote_path(remote_prefix, "index.json")).decode("utf-8"))
    except (json.JSONDecodeError, RemoteError) as exc:
        raise CentralLifecycleError(f"cannot verify remote snapshot before deprecate: {exc}") from exc
    if _index_sha256(remote_index) != _index_sha256(cache_index):
        raise CentralLifecycleError("remote index changed since the local cache was pulled; refresh before deprecating")


def _skill_lifecycle_state(skill: dict) -> str:
    lifecycle = skill.get("lifecycle")
    if isinstance(lifecycle, dict) and lifecycle.get("state"):
        return str(lifecycle["state"])
    if skill.get("state"):
        return str(skill["state"])
    return "published"


def _index_sha256(index: dict) -> str:
    data = json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _normalize_skill_ids(skill_ids: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in skill_ids:
        skill_id = str(raw).strip()
        if not skill_id:
            continue
        if skill_id not in seen:
            result.append(skill_id)
            seen.add(skill_id)
    if not result:
        raise CentralLifecycleError("at least one skill id is required")
    return result
