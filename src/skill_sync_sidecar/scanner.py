from __future__ import annotations

import hashlib
import json
import os
import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple

from .model import ScanSummary, SkillFile, SkillIssue, SkillRecord


DEFAULT_ROOTS: Tuple[Tuple[str, Path], ...] = (
    ("cc-switch", Path.home() / ".cc-switch" / "skills"),
    ("skillshub", Path.home() / ".skillshub"),
    ("codex", Path.home() / ".codex" / "skills"),
)

DEFAULT_EXCLUDED_DIRS = {
    ".cache",
    ".git",
    ".hg",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".skill-sync-backups",
    ".skill-sync-bases",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "logs",
    "node_modules",
    "target",
    "tmp",
    "venv",
}

DEFAULT_EXCLUDED_FILES = {
    ".DS_Store",
    ".encryption-key",
    ".env",
    ".env.local",
    ".npmrc",
    ".pypirc",
}

DEFAULT_EXCLUDED_FILE_PATTERNS = {
    ".env.*",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}

DEFAULT_EXCLUDED_PATH_PATTERNS = {
    "data/session-timers",
    "data/session-timers/*",
    "data/session-archives",
    "data/session-archives/*",
}

MAX_RECOMMENDED_SKILL_SIZE = 10 * 1024 * 1024
MAX_RECOMMENDED_FILE_COUNT = 500

