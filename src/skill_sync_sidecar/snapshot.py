from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Sequence
from zipfile import ZIP_DEFLATED, ZipFile

from .model import ScanSummary, SkillRecord
from .scanner import normalize_skill_id


def write_snapshot(summary: ScanSummary, output_dir: Path, label: str | None = None) -> Dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    skills_dir = output_dir / "skills"
    skills_dir.mkdir(exist_ok=True)

    created_at = datetime.now(timezone.utc).isoformat()
    snapshot_id = label or _timestamp_id()
    entries = []

    for skill in summary.skills:
        source_id = normalize_skill_id(skill.source)
        skill_dir = skills_dir / source_id / skill.skill_id
        skill_dir.mkdir(parents=True, exist_ok=True)
        archive_name = f"{skill.content_hash}.zip"
        archive_path = skill_dir / archive_name
        write_skill_zip(skill, archive_path)
        entries.append(
            {
                "key": f"{skill.source}/{skill.skill_id}",
                "source": skill.source,
                "skill_id": skill.skill_id,
                "scope": skill.scope,
                "name": skill.name,
                "description": skill.description,
                "targets": skill.targets,
                "project_path": str(skill.project_path) if skill.project_path else None,
                "content_hash": skill.content_hash,
                "risk_level": skill.risk_level,
                "size_bytes": skill.size_bytes,
                "file_count": skill.file_count,
                "source_path": str(skill.path),
                "archive": archive_path.relative_to(output_dir).as_posix(),
                "issues": [issue.__dict__ for issue in skill.issues],
            }
        )

    index = {
        "protocol_version": 0,
        "snapshot_id": snapshot_id,
        "created_at": created_at,
        "total": len(entries),
        "skills": entries,
    }
    (output_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return index


def write_skill_zip(skill: SkillRecord, archive_path: Path) -> None:
    with ZipFile(archive_path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            ".skill-sync/manifest.json",
            json.dumps(skill.to_dict(include_files=True), ensure_ascii=False, indent=2) + "\n",
        )
        for file in skill.files:
            archive.write(skill.path / file.path, file.path)


def _timestamp_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
