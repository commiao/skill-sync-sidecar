from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, List
from zipfile import ZipFile

from .model import SkillFile
from .scanner import hash_skill_files, sha256_file


class StageError(RuntimeError):
    pass


@dataclass(frozen=True)
class StagedSkill:
    key: str
    skill_id: str
    source: str
    scope: str
    content_hash: str
    output_path: str
    file_count: int


def stage_snapshot(snapshot_dir: Path, output_dir: Path, clean: bool = False) -> Dict[str, object]:
    index_path = snapshot_dir / "index.json"
    if not index_path.exists():
        raise StageError(f"snapshot has no index.json: {snapshot_dir}")

    index = json.loads(index_path.read_text(encoding="utf-8"))
    snapshot_id = str(index.get("snapshot_id") or "unknown-snapshot")
    stage_root = output_dir / sanitize_component(snapshot_id)
    if clean and stage_root.exists():
        shutil.rmtree(stage_root)
    stage_root.mkdir(parents=True, exist_ok=True)

    staged = [stage_skill(snapshot_dir, stage_root, skill) for skill in index.get("skills", [])]
    stage_index = {
        "protocol_version": index.get("protocol_version", 0),
        "snapshot_id": snapshot_id,
        "source_snapshot": str(snapshot_dir.resolve()),
        "total": len(staged),
        "skills": [skill.__dict__ for skill in staged],
    }
    (stage_root / ".stage-index.json").write_text(
        json.dumps(stage_index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return stage_index


def stage_skill(snapshot_dir: Path, stage_root: Path, skill: dict) -> StagedSkill:
    archive_rel = skill.get("archive")
    if not archive_rel:
        raise StageError(f"skill has no archive field: {skill.get('key')}")
    archive_path = snapshot_dir / str(archive_rel)
    if not archive_path.exists():
        raise StageError(f"archive missing: {archive_rel}")

    source = sanitize_component(str(skill.get("source") or "unknown-source"))
    skill_id = sanitize_component(str(skill.get("skill_id") or "unknown-skill"))
    target_dir = stage_root / source / skill_id
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    with ZipFile(archive_path) as archive:
        names = archive.namelist()
        manifest_name = ".skill-sync/manifest.json"
        if manifest_name not in names:
            raise StageError(f"archive has no {manifest_name}: {archive_rel}")
        manifest = json.loads(archive.read(manifest_name).decode("utf-8"))
        expected_hash = skill.get("content_hash")
        actual_hash = manifest.get("content_hash")
        if expected_hash != actual_hash:
            raise StageError(
                f"content hash mismatch for {skill.get('key')}: index={expected_hash} manifest={actual_hash}"
            )

        for member in names:
            if member == manifest_name or member.endswith("/"):
                continue
            validate_archive_member(member)
            destination = target_dir / member
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(member))

    validate_staged_files(target_dir, manifest)
    return StagedSkill(
        key=str(skill.get("key") or f"{skill.get('source')}/{skill.get('skill_id')}"),
        skill_id=str(skill.get("skill_id") or skill_id),
        source=str(skill.get("source") or source),
        scope=str(skill.get("scope") or manifest.get("scope") or "global"),
        content_hash=str(skill.get("content_hash") or ""),
        output_path=str(target_dir),
        file_count=int(skill.get("file_count") or len(manifest.get("files") or [])),
    )


def validate_archive_member(member: str) -> None:
    path = PurePosixPath(member)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise StageError(f"unsafe archive member path: {member}")


def validate_staged_files(target_dir: Path, manifest: dict) -> None:
    files = []
    for file_data in manifest.get("files") or []:
        rel = file_data.get("path")
        expected_sha = file_data.get("sha256")
        expected_size = file_data.get("size")
        if not rel or not expected_sha:
            raise StageError("manifest file entry missing path or sha256")
        validate_archive_member(str(rel))
        path = target_dir / str(rel)
        if not path.exists():
            raise StageError(f"staged file missing: {rel}")
        actual_size = path.stat().st_size
        if expected_size is not None and actual_size != int(expected_size):
            raise StageError(f"size mismatch for {rel}: expected={expected_size} actual={actual_size}")
        actual_sha = sha256_file(path)
        if actual_sha != expected_sha:
            raise StageError(f"sha256 mismatch for {rel}")
        files.append(SkillFile(str(rel), actual_size, actual_sha))

    actual_hash = hash_skill_files(sorted(files, key=lambda file: file.path))
    expected_hash = manifest.get("content_hash")
    if expected_hash and actual_hash != expected_hash:
        raise StageError(f"staged content hash mismatch: expected={expected_hash} actual={actual_hash}")


def sanitize_component(value: str) -> str:
    keep = []
    for char in value:
        keep.append(char if char.isalnum() or char in {"-", "_", "."} else "-")
    cleaned = "".join(keep).strip("-._")
    return cleaned or "unnamed"
