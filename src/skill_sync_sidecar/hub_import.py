from __future__ import annotations

import difflib
import json
from datetime import datetime, timezone
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

STATUS_METADATA: Dict[str, Dict[str, str]] = {
    "already_in_hub": {
        "label": "已在 Hub",
        "operator_action": "无需导入",
        "description": "Hub 已有相同 skill；导入会被拒绝是正常保护。",
    },
    "update_available": {
        "label": "可更新",
        "operator_action": "先看差异再更新",
        "description": "Hub 中已有同名 skill，但外部来源内容不同。",
    },
    "importable": {
        "label": "可导入",
        "operator_action": "可纳入导入候选",
        "description": "Hub 中还没有这个 skill ID。",
    },
}

REASON_LABELS = {
    "same_resolved_path": "来源目录实际指向 Hub 中已有 skill。",
    "same_id_and_hash": "Hub 中已有相同 skill ID 和内容 hash。",
    "same_id_different_hash": "Hub 中已有同名 skill，但内容 hash 不同。",
    "missing_from_hub": "Hub 中没有这个 skill ID。",
}


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
        "status_metadata": STATUS_METADATA,
        "action_plan": _build_action_plan(items, hub_root),
        "items": sorted(items, key=_item_sort_key),
    }


def render_hub_import_diagnosis_text(diagnosis: Dict[str, object], *, max_items: int = 40) -> str:
    lines = [
        f"hub: {diagnosis.get('hub_root')}",
        f"hub_total: {diagnosis.get('hub_total')}",
        f"source_total: {diagnosis.get('source_total')}",
    ]
    summary = diagnosis.get("summary") if isinstance(diagnosis.get("summary"), dict) else {}
    lines.append(
        "summary: 已在 Hub={} 可更新={} 可导入={}".format(
            summary.get("already_in_hub", 0),
            summary.get("update_available", 0),
            summary.get("importable", 0),
        )
    )
    action_plan = diagnosis.get("action_plan") if isinstance(diagnosis.get("action_plan"), dict) else {}
    action_summary = action_plan.get("summary") if isinstance(action_plan.get("summary"), dict) else {}
    if action_summary:
        lines.append(
            "dry-run plan: 预演导入={} 更新前审查={} 选择来源={} 跳过={}".format(
                action_summary.get("preview_import", 0),
                action_summary.get("review_update", 0),
                action_summary.get("review_duplicate_import", 0),
                action_summary.get("skip_existing", 0),
            )
        )
    items = diagnosis.get("items") if isinstance(diagnosis.get("items"), list) else []
    if items:
        lines.append("")
        lines.append(f"items (first {min(max_items, len(items))}/{len(items)}):")
    for item in items[:max_items]:
        lines.append(
            "- {label}: {source}/{skill_id} -> {action}; {reason}".format(
                label=item.get("status_label"),
                source=item.get("source"),
                skill_id=item.get("skill_id"),
                action=item.get("operator_action"),
                reason=item.get("reason_label"),
            )
        )
    if len(items) > max_items:
        lines.append(f"... {len(items) - max_items} more")
    return "\n".join(lines)


def build_hub_import_preview_package(
    hub_root: Path = DEFAULT_HUB_ROOT,
    source_roots: Optional[Sequence[Tuple[str, Path]]] = None,
    *,
    out_dir: Path,
    max_diff_lines: int = 160,
) -> Dict[str, object]:
    diagnosis = build_hub_import_diagnosis(hub_root, source_roots=source_roots)
    out_dir = out_dir.expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    actions = []
    for action in diagnosis.get("action_plan", {}).get("actions", []):
        if not isinstance(action, dict) or action.get("action") == "skip_existing":
            continue
        actions.append(_preview_action(action, max_diff_lines=max_diff_lines))

    package = {
        "record_type": "skill-sync-hub-import-preview",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "dry_run",
        "writes_files": False,
        "hub_root": diagnosis.get("hub_root"),
        "source_roots": diagnosis.get("source_roots"),
        "diagnosis_summary": diagnosis.get("summary"),
        "action_summary": diagnosis.get("action_plan", {}).get("summary"),
        "review_required": diagnosis.get("action_plan", {}).get("review_required"),
        "safe_to_apply_automatically": False,
        "actions": actions,
        "skipped_existing": diagnosis.get("action_plan", {}).get("summary", {}).get("skip_existing", 0),
    }

    preview_json = out_dir / "preview.json"
    preview_md = out_dir / "preview.md"
    preview_json.write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    preview_md.write_text(render_hub_import_preview_markdown(package), encoding="utf-8")
    return {
        **package,
        "out_dir": str(out_dir),
        "preview_json": str(preview_json),
        "preview_md": str(preview_md),
    }


