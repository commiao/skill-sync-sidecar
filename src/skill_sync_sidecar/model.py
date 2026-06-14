from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass(frozen=True)
class SkillFile:
    path: str
    size: int
    sha256: str


@dataclass
class SkillIssue:
    severity: str
    code: str
    message: str
    path: Optional[str] = None


@dataclass
class SkillRecord:
    skill_id: str
    source: str
    scope: str
    path: Path
    skill_md: Path
    content_hash: str
    size_bytes: int
    file_count: int
    name: Optional[str] = None
    description: Optional[str] = None
    targets: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    project_path: Optional[Path] = None
    manifest_path: Optional[Path] = None
    files: List[SkillFile] = field(default_factory=list)
    issues: List[SkillIssue] = field(default_factory=list)

    @property
    def risk_level(self) -> str:
        severities = {issue.severity for issue in self.issues}
        if "error" in severities:
            return "error"
        if "warning" in severities:
            return "warning"
        return "ok"

    def to_dict(self, include_files: bool = False) -> Dict[str, object]:
        data: Dict[str, object] = {
            "skill_id": self.skill_id,
            "source": self.source,
            "scope": self.scope,
            "path": str(self.path),
            "skill_md": str(self.skill_md),
            "manifest_path": str(self.manifest_path) if self.manifest_path else None,
            "project_path": str(self.project_path) if self.project_path else None,
            "name": self.name,
            "description": self.description,
            "targets": self.targets,
            "exclude": self.exclude,
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "file_count": self.file_count,
            "risk_level": self.risk_level,
            "issues": [issue.__dict__ for issue in self.issues],
        }
        if include_files:
            data["files"] = [file.__dict__ for file in self.files]
        return data


@dataclass
class ScanSummary:
    skills: List[SkillRecord]

    def to_dict(self, include_files: bool = False) -> Dict[str, object]:
        by_source: Dict[str, int] = {}
        by_risk: Dict[str, int] = {"ok": 0, "warning": 0, "error": 0}
        duplicate_ids: Dict[str, int] = {}

        for skill in self.skills:
            by_source[skill.source] = by_source.get(skill.source, 0) + 1
            by_risk[skill.risk_level] = by_risk.get(skill.risk_level, 0) + 1
            duplicate_ids[skill.skill_id] = duplicate_ids.get(skill.skill_id, 0) + 1

        return {
            "total": len(self.skills),
            "by_source": dict(sorted(by_source.items())),
            "by_risk": by_risk,
            "duplicates": {
                skill_id: count
                for skill_id, count in sorted(duplicate_ids.items())
                if count > 1
            },
            "skills": [skill.to_dict(include_files=include_files) for skill in self.skills],
        }
