from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .model import SkillRecord
from .scanner import scan_roots, scan_skill


DEFAULT_HUB_ROOT = Path.home() / ".skillshub"
DEFAULT_SOURCE_ROOTS: Tuple[Tuple[str, Path], ...] = (
    ("agents", Path.home() / ".agents" / "skills"),
    ("codex", Path.home() / ".codex" / "skills"),
    ("cc-switch", Path.home() / ".cc-switch" / "skills"),
)


class HubImportDiagnosisError(RuntimeError):
    pass


def parse_hub_source_spec(spec: str) -> Tuple[str, Path]:
    if "=" not in spec:
        raise ValueError(f"hub source must be id=/path: {spec}")
    source_id, raw_path = spec.split("=", 1)
    source_id = source_id.strip()
    raw_path = raw_path.strip()
    if not source_id or not raw_path:
        raise ValueError(f"hub source must be id=/path: {spec}")
    return source_id, Path(raw_path).expanduser()


def build_hub_import_diagnosis(
    hub_root: Path = DEFAULT_HUB_ROOT,
    source_roots: Optional[Sequence[Tuple[str, Path]]] = None,
) -> Dict[str, object]:
    hub_root = hub_root.expanduser()
    sources = list(source_roots or DEFAULT_SOURCE_ROOTS)
    hub_records = _scan_root("hub", hub_root)
    source_records: List[SkillRecord] = []
    for source_id, root in sources:
        source_records.extend(_scan_root(source_id, root.expanduser()))

    hub_by_id = _group_by_skill_id(hub_records)
    hub_by_resolved_path = {
        _resolved_path(record.path): record
        for record in hub_records
    }
    source_ids = _group_by_skill_id(source_records)
    items = [
        _diagnose_source_record(record, hub_by_id, hub_by_resolved_path, source_ids)
        for record in source_records
    ]
    summary: Dict[str, int] = {}
    for item in items:
        status = str(item["status"])
        summary[status] = summary.get(status, 0) + 1

    return {
        "record_type": "skill-sync-hub-import-diagnosis",
        "hub_root": str(hub_root),
        "hub_exists": hub_root.exists(),
        "hub_total": len(hub_records),
        "source_roots": [{"id": source_id, "path": str(root.expanduser()), "exists": root.expanduser().exists()} for source_id, root in sources],
        "source_total": len(source_records),
        "summary": dict(sorted(summary.items())),
        "items": sorted(items, key=lambda item: (str(item["status"]), str(item["source"]), str(item["skill_id"]), str(item["path"]))),
    }


def render_hub_import_diagnosis_text(diagnosis: Dict[str, object], *, max_items: int = 40) -> str:
    lines = [
        f"hub: {diagnosis.get('hub_root')}",
        f"hub_total: {diagnosis.get('hub_total')}",
        f"source_total: {diagnosis.get('source_total')}",
    ]
    summary = diagnosis.get("summary") if isinstance(diagnosis.get("summary"), dict) else {}
    lines.append(
        "summary: already_in_hub={} update_available={} importable={}".format(
            summary.get("already_in_hub", 0),
            summary.get("update_available", 0),
            summary.get("importable", 0),
        )
    )
    items = diagnosis.get("items") if isinstance(diagnosis.get("items"), list) else []
    if items:
        lines.append("")
        lines.append(f"items (first {min(max_items, len(items))}/{len(items)}):")
    for item in items[:max_items]:
        lines.append(
            "- {status}: {source}/{skill_id} -> {reason}".format(
                status=item.get("status"),
                source=item.get("source"),
                skill_id=item.get("skill_id"),
                reason=item.get("reason"),
            )
        )
    if len(items) > max_items:
        lines.append(f"... {len(items) - max_items} more")
    return "\n".join(lines)


def _scan_root(source_id: str, root: Path) -> List[SkillRecord]:
    root = root.expanduser()
    if not root.exists():
        return []
    records = list(scan_roots([f"{source_id}={root}"]).skills)
    seen_paths = {Path(record.path) for record in records}
    for child in sorted(root.iterdir()):
        skill_md = child / "SKILL.md"
        if not child.is_symlink() or not child.is_dir() or not skill_md.exists():
            continue
        if child in seen_paths:
            continue
        record = scan_skill(source_id, child.resolve(), child.resolve() / "SKILL.md")
        record.path = child
        record.skill_md = skill_md
        record.manifest_path = child / "manifest.json" if (child / "manifest.json").exists() else None
        records.append(record)
        seen_paths.add(child)
    records.sort(key=lambda record: (record.source, record.skill_id, str(record.path)))
    return records


def _group_by_skill_id(records: Sequence[SkillRecord]) -> Dict[str, List[SkillRecord]]:
    grouped: Dict[str, List[SkillRecord]] = {}
    for record in records:
        grouped.setdefault(record.skill_id, []).append(record)
    return grouped


def _diagnose_source_record(
    record: SkillRecord,
    hub_by_id: Dict[str, List[SkillRecord]],
    hub_by_resolved_path: Dict[str, SkillRecord],
    source_ids: Dict[str, List[SkillRecord]],
) -> Dict[str, object]:
    resolved_path = _resolved_path(record.path)
    hub_same_path = hub_by_resolved_path.get(resolved_path)
    hub_matches = hub_by_id.get(record.skill_id, [])
    duplicate_sources = [
        {
            "source": other.source,
            "path": str(other.path),
            "content_hash": other.content_hash,
        }
        for other in source_ids.get(record.skill_id, [])
        if other is not record
    ]
    base = {
        "source": record.source,
        "skill_id": record.skill_id,
        "path": str(record.path),
        "resolved_path": resolved_path,
        "content_hash": record.content_hash,
        "risk_level": record.risk_level,
        "duplicate_sources": duplicate_sources,
    }
    if hub_same_path:
        return {
            **base,
            "status": "already_in_hub",
            "hub_path": str(hub_same_path.path),
            "hub_hash": hub_same_path.content_hash,
            "reason": "source path resolves to an existing Hub skill",
        }
    if hub_matches:
        hub_hashes = sorted({item.content_hash for item in hub_matches})
        hub_paths = [str(item.path) for item in hub_matches]
        if record.content_hash in hub_hashes:
            return {
                **base,
                "status": "already_in_hub",
                "hub_path": hub_paths[0],
                "hub_hash": record.content_hash,
                "hub_paths": hub_paths,
                "reason": "same skill_id and content_hash already exist in Hub",
            }
        return {
            **base,
            "status": "update_available",
            "hub_path": hub_paths[0],
            "hub_hashes": hub_hashes,
            "hub_paths": hub_paths,
            "reason": "same skill_id exists in Hub with a different content_hash",
        }
    return {
        **base,
        "status": "importable",
        "hub_path": None,
        "reason": "skill_id does not exist in Hub",
    }


def _resolved_path(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)