def render_hub_import_preview_text(package: Dict[str, object]) -> str:
    action_summary = package.get("action_summary") if isinstance(package.get("action_summary"), dict) else {}
    lines = [
        f"out_dir: {package.get('out_dir')}",
        f"preview_json: {package.get('preview_json')}",
        f"preview_md: {package.get('preview_md')}",
        "mode: dry_run",
        "actions: preview_import={} review_update={} review_duplicate_import={} skipped_existing={}".format(
            action_summary.get("preview_import", 0),
            action_summary.get("review_update", 0),
            action_summary.get("review_duplicate_import", 0),
            package.get("skipped_existing", 0),
        ),
        f"review_required: {package.get('review_required')}",
    ]
    return "\n".join(lines)


def render_hub_import_preview_markdown(package: Dict[str, object]) -> str:
    action_summary = package.get("action_summary") if isinstance(package.get("action_summary"), dict) else {}
    lines = [
        "# Skillshub Import Preview",
        "",
        "- mode: `dry_run`",
        "- writes_files: `false`",
        f"- hub_root: `{package.get('hub_root')}`",
        f"- preview_import: `{action_summary.get('preview_import', 0)}`",
        f"- review_update: `{action_summary.get('review_update', 0)}`",
        f"- review_duplicate_import: `{action_summary.get('review_duplicate_import', 0)}`",
        f"- skipped_existing: `{package.get('skipped_existing', 0)}`",
        "",
        "## Actions",
        "",
    ]
    actions = package.get("actions") if isinstance(package.get("actions"), list) else []
    if not actions:
        lines.append("No non-skip actions.")
        return "\n".join(lines) + "\n"
    for action in actions:
        lines.extend(_render_preview_action_markdown(action))
    return "\n".join(lines) + "\n"


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
        return _with_status_metadata({
            **base,
            "status": "already_in_hub",
            "hub_path": str(hub_same_path.path),
            "hub_hash": hub_same_path.content_hash,
            "reason_code": "same_resolved_path",
            "reason": "source path resolves to an existing Hub skill",
        })
    if hub_matches:
        hub_hashes = sorted({item.content_hash for item in hub_matches})
        hub_paths = [str(item.path) for item in hub_matches]
        if record.content_hash in hub_hashes:
            return _with_status_metadata({
                **base,
                "status": "already_in_hub",
                "hub_path": hub_paths[0],
                "hub_hash": record.content_hash,
                "hub_paths": hub_paths,
                "reason_code": "same_id_and_hash",
                "reason": "same skill_id and content_hash already exist in Hub",
            })
        return _with_status_metadata({
            **base,
            "status": "update_available",
            "hub_path": hub_paths[0],
            "hub_hashes": hub_hashes,
            "hub_paths": hub_paths,
            "reason_code": "same_id_different_hash",
            "reason": "same skill_id exists in Hub with a different content_hash",
        })
    return _with_status_metadata({
        **base,
        "status": "importable",
        "hub_path": None,
        "reason_code": "missing_from_hub",
        "reason": "skill_id does not exist in Hub",
    })


def _with_status_metadata(item: Dict[str, object]) -> Dict[str, object]:
    metadata = STATUS_METADATA.get(str(item.get("status")), {})
    reason_code = str(item.get("reason_code") or "")
    return {
        **item,
        "status_label": metadata.get("label", str(item.get("status") or "")),
        "operator_action": metadata.get("operator_action", "-"),
        "status_description": metadata.get("description", ""),
        "reason_label": REASON_LABELS.get(reason_code, str(item.get("reason") or "")),
    }


def _build_action_plan(items: Sequence[Dict[str, object]], hub_root: Path) -> Dict[str, object]:
    actions = [_build_action(item, hub_root) for item in items]
    summary: Dict[str, int] = {}
    for action in actions:
        action_id = str(action["action"])
        summary[action_id] = summary.get(action_id, 0) + 1
    review_required = sum(1 for action in actions if action.get("requires_review"))
    return {
        "mode": "dry_run",
        "safe_to_apply_automatically": False,
        "summary": dict(sorted(summary.items())),
        "review_required": review_required,
        "actions": sorted(actions, key=_action_sort_key),
    }


