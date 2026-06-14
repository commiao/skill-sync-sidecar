from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Dict, Optional

from .stage import sanitize_component, stage_snapshot
from .sync_state import build_sync_status


class TombstoneError(RuntimeError):
    pass


DELETE_ACTIONS = {"local_deleted", "remote_deleted"}


def build_tombstones(
    local_root: Path,
    remote_snapshot_dir: Path,
    out_dir: Path,
    last_applied_record: Optional[Path] = None,
) -> Dict[str, object]:
    status = build_sync_status(local_root, remote_snapshot_dir, last_applied_record)
    delete_items = [item for item in status["items"] if item["action"] in DELETE_ACTIONS]
    out_dir.mkdir(parents=True, exist_ok=True)

    tombstones = []
    with TemporaryDirectory(prefix="skill-sync-tombstone-stage-", dir="/private/tmp") as tmp:
        staged_by_skill_id = {}
        if any(item["remote_hash"] for item in delete_items):
            stage_index = stage_snapshot(remote_snapshot_dir, Path(tmp), clean=True)
            staged_by_skill_id = {str(skill["skill_id"]): dict(skill) for skill in stage_index.get("skills", [])}

        for item in delete_items:
            tombstones.append(
                _write_tombstone(
                    dict(item),
                    local_root,
                    remote_snapshot_dir,
                    out_dir,
                    staged_by_skill_id,
                    last_applied_record,
                )
            )

    result = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "local_root": str(local_root.resolve()),
        "remote_snapshot": str(remote_snapshot_dir.resolve()),
        "last_applied_record": str(last_applied_record.resolve()) if last_applied_record else None,
        "total_tombstones": len(tombstones),
        "out": str(out_dir.resolve()),
        "tombstones": tombstones,
    }
    (out_dir / "tombstone-index.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def _write_tombstone(
    item: dict,
    local_root: Path,
    remote_snapshot_dir: Path,
    out_dir: Path,
    staged_by_skill_id: Dict[str, dict],
    last_applied_record: Optional[Path],
) -> Dict[str, object]:
    skill_id = str(item["skill_id"])
    action = str(item["action"])
    tombstone_dir = out_dir / f"{_timestamp_id()}-{sanitize_component(skill_id)}-{action}"
    if tombstone_dir.exists():
        raise TombstoneError(f"tombstone already exists: {tombstone_dir}")
    tombstone_dir.mkdir(parents=True)

    propagation = "delete_remote" if action == "local_deleted" else "delete_local"
    metadata = {
        "protocol_version": 0,
        "record_type": "skill-sync-tombstone",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "skill_id": skill_id,
        "status_action": action,
        "propagation": propagation,
        "reason": item.get("reason"),
        "base_hash": item.get("base_hash"),
        "local_hash": item.get("local_hash"),
        "remote_hash": item.get("remote_hash"),
        "local_root": str(local_root.resolve()),
        "remote_snapshot": str(remote_snapshot_dir.resolve()),
        "last_applied_record": str(last_applied_record.resolve()) if last_applied_record else None,
        "state": "pending",
    }
    (tombstone_dir / "tombstone.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_local_material(tombstone_dir, local_root, skill_id)
    _write_remote_material(tombstone_dir, staged_by_skill_id, skill_id)
    _write_base_material(tombstone_dir, item, last_applied_record)

    return {
        "skill_id": skill_id,
        "action": action,
        "propagation": propagation,
        "path": str(tombstone_dir.resolve()),
        "base_hash": item.get("base_hash"),
        "local_hash": item.get("local_hash"),
        "remote_hash": item.get("remote_hash"),
    }


def _write_local_material(tombstone_dir: Path, local_root: Path, skill_id: str) -> None:
    local_path = local_root / skill_id
    if local_path.exists():
        shutil.copytree(local_path, tombstone_dir / "local")
        return
    (tombstone_dir / "local.json").write_text(
        json.dumps({"state": "absent", "path": str(local_path)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_remote_material(tombstone_dir: Path, staged_by_skill_id: Dict[str, dict], skill_id: str) -> None:
    staged = staged_by_skill_id.get(skill_id)
    if staged and staged.get("output_path") and Path(str(staged["output_path"])).exists():
        shutil.copytree(Path(str(staged["output_path"])), tombstone_dir / "remote")
        return
    (tombstone_dir / "remote.json").write_text(
        json.dumps({"state": "absent", "skill_id": skill_id}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_base_material(tombstone_dir: Path, item: dict, last_applied_record: Optional[Path]) -> None:
    base = {
        "state": "metadata_only",
        "skill_id": item.get("skill_id"),
        "base_hash": item.get("base_hash"),
        "last_applied_record": str(last_applied_record.resolve()) if last_applied_record else None,
        "note": "Tombstones are non-destructive markers. Delete execution must use a later explicit retention/rollback gate.",
    }
    (tombstone_dir / "base.json").write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _timestamp_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
