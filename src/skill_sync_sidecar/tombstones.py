from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Dict, Optional

from .report_retention import atomic_json, records, report_lock, signature, _prune_locked
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
    with report_lock(out_dir):
        return _build_locked(local_root, remote_snapshot_dir, out_dir, last_applied_record)


def _build_locked(local_root, remote_snapshot_dir, out_dir, last_applied_record):
    status = build_sync_status(local_root, remote_snapshot_dir, last_applied_record)
    delete_items = [item for item in status["items"] if item["action"] in DELETE_ACTIONS]
    existing = {}
    for path, metadata in sorted(records(out_dir), key=lambda pair: pair[0].stat().st_mtime):
        if (path / 'base.json').is_file() and ((path / 'local').is_dir() or (path / 'remote').is_dir()):
            existing[signature(metadata)] = (path, metadata)
    tombstones = []
    pending = []
    reused = 0
    for item in delete_items:
        metadata = _metadata(item, local_root, remote_snapshot_dir, last_applied_record)
        previous = existing.get(signature(metadata))
        if previous is None:
            pending.append(item)
        else:
            path, old = previous
            tombstones.append(_index_entry(path, old))
            reused += 1
    with TemporaryDirectory(prefix="skill-sync-tombstone-stage-") as tmp:
        staged_by_skill_id = {}
        if any(item["remote_hash"] for item in pending):
            stage_index = stage_snapshot(remote_snapshot_dir, Path(tmp), clean=True)
            staged_by_skill_id = {str(skill["skill_id"]): dict(skill) for skill in stage_index.get("skills", [])}
        for item in pending:
            tombstones.append(_write_tombstone(dict(item), local_root, remote_snapshot_dir,
                                              out_dir, staged_by_skill_id, last_applied_record))
    result = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "local_root": str(local_root.resolve()),
        "remote_snapshot": str(remote_snapshot_dir.resolve()),
        "last_applied_record": str(last_applied_record.resolve()) if last_applied_record else None,
        "total_tombstones": len(tombstones), "reused": reused,
        "out": str(out_dir.resolve()), "tombstones": tombstones,
    }
    atomic_json(out_dir / "tombstone-index.json", result)
    result["retention"] = _prune_locked(out_dir)
    atomic_json(out_dir / "retention-status.json", result["retention"])
    return result


def _index_entry(path, metadata):
    return {"skill_id": metadata["skill_id"], "action": metadata["status_action"],
            "propagation": metadata["propagation"], "path": str(path.resolve()),
            "base_hash": metadata.get("base_hash"), "local_hash": metadata.get("local_hash"),
            "remote_hash": metadata.get("remote_hash")}


def _metadata(item, local_root, remote_snapshot_dir, last_applied_record):
    action = str(item["action"])
    return {
        "protocol_version": 0, "record_type": "skill-sync-tombstone",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "skill_id": str(item["skill_id"]), "status_action": action,
        "propagation": "deprecate_remote" if action == "local_deleted" else "delete_local",
        "reason": item.get("reason"), "base_hash": item.get("base_hash"),
        "local_hash": item.get("local_hash"), "remote_hash": item.get("remote_hash"),
        "local_root": str(local_root.resolve()), "remote_snapshot": str(remote_snapshot_dir.resolve()),
        "last_applied_record": str(last_applied_record.resolve()) if last_applied_record else None,
        "state": "pending",
        "note": "Device-side deletion can only request restore or central deprecate review; it must not physically delete central files.",
    }


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

    metadata = _metadata(item, local_root, remote_snapshot_dir, last_applied_record)
    try:
        _write_local_material(tombstone_dir, local_root, skill_id)
        _write_remote_material(tombstone_dir, staged_by_skill_id, skill_id)
        _write_base_material(tombstone_dir, item, last_applied_record)
        # Metadata is the completion marker, published only after all material exists.
        atomic_json(tombstone_dir / "tombstone.json", metadata)
    except Exception:
        shutil.rmtree(tombstone_dir)
        raise
    return _index_entry(tombstone_dir, metadata)


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
        "note": "Tombstones are non-destructive markers. Device-side local deletion may lead to restore or central deprecate review, never automatic central file deletion.",
    }
    (tombstone_dir / "base.json").write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _timestamp_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