def _build_action(item: Dict[str, object], hub_root: Path) -> Dict[str, object]:
    status = str(item.get("status") or "")
    skill_id = str(item.get("skill_id") or "")
    duplicate_sources = item.get("duplicate_sources") if isinstance(item.get("duplicate_sources"), list) else []
    base = {
        "source": item.get("source"),
        "skill_id": skill_id,
        "source_path": item.get("path"),
        "status": status,
        "status_label": item.get("status_label"),
        "writes_files": False,
    }
    if status == "importable" and duplicate_sources:
        return {
            **base,
            "action": "review_duplicate_import",
            "action_label": "先选择来源",
            "target_path": str(hub_root / skill_id),
            "requires_review": True,
            "reason": "多个外部来源提供同名 skill；导入前需要选定 canonical 来源。",
        }
    if status == "importable":
        return {
            **base,
            "action": "preview_import",
            "action_label": "预演导入",
            "target_path": str(hub_root / skill_id),
            "requires_review": False,
            "reason": "Hub 中不存在该 skill；当前仅生成导入预演，不写入文件。",
        }
    if status == "update_available":
        return {
            **base,
            "action": "review_update",
            "action_label": "更新前审查",
            "target_path": item.get("hub_path"),
            "requires_review": True,
            "reason": "Hub 中已有同名 skill 且内容不同；更新前必须查看差异并显式确认。",
        }
    return {
        **base,
        "action": "skip_existing",
        "action_label": "跳过",
        "target_path": item.get("hub_path"),
        "requires_review": False,
        "reason": "Hub 中已有相同 skill；不需要导入。",
    }


def _item_sort_key(item: Dict[str, object]) -> Tuple[int, str, str, str]:
    rank = {"importable": 0, "update_available": 1, "already_in_hub": 2}
    return (
        rank.get(str(item.get("status")), 9),
        str(item.get("source")),
        str(item.get("skill_id")),
        str(item.get("path")),
    )


def _action_sort_key(action: Dict[str, object]) -> Tuple[int, str, str, str]:
    rank = {"preview_import": 0, "review_duplicate_import": 1, "review_update": 2, "skip_existing": 3}
    return (
        rank.get(str(action.get("action")), 9),
        str(action.get("source")),
        str(action.get("skill_id")),
        str(action.get("source_path")),
    )


def _preview_action(action: Dict[str, object], *, max_diff_lines: int) -> Dict[str, object]:
    source_path = Path(str(action.get("source_path"))).expanduser()
    source_record = _scan_preview_record(str(action.get("source") or "source"), source_path)
    preview = {
        **action,
        "source_hash": source_record.content_hash if source_record else None,
        "source_file_count": source_record.file_count if source_record else None,
        "source_size_bytes": source_record.size_bytes if source_record else None,
        "source_files": [file.__dict__ for file in source_record.files] if source_record else [],
        "errors": [] if source_record else [f"cannot scan source skill at {source_path}"],
    }
    if action.get("action") == "review_update" and action.get("target_path"):
        target_path = Path(str(action.get("target_path"))).expanduser()
        hub_record = _scan_preview_record("hub", target_path)
        preview.update(
            {
                "hub_hash": hub_record.content_hash if hub_record else None,
                "hub_file_count": hub_record.file_count if hub_record else None,
                "hub_size_bytes": hub_record.size_bytes if hub_record else None,
                "hub_files": [file.__dict__ for file in hub_record.files] if hub_record else [],
                "skill_md_diff": _diff_skill_md(target_path, source_path, max_lines=max_diff_lines),
            }
        )
        if not hub_record:
            preview.setdefault("errors", []).append(f"cannot scan hub skill at {target_path}")
    return preview


def _scan_preview_record(source: str, skill_dir: Path) -> Optional[SkillRecord]:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None
    try:
        return scan_skill(source, skill_dir.resolve(), skill_md.resolve())
    except OSError:
        return None


def _diff_skill_md(old_dir: Path, new_dir: Path, *, max_lines: int) -> Dict[str, object]:
    old_file = old_dir / "SKILL.md"
    new_file = new_dir / "SKILL.md"
    try:
        old_lines = old_file.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        new_lines = new_file.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError as exc:
        return {"ok": False, "error": str(exc), "lines": [], "truncated": False}
    diff_lines = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=str(old_file),
            tofile=str(new_file),
            lineterm="",
        )
    )
    truncated = len(diff_lines) > max_lines
    if truncated:
        diff_lines = diff_lines[:max_lines]
    return {"ok": True, "lines": [line.rstrip("\n") for line in diff_lines], "truncated": truncated}


def _render_preview_action_markdown(action: Dict[str, object]) -> List[str]:
    lines = [
        f"### {action.get('skill_id')} - {action.get('action_label')}",
        "",
        f"- action: `{action.get('action')}`",
        f"- source: `{action.get('source')}`",
        f"- source_path: `{action.get('source_path')}`",
        f"- target_path: `{action.get('target_path')}`",
        f"- requires_review: `{str(action.get('requires_review')).lower()}`",
        f"- source_hash: `{action.get('source_hash')}`",
        f"- source_files: `{action.get('source_file_count')}`",
        f"- reason: {action.get('reason')}",
        "",
    ]
    diff = action.get("skill_md_diff") if isinstance(action.get("skill_md_diff"), dict) else None
    if diff and diff.get("lines"):
        lines.extend(["```diff", *[str(line) for line in diff.get("lines", [])], "```", ""])
        if diff.get("truncated"):
            lines.extend(["Diff truncated.", ""])
    return lines


def _resolved_path(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)
