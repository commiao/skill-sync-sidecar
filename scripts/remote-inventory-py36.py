#!/usr/bin/env python3
"""Compute a sidecar-compatible skill inventory on older Python hosts.

This helper intentionally stays Python 3.6 compatible so legacy machines can
participate in read-only validation before the full sidecar runtime is deployed.
"""

import argparse
import fnmatch
import hashlib
import json
import os
import re


EXCLUDED_DIRS = {
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
EXCLUDED_FILES = {
    ".DS_Store",
    ".encryption-key",
    ".env",
    ".env.local",
    ".npmrc",
    ".pypirc",
}
EXCLUDED_FILE_PATTERNS = {
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
EXCLUDED_PATH_PATTERNS = {
    "data/session-timers",
    "data/session-timers/*",
    "data/session-archives",
    "data/session-archives/*",
}
FRONT_MATTER_RE = re.compile(r"^---\s*\n(?P<body>.*?)\n---\s*\n", re.DOTALL)
SAFE_ID_RE = re.compile(r"[^a-z0-9._-]+")


def sha256_file(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def hash_skill_files(files):
    hasher = hashlib.sha256()
    for item in files:
        hasher.update(item["path"].encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(str(item["size"]).encode("ascii"))
        hasher.update(b"\0")
        hasher.update(item["sha256"].encode("ascii"))
        hasher.update(b"\0")
    return hasher.hexdigest()


def normalize_skill_id(raw):
    normalized = SAFE_ID_RE.sub("-", raw.lower()).strip("-._")
    return normalized or "unnamed-skill"


def is_excluded(rel_path, patterns):
    for pattern in patterns or []:
        clean = str(pattern).strip("/")
        if not clean:
            continue
        if rel_path == clean or rel_path.startswith(clean + "/"):
            return True
        if fnmatch.fnmatch(rel_path, clean) or fnmatch.fnmatch(os.path.basename(rel_path), clean):
            return True
    return False


def is_default_excluded_file(filename):
    if filename in EXCLUDED_FILES:
        return True
    return any(fnmatch.fnmatch(filename, pattern) for pattern in EXCLUDED_FILE_PATTERNS)


def is_default_excluded_path(rel_path):
    clean = rel_path.strip("/")
    return any(fnmatch.fnmatch(clean, pattern) for pattern in EXCLUDED_PATH_PATTERNS)


def parse_manifest(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        return {"_manifest_error": str(exc)}
    return data if isinstance(data, dict) else {"_manifest_error": "manifest.json must contain a JSON object"}


def parse_metadata(skill_md):
    try:
        with open(skill_md, "r", encoding="utf-8") as handle:
            text = handle.read()
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


def is_nested_skill_dir(root, path):
    candidate = os.path.realpath(path)
    if candidate == root:
        return False
    return os.path.exists(os.path.join(candidate, "SKILL.md"))


def iter_skill_files(skill_dir, exclude_patterns):
    root = os.path.realpath(skill_dir)
    for current_root, dirnames, filenames in os.walk(skill_dir):
        dirnames[:] = [
            name
            for name in sorted(dirnames)
            if name not in EXCLUDED_DIRS
            and not (name.startswith(".") and name != ".well-known")
            and not is_nested_skill_dir(root, os.path.join(current_root, name))
            and not is_default_excluded_path(os.path.relpath(os.path.join(current_root, name), root).replace(os.sep, "/"))
            and not is_excluded(os.path.relpath(os.path.join(current_root, name), root).replace(os.sep, "/"), exclude_patterns)
        ]
        for filename in sorted(filenames):
            if is_default_excluded_file(filename):
                continue
            path = os.path.join(current_root, filename)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if is_default_excluded_path(rel):
                continue
            if is_excluded(rel, exclude_patterns):
                continue
            if os.path.isfile(path):
                yield path, rel


def discover_skill_dirs(root):
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in sorted(dirnames)
            if name not in EXCLUDED_DIRS and not (name.startswith(".") and name != ".well-known")
        ]
        if "SKILL.md" in filenames:
            yield current_root


def scan_skill(source, skill_dir, include_files=False):
    skill_md = os.path.join(skill_dir, "SKILL.md")
    manifest = parse_manifest(os.path.join(skill_dir, "manifest.json"))
    metadata = parse_metadata(skill_md)
    scope = manifest.get("scope") or "global"
    exclude_patterns = list(manifest.get("exclude") or [])
    files = []
    size_bytes = 0

    for path, rel in iter_skill_files(skill_dir, exclude_patterns):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        size_bytes += size
        files.append({"path": rel, "size": size, "sha256": sha256_file(path)})

    files.sort(key=lambda item: item["path"])
    skill_id = normalize_skill_id(manifest.get("skill_id") or metadata.get("name") or os.path.basename(skill_dir))
    issues = []
    if not metadata.get("name"):
        issues.append({"severity": "warning", "code": "missing_name", "message": "SKILL.md front matter has no name.", "path": skill_md})
    if not metadata.get("description"):
        issues.append({"severity": "warning", "code": "missing_description", "message": "SKILL.md front matter has no description.", "path": skill_md})
    if size_bytes > 10 * 1024 * 1024:
        issues.append({"severity": "warning", "code": "large_skill", "message": "Skill size exceeds recommended size.", "path": skill_dir})
    if len(files) > 500:
        issues.append({"severity": "warning", "code": "many_files", "message": "Skill has many files.", "path": skill_dir})
    severities = {issue["severity"] for issue in issues}
    risk_level = "error" if "error" in severities else "warning" if "warning" in severities else "ok"

    record = {
        "skill_id": skill_id,
        "source": source,
        "scope": scope,
        "path": skill_dir,
        "skill_md": skill_md,
        "manifest_path": os.path.join(skill_dir, "manifest.json") if os.path.exists(os.path.join(skill_dir, "manifest.json")) else None,
        "name": manifest.get("name") or metadata.get("name"),
        "description": manifest.get("description") or metadata.get("description"),
        "targets": list(manifest.get("targets") or ["cc-switch", "skillshub", "codex", "openclaw"]),
        "exclude": exclude_patterns,
        "content_hash": hash_skill_files(files),
        "size_bytes": size_bytes,
        "file_count": len(files),
        "risk_level": risk_level,
        "issues": issues,
    }
    if include_files:
        record["files"] = files
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    parser.add_argument("--source", default="openclaw")
    parser.add_argument("--include-files", action="store_true")
    args = parser.parse_args()

    skills = [scan_skill(args.source, path, include_files=args.include_files) for path in discover_skill_dirs(args.root)]
    skills.sort(key=lambda item: (item["source"], item["skill_id"], item["path"]))
    by_source = {}
    by_risk = {"ok": 0, "warning": 0, "error": 0}
    ids = {}
    for skill in skills:
        by_source[skill["source"]] = by_source.get(skill["source"], 0) + 1
        by_risk[skill["risk_level"]] = by_risk.get(skill["risk_level"], 0) + 1
        ids[skill["skill_id"]] = ids.get(skill["skill_id"], 0) + 1

    print(json.dumps({
        "total": len(skills),
        "by_source": by_source,
        "by_risk": by_risk,
        "duplicates": {key: value for key, value in sorted(ids.items()) if value > 1},
        "skills": skills,
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