FRONT_MATTER_RE = re.compile(r"^---\s*\n(?P<body>.*?)\n---\s*\n", re.DOTALL)
SAFE_ID_RE = re.compile(r"[^a-z0-9._-]+")
LOCAL_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w:/.-])(?P<path>/(?:home|Users|opt|var|etc|private|tmp)/[^\s`'\"),\]]+)")


def default_roots() -> List[Tuple[str, Path]]:
    return [(source, path) for source, path in DEFAULT_ROOTS if path.exists()]


def parse_root_spec(spec: str) -> Tuple[str, Path]:
    if "=" in spec:
        source, raw_path = spec.split("=", 1)
        source = source.strip() or "custom"
    else:
        raw_path = spec
        source = Path(raw_path).name or "custom"
    return source, Path(raw_path).expanduser()


def scan_roots(root_specs: Optional[Sequence[str]] = None) -> ScanSummary:
    roots = [parse_root_spec(spec) for spec in root_specs] if root_specs else default_roots()
    skills: List[SkillRecord] = []
    seen_paths = set()

    for source, root in roots:
        if not root.exists():
            continue
        for skill_md in discover_skill_files(root):
            skill_dir = skill_md.parent.resolve()
            if skill_dir in seen_paths:
                continue
            seen_paths.add(skill_dir)
            skills.append(scan_skill(source, skill_dir, skill_md))

    skills.sort(key=lambda item: (item.source, item.skill_id, str(item.path)))
    return ScanSummary(skills)


def discover_skill_files(root: Path) -> Iterator[Path]:
    for current_root, dirnames, filenames in os.walk(root):
        current = Path(current_root)
        dirnames[:] = [
            dirname
            for dirname in sorted(dirnames)
            if dirname not in DEFAULT_EXCLUDED_DIRS and not _is_hidden_vendor_dir(dirname)
        ]
        if "SKILL.md" in filenames:
            yield current / "SKILL.md"


def scan_skill(source: str, skill_dir: Path, skill_md: Path) -> SkillRecord:
    files: List[SkillFile] = []
    issues: List[SkillIssue] = []
    size_bytes = 0
    manifest = parse_skill_manifest(skill_dir / "manifest.json")
    project_path = detect_project_path(skill_dir)
    scope = manifest.get("scope") or ("project" if project_path else "global")
    exclude_patterns = list(manifest.get("exclude") or [])

    if skill_dir.is_symlink():
        target = os.readlink(skill_dir)
        if Path(target).is_absolute():
            issues.append(
                SkillIssue(
                    "warning",
                    "absolute_symlink",
                    "Skill directory is an absolute symlink; normalize before syncing.",
                    str(skill_dir),
                )
            )

    for file_path in iter_skill_files(skill_dir, exclude_patterns):
        try:
            stat = file_path.stat()
        except OSError as exc:
            issues.append(SkillIssue("warning", "stat_failed", str(exc), str(file_path)))
            continue
        if not file_path.is_file():
            continue
        size_bytes += stat.st_size
        rel = file_path.relative_to(skill_dir).as_posix()
        files.append(SkillFile(rel, stat.st_size, sha256_file(file_path)))

    files.sort(key=lambda file: file.path)
    content_hash = hash_skill_files(files)
    metadata = parse_skill_metadata(skill_md)
    skill_id = normalize_skill_id(manifest.get("skill_id") or metadata.get("name") or skill_dir.name)
    name = manifest.get("name") or metadata.get("name")
    description = manifest.get("description") or metadata.get("description")
    targets = list(manifest.get("targets") or default_targets_for_scope(scope))

    issues.extend(validate_manifest(skill_dir, manifest))
    issues.extend(validate_skill(skill_dir, skill_md, {"name": name, "description": description, **metadata}, files, size_bytes))

    return SkillRecord(
        skill_id=skill_id,
        source=source,
        scope=scope,
        path=skill_dir,
        skill_md=skill_md,
        content_hash=content_hash,
        size_bytes=size_bytes,
        file_count=len(files),
        name=name,
        description=description,
        targets=targets,
        exclude=exclude_patterns,
        project_path=project_path,
        manifest_path=skill_dir / "manifest.json" if (skill_dir / "manifest.json").exists() else None,
        files=files,
        issues=issues,
    )


def iter_skill_files(skill_dir: Path, exclude_patterns: Optional[Sequence[str]] = None) -> Iterator[Path]:
    root = skill_dir.resolve()
    excludes = list(exclude_patterns or [])
    for current_root, dirnames, filenames in os.walk(skill_dir):
        current = Path(current_root)
        dirnames[:] = [
            dirname
            for dirname in sorted(dirnames)
            if dirname not in DEFAULT_EXCLUDED_DIRS
            and not _is_hidden_vendor_dir(dirname)
            and not _is_nested_skill_dir(root, current / dirname)
            and not is_default_excluded_path((current / dirname).relative_to(root).as_posix())
            and not is_excluded((current / dirname).relative_to(root).as_posix(), excludes)
        ]
        for filename in sorted(filenames):
            if is_default_excluded_file(filename):
                continue
            path = current / filename
            rel = path.relative_to(root).as_posix()
            if is_default_excluded_path(rel):
                continue
            if is_excluded(rel, excludes):
                continue
            yield path


def parse_skill_metadata(skill_md: Path) -> dict:
    try:
        text = skill_md.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"_decode_error": "SKILL.md is not valid UTF-8"}
    except OSError as exc:
        return {"_read_error": str(exc)}

    match = FRONT_MATTER_RE.match(text)
    if not match:
        return {}

    metadata = {}
    lines = match.group("body").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if ":" not in line or line.lstrip().startswith("#"):
            index += 1
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in {"name", "description"}:
            if value in {"|", ">"}:
                block_lines = []
                index += 1
                while index < len(lines):
                    next_line = lines[index]
                    if next_line and not next_line.startswith((" ", "\t")):
                        index -= 1
                        break
                    if next_line.strip():
                        block_lines.append(next_line.strip())
                    index += 1
                metadata[key] = " ".join(block_lines).strip()
            else:
                metadata[key] = value
        index += 1
    return metadata


def parse_skill_manifest(manifest_path: Path) -> dict:
    if not manifest_path.exists():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"_manifest_error": str(exc)}
    if not isinstance(data, dict):
        return {"_manifest_error": "manifest.json must contain a JSON object"}
    return data


def validate_manifest(skill_dir: Path, manifest: dict) -> List[SkillIssue]:
    issues: List[SkillIssue] = []
    error = manifest.get("_manifest_error")
    if error:
        issues.append(SkillIssue("error", "invalid_manifest", error, str(skill_dir / "manifest.json")))
        return issues

    scope = manifest.get("scope")
    if scope and scope not in {"global", "project"}:
        issues.append(
            SkillIssue("warning", "invalid_scope", "manifest scope should be global or project.", str(skill_dir / "manifest.json"))
        )
    targets = manifest.get("targets")
    if targets is not None and not isinstance(targets, list):
        issues.append(
            SkillIssue("warning", "invalid_targets", "manifest targets should be a list.", str(skill_dir / "manifest.json"))
        )
    exclude = manifest.get("exclude")
    if exclude is not None and not isinstance(exclude, list):
        issues.append(
            SkillIssue("warning", "invalid_exclude", "manifest exclude should be a list.", str(skill_dir / "manifest.json"))
        )
    return issues


def detect_project_path(skill_dir: Path) -> Optional[Path]:
    parent = skill_dir.parent
    if parent.name != "skills":
        return None
    project_root = parent.parent
    if (project_root / "AGENTS.md").exists() or (project_root / ".git").exists():
        return project_root
    return None


def default_targets_for_scope(scope: str) -> List[str]:
    if scope == "project":
        return ["codex", "cursor", "qoder"]
    return ["cc-switch", "skillshub", "codex", "openclaw"]


def is_excluded(rel_path: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        clean = str(pattern).strip("/")
        if not clean:
            continue
        if rel_path == clean or rel_path.startswith(clean + "/"):
            return True
        if fnmatch(rel_path, clean) or fnmatch(Path(rel_path).name, clean):
            return True
    return False


def is_default_excluded_file(filename: str) -> bool:
    if filename in DEFAULT_EXCLUDED_FILES:
        return True
    return any(fnmatch(filename, pattern) for pattern in DEFAULT_EXCLUDED_FILE_PATTERNS)


def is_default_excluded_path(rel_path: str) -> bool:
    clean = rel_path.strip("/")
    return any(fnmatch(clean, pattern) for pattern in DEFAULT_EXCLUDED_PATH_PATTERNS)


def validate_skill(
    skill_dir: Path,
    skill_md: Path,
    metadata: dict,
    files: Sequence[SkillFile],
    size_bytes: int,
) -> List[SkillIssue]:
    issues: List[SkillIssue] = []

    if metadata.get("_decode_error"):
        issues.append(SkillIssue("error", "skill_md_not_utf8", metadata["_decode_error"], str(skill_md)))
    if metadata.get("_read_error"):
        issues.append(SkillIssue("error", "skill_md_unreadable", metadata["_read_error"], str(skill_md)))
    if not metadata.get("name"):
        issues.append(SkillIssue("warning", "missing_name", "SKILL.md front matter has no name.", str(skill_md)))
    if not metadata.get("description"):
        issues.append(
            SkillIssue("warning", "missing_description", "SKILL.md front matter has no description.", str(skill_md))
        )
    if size_bytes > MAX_RECOMMENDED_SKILL_SIZE:
        issues.append(
            SkillIssue(
                "warning",
                "large_skill",
                f"Skill size is {size_bytes} bytes; consider excluding generated artifacts.",
                str(skill_dir),
            )
        )
    if len(files) > MAX_RECOMMENDED_FILE_COUNT:
        issues.append(
            SkillIssue(
                "warning",
                "many_files",
                f"Skill has {len(files)} files; check whether dependencies or build output are included.",
                str(skill_dir),
            )
        )

    risky_patterns = [
        ("curl_pipe_shell", re.compile(r"curl\s+[^|\n]+\|\s*(sh|bash|zsh)")),
        ("wget_pipe_shell", re.compile(r"wget\s+[^|\n]+\|\s*(sh|bash|zsh)")),
        ("destructive_rm", re.compile(r"rm\s+-[^\n]*r[^\n]*f")),
        ("sudo_command", re.compile(r"\bsudo\b")),
    ]
    try:
        skill_text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        skill_text = ""
    for code, pattern in risky_patterns:
        if pattern.search(skill_text):
            issues.append(
                SkillIssue(
                    "warning",
                    code,
                    "SKILL.md contains a risky shell pattern; require review before auto-install.",
                    str(skill_md),
                )
            )

    external_paths = sorted(set(match.group("path").rstrip(".,;:") for match in LOCAL_ABSOLUTE_PATH_RE.finditer(skill_text)))
    for external_path in external_paths[:5]:
        issues.append(
            SkillIssue(
                "warning",
                "external_absolute_path_reference",
                f"SKILL.md references a local absolute path outside the package: {external_path}",
                str(skill_md),
            )
        )

    return issues


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def hash_skill_files(files: Sequence[SkillFile]) -> str:
    hasher = hashlib.sha256()
    for file in files:
        hasher.update(file.path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(str(file.size).encode("ascii"))
        hasher.update(b"\0")
        hasher.update(file.sha256.encode("ascii"))
        hasher.update(b"\0")
    return hasher.hexdigest()


def normalize_skill_id(raw: str) -> str:
    normalized = SAFE_ID_RE.sub("-", raw.lower()).strip("-._")
    return normalized or "unnamed-skill"


def _is_hidden_vendor_dir(dirname: str) -> bool:
    return dirname.startswith(".") and dirname not in {".well-known"}


def _is_nested_skill_dir(root: Path, path: Path) -> bool:
    candidate = path.resolve()
    if candidate == root:
        return False
    return (candidate / "SKILL.md").exists()
