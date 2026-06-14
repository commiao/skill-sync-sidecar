from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Dict, Optional

from .stage import sanitize_component, stage_snapshot
from .sync_state import build_sync_status


class ConflictPackageError(RuntimeError):
    pass


def build_conflict_packages(
    local_root: Path,
    remote_snapshot_dir: Path,
    out_dir: Path,
    last_applied_record: Optional[Path] = None,
) -> Dict[str, object]:
    status = build_sync_status(local_root, remote_snapshot_dir, last_applied_record)
    conflicts = [item for item in status["items"] if item["action"] == "conflict"]
    out_dir.mkdir(parents=True, exist_ok=True)

    packages = []
    with TemporaryDirectory(prefix="skill-sync-conflict-stage-", dir="/private/tmp") as tmp:
        staged_by_skill_id = {}
        if conflicts:
            stage_index = stage_snapshot(remote_snapshot_dir, Path(tmp), clean=True)
            staged_by_skill_id = {str(skill["skill_id"]): dict(skill) for skill in stage_index.get("skills", [])}

        for item in conflicts:
            package = _write_conflict_package(
                dict(item),
                local_root,
                remote_snapshot_dir,
                out_dir,
                staged_by_skill_id,
                last_applied_record,
            )
            packages.append(package)

    result = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "local_root": str(local_root.resolve()),
        "remote_snapshot": str(remote_snapshot_dir.resolve()),
        "last_applied_record": str(last_applied_record.resolve()) if last_applied_record else None,
        "total_conflicts": len(packages),
        "out": str(out_dir.resolve()),
        "packages": packages,
    }
    (out_dir / "conflict-index.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def _write_conflict_package(
    item: dict,
    local_root: Path,
    remote_snapshot_dir: Path,
    out_dir: Path,
    staged_by_skill_id: Dict[str, dict],
    last_applied_record: Optional[Path],
) -> Dict[str, object]:
    skill_id = str(item["skill_id"])
    package_dir = out_dir / f"{_timestamp_id()}-{sanitize_component(skill_id)}"
    if package_dir.exists():
        raise ConflictPackageError(f"conflict package already exists: {package_dir}")
    package_dir.mkdir(parents=True)

    metadata = {
        "protocol_version": 0,
        "record_type": "skill-sync-conflict-package",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "skill_id": skill_id,
        "reason": item.get("reason"),
        "base_hash": item.get("base_hash"),
        "local_hash": item.get("local_hash"),
        "remote_hash": item.get("remote_hash"),
        "local_root": str(local_root.resolve()),
        "remote_snapshot": str(remote_snapshot_dir.resolve()),
        "last_applied_record": str(last_applied_record.resolve()) if last_applied_record else None,
    }
    (package_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_local_material(package_dir, local_root, skill_id)
    _write_remote_material(package_dir, staged_by_skill_id, skill_id)
    _write_base_material(package_dir, item, last_applied_record)

    return {
        "skill_id": skill_id,
        "path": str(package_dir.resolve()),
        "base_hash": item.get("base_hash"),
        "local_hash": item.get("local_hash"),
        "remote_hash": item.get("remote_hash"),
    }


def _write_local_material(package_dir: Path, local_root: Path, skill_id: str) -> None:
    local_path = local_root / skill_id
    if local_path.exists():
        shutil.copytree(local_path, package_dir / "local")
        return
    (package_dir / "local.json").write_text(
        json.dumps({"state": "absent", "path": str(local_path)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_remote_material(package_dir: Path, staged_by_skill_id: Dict[str, dict], skill_id: str) -> None:
    staged = staged_by_skill_id.get(skill_id)
    if staged and staged.get("output_path") and Path(str(staged["output_path"])).exists():
        shutil.copytree(Path(str(staged["output_path"])), package_dir / "remote")
        return
    (package_dir / "remote.json").write_text(
        json.dumps({"state": "absent", "skill_id": skill_id}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_base_material(package_dir: Path, item: dict, last_applied_record: Optional[Path]) -> None:
    base = {
        "state": "metadata_only",
        "skill_id": item.get("skill_id"),
        "base_hash": item.get("base_hash"),
        "last_applied_record": str(last_applied_record.resolve()) if last_applied_record else None,
        "note": "Base file contents are not guaranteed to be retained; use this hash to identify the common ancestor.",
    }
    source = _base_source_path(item, last_applied_record)
    if source and source.exists() and source.is_dir():
        shutil.copytree(source, package_dir / "base")
        base["state"] = "copied"
        base["source_path"] = str(source)
    (package_dir / "base.json").write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _base_source_path(item: dict, last_applied_record: Optional[Path]) -> Optional[Path]:
    if not last_applied_record or not last_applied_record.exists():
        return None
    try:
        record = json.loads(last_applied_record.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    skill_id = item.get("skill_id")
    base_hash = item.get("base_hash")
    for applied in record.get("applied", []):
        if applied.get("skill_id") != skill_id or applied.get("content_hash") != base_hash:
            continue
        source_path = applied.get("source_path")
        if source_path:
            return Path(str(source_path))
    return None


def _timestamp_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
