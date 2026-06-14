from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from zipfile import ZipFile


class ReconcileError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReconcileItem:
    skill_id: str
    status: str
    recommendation: str
    conflict_category: Optional[str]
    local_hash: Optional[str]
    remote_hash: Optional[str]
    local_path: Optional[str]
    remote_archive: Optional[str]
    changed_files: List[str]
    remote_only_files: List[str]
    local_only_files: List[str]


def load_inventory(path: Path) -> Dict[str, object]:
    if not path.exists():
        raise ReconcileError(f"inventory JSON not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if "skills" not in data or not isinstance(data["skills"], list):
        raise ReconcileError(f"inventory JSON has no skills list: {path}")
    return data


def build_reconcile_report(
    local_inventory: Dict[str, object],
    remote_snapshot_dir: Path,
    previous_local_inventory: Optional[Dict[str, object]] = None,
    label: Optional[str] = None,
) -> Dict[str, object]:
    remote_index_path = remote_snapshot_dir / "index.json"
    if not remote_index_path.exists():
        raise ReconcileError(f"remote snapshot has no index.json: {remote_snapshot_dir}")

    remote_index = json.loads(remote_index_path.read_text(encoding="utf-8"))
    local_entries = _unique_by_skill_id(local_inventory.get("skills", []), "local")
    remote_entries = _unique_by_skill_id(remote_index.get("skills", []), "remote")
    previous_entries = (
        _unique_by_skill_id(previous_local_inventory.get("skills", []), "previous_local")
        if previous_local_inventory
        else {}
    )

    items: List[ReconcileItem] = []
    for skill_id in sorted(set(local_entries) | set(remote_entries)):
        local = local_entries.get(skill_id)
        remote = remote_entries.get(skill_id)
        local_hash = _hash(local)
        remote_hash = _hash(remote)
        if local and not remote:
            status = "local_new"
        elif remote and not local:
            status = "remote_new"
        elif local_hash == remote_hash:
            status = "same_without_base"
        else:
            status = "conflict"

        changed_files: List[str] = []
        remote_only_files: List[str] = []
        local_only_files: List[str] = []
        if status == "conflict" and local and remote:
            diff = _file_diff(local, remote, remote_snapshot_dir)
            changed_files = diff["changed"]
            remote_only_files = diff["remote_only"]
            local_only_files = diff["local_only"]

        items.append(
            ReconcileItem(
                skill_id=skill_id,
                status=status,
                recommendation=_recommendation(status),
                conflict_category=_conflict_category(status, changed_files, remote_only_files, local_only_files),
                local_hash=local_hash,
                remote_hash=remote_hash,
                local_path=str(local.get("path")) if local and local.get("path") else None,
                remote_archive=str(remote.get("archive")) if remote and remote.get("archive") else None,
                changed_files=changed_files,
                remote_only_files=remote_only_files,
                local_only_files=local_only_files,
            )
        )

    summary = Counter(item.status for item in items)
    changed_since_previous = _changed_since_previous(previous_entries, local_entries) if previous_entries else None

    return {
        "protocol_version": 0,
        "report_type": "skill-sync-reconcile",
        "label": label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "local_total": len(local_entries),
        "remote_total": len(remote_entries),
        "comparison_total": len(items),
        "summary": dict(sorted(summary.items())),
        "recommendations": _recommendation_summary(items),
        "changed_since_previous": changed_since_previous,
        "safe_to_auto_apply": summary.get("conflict", 0) == 0 and summary.get("local_new", 0) == 0,
        "items": [item.__dict__ for item in items],
    }


def write_reconcile_outputs(report: Dict[str, object], output_dir: Path) -> Dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "reconcile-report.json"
    md_path = output_dir / "reconcile-report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return {"json": str(json_path.resolve()), "markdown": str(md_path.resolve())}


def render_markdown(report: Dict[str, object]) -> str:
    lines = [
        "# Skill Sync Reconcile Report",
        "",
        f"- Created at: `{report.get('created_at')}`",
        f"- Label: `{report.get('label') or ''}`",
        f"- Local skills: `{report.get('local_total')}`",
        f"- Remote skills: `{report.get('remote_total')}`",
        f"- Safe to auto apply: `{str(report.get('safe_to_auto_apply')).lower()}`",
        "",
        "## Summary",
        "",
    ]
    for key, value in dict(report.get("summary") or {}).items():
        lines.append(f"- `{key}`: `{value}`")

    changed = report.get("changed_since_previous")
    if isinstance(changed, dict):
        lines.extend(["", "## Changed Since Previous Local Inventory", ""])
        lines.append(f"- Changed: `{changed.get('changed_count', 0)}`")
        lines.append(f"- Added: `{len(changed.get('added', []))}`")
        lines.append(f"- Removed: `{len(changed.get('removed', []))}`")
        if changed.get("changed"):
            lines.append("")
            lines.append("Changed skills:")
            for skill_id in changed["changed"]:
                lines.append(f"- `{skill_id}`")

    grouped: Dict[str, List[dict]] = {}
    for item in report.get("items", []):
        grouped.setdefault(str(item.get("status")), []).append(item)

    for status in ("conflict", "local_new", "remote_new", "same_without_base"):
        items = grouped.get(status, [])
        if not items:
            continue
        lines.extend(["", f"## {status}", ""])
        for item in items:
            category = item.get("conflict_category")
            category_text = f" / `{category}`" if category else ""
            lines.append(f"- `{item.get('skill_id')}`: `{item.get('recommendation')}`{category_text}")
            changed_files = item.get("changed_files") or []
            remote_only = item.get("remote_only_files") or []
            local_only = item.get("local_only_files") or []
            if changed_files:
                lines.append(f"  - changed: {', '.join('`' + path + '`' for path in changed_files[:12])}")
            if remote_only:
                lines.append(f"  - remote_only: {', '.join('`' + path + '`' for path in remote_only[:12])}")
            if local_only:
                lines.append(f"  - local_only: {', '.join('`' + path + '`' for path in local_only[:12])}")

    lines.append("")
    return "\n".join(lines)


def _unique_by_skill_id(entries: object, label: str) -> Dict[str, dict]:
    result: Dict[str, dict] = {}
    duplicates: List[str] = []
    if not isinstance(entries, list):
        raise ReconcileError(f"{label} skills must be a list")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        skill_id = entry.get("skill_id")
        if not skill_id:
            continue
        key = str(skill_id)
        if key in result:
            duplicates.append(key)
        result[key] = dict(entry)
    if duplicates:
        raise ReconcileError(f"duplicate {label} skill ids: {', '.join(sorted(set(duplicates)))}")
    return result


def _hash(entry: Optional[dict]) -> Optional[str]:
    if not entry:
        return None
    value = entry.get("content_hash")
    return str(value) if value else None


def _recommendation(status: str) -> str:
    if status == "same_without_base":
        return "adopt_base_candidate"
    if status == "remote_new":
        return "review_pull_new"
    if status == "local_new":
        return "review_push_new"
    if status == "conflict":
        return "manual_merge_required"
    return "review_required"


def _recommendation_summary(items: List[ReconcileItem]) -> Dict[str, int]:
    summary = Counter(item.recommendation for item in items)
    return dict(sorted(summary.items()))


def _conflict_category(status: str, changed: List[str], remote_only: List[str], local_only: List[str]) -> Optional[str]:
    if status != "conflict":
        return None
    paths = changed + remote_only + local_only
    if not paths:
        return "hash_only"
    if all(_is_generated_path(path) for path in paths):
        return "generated_only"
    non_generated = [path for path in paths if not _is_generated_path(path)]
    if non_generated == ["SKILL.md"]:
        return "skill_md_only"
    if all(_is_doc_path(path) for path in non_generated):
        return "docs_only"
    if all(_is_code_or_config_path(path) for path in non_generated):
        return "code_or_config"
    if any(_is_code_or_config_path(path) for path in non_generated):
        return "mixed_with_code"
    return "mixed"


def _is_generated_path(path: str) -> bool:
    return path.startswith("data/session-timers/") or path.startswith("data/session-archives/")


def _is_doc_path(path: str) -> bool:
    return path == "SKILL.md" or path.startswith("docs/") or path.endswith((".md", ".mdx", ".txt"))


def _is_code_or_config_path(path: str) -> bool:
    return path.endswith(
        (
            ".py",
            ".js",
            ".ts",
            ".sh",
            ".json",
            ".yaml",
            ".yml",
            ".toml",
            ".sql",
            ".cfg",
            ".ini",
        )
    )


def _changed_since_previous(previous: Dict[str, dict], current: Dict[str, dict]) -> Dict[str, object]:
    changed = sorted(
        skill_id
        for skill_id in set(previous) & set(current)
        if _hash(previous[skill_id]) != _hash(current[skill_id])
    )
    return {
        "changed_count": len(changed),
        "changed": changed,
        "added": sorted(set(current) - set(previous)),
        "removed": sorted(set(previous) - set(current)),
    }


def _file_diff(local_entry: dict, remote_entry: dict, remote_snapshot_dir: Path) -> Dict[str, List[str]]:
    local_files = {
        str(item.get("path")): {
            "size": item.get("size"),
            "sha256": item.get("sha256"),
        }
        for item in local_entry.get("files", [])
        if isinstance(item, dict) and item.get("path")
    }
    remote_files = _remote_files_from_archive(remote_snapshot_dir, remote_entry)
    common = set(local_files) & set(remote_files)
    return {
        "changed": sorted(path for path in common if local_files[path] != remote_files[path]),
        "remote_only": sorted(set(remote_files) - set(local_files)),
        "local_only": sorted(set(local_files) - set(remote_files)),
    }


def _remote_files_from_archive(remote_snapshot_dir: Path, remote_entry: dict) -> Dict[str, dict]:
    archive = remote_entry.get("archive")
    if not archive:
        return {}
    archive_path = remote_snapshot_dir / str(archive)
    if not archive_path.exists():
        raise ReconcileError(f"remote archive missing: {archive_path}")
    with ZipFile(archive_path) as zip_file:
        try:
            manifest = json.loads(zip_file.read(".skill-sync/manifest.json").decode("utf-8"))
        except KeyError as exc:
            raise ReconcileError(f"remote archive has no .skill-sync/manifest.json: {archive_path}") from exc
    return {
        str(item.get("path")): {
            "size": item.get("size"),
            "sha256": item.get("sha256"),
        }
        for item in manifest.get("files", [])
        if isinstance(item, dict) and item.get("path")
    }
