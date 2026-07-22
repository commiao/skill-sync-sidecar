from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional, Sequence

from .apply import ApplyError, build_apply_plan, execute_apply_plan
from .approved_push import _write_sync_base_record
from .stage import StageError, stage_snapshot


class RestoreError(RuntimeError):
    pass


def restore_from_central(
    local_root: Path,
    remote_snapshot_dir: Path,
    skill_ids: Sequence[str],
    *,
    target: str = "mixed-scope-root",
    base_record_out: Optional[Path] = None,
    remote_prefix: str = "",
    yes: bool = False,
) -> dict:
    normalized = _normalize_skill_ids(skill_ids)
    snapshot_index = _load_snapshot_index(remote_snapshot_dir)
    remote_skill_ids = {
        str(skill.get("skill_id"))
        for skill in snapshot_index.get("skills", [])
        if isinstance(skill, dict) and skill.get("skill_id")
    }
    missing = [skill_id for skill_id in normalized if skill_id not in remote_skill_ids]
    if missing:
        raise RestoreError(f"skill is not present in central snapshot: {', '.join(missing)}")

    with TemporaryDirectory(prefix="skill-sync-central-restore-") as tmp:
        stage_index = stage_snapshot(remote_snapshot_dir, Path(tmp), clean=True)
        staged_dir = Path(str(stage_index["skills"][0]["output_path"])).parents[1] if stage_index.get("skills") else Path(tmp)
        plan = build_apply_plan(
            staged_dir,
            target,
            target_root=local_root,
            skill_ids=normalized,
        )
        allowed = [item for item in plan.get("items", []) if item.get("allowed")]
        if len(allowed) != len(normalized):
            blocked = [
                {
                    "skill_id": item.get("skill_id"),
                    "reason": item.get("reason"),
                }
                for item in plan.get("items", [])
                if item.get("skill_id") in set(normalized) and not item.get("allowed")
            ]
            raise RestoreError(f"selected skill is not restorable into {target}: {blocked}")

        if not yes:
            return {
                "ok": True,
                "record_type": "skill-sync-central-restore",
                "mode": "dry_run",
                "dry_run": True,
                "safe_to_restore": True,
                "skill_ids": normalized,
                "target": target,
                "target_root": str(local_root.expanduser().resolve()),
                "snapshot_id": snapshot_index.get("snapshot_id"),
                "planned": len(allowed),
                "items": allowed,
            }

        result = execute_apply_plan(plan)
        base_record_path = _write_sync_base_record(
            local_root,
            snapshot_index,
            remote_prefix,
            out=base_record_out,
        ) if base_record_out else None
        return {
            "ok": True,
            "record_type": "skill-sync-central-restore",
            "mode": "restore",
            "dry_run": False,
            "safe_to_restore": True,
            "skill_ids": normalized,
            "target": target,
            "target_root": str(local_root.expanduser().resolve()),
            "snapshot_id": snapshot_index.get("snapshot_id"),
            "restored": result.get("total_applied", 0),
            "apply_result": result,
            "base_record_path": base_record_path,
        }


def _load_snapshot_index(remote_snapshot_dir: Path) -> dict:
    index_path = remote_snapshot_dir.expanduser() / "index.json"
    if not index_path.exists():
        raise RestoreError(f"central snapshot cache has no index.json: {remote_snapshot_dir}")
    try:
        return json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RestoreError(f"cannot read central snapshot index: {exc}") from exc


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
        raise RestoreError("at least one skill id is required")
    return result
