from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .scanner import scan_roots


class ProjectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolAdapter:
    tool_id: str
    name: str
    roots: List[Path]
    target_aliases: List[str]
    supported_scopes: List[str]


def default_tool_adapters() -> List[ToolAdapter]:
    home = Path.home()
    return [
        ToolAdapter("cc-switch", "cc-switch", [home / ".cc-switch" / "skills"], ["cc-switch"], ["global", "project"]),
        ToolAdapter("skillshub", "skillshub", [home / ".skillshub"], ["skillshub"], ["global"]),
        ToolAdapter("codex", "Codex", [home / ".codex" / "skills", home / ".agents" / "skills"], ["codex"], ["global"]),
        ToolAdapter("cursor", "Cursor", [home / ".cursor" / "skills-cursor"], ["cursor"], ["global"]),
        ToolAdapter("claude-code", "Claude Code", [home / ".claude" / "skills"], ["claude-code", "claude"], ["global"]),
    ]


def parse_tool_adapter_spec(spec: str) -> ToolAdapter:
    if "=" not in spec:
        raise ValueError(f"tool adapter must be id=/path[,/path]: {spec}")
    tool_id, raw_paths = spec.split("=", 1)
    tool_id = tool_id.strip()
    roots = [Path(item).expanduser() for item in raw_paths.split(",") if item.strip()]
    if not tool_id or not roots:
        raise ValueError(f"tool adapter must be id=/path[,/path]: {spec}")
    defaults = {adapter.tool_id: adapter for adapter in default_tool_adapters()}
    default = defaults.get(tool_id)
    return ToolAdapter(
        tool_id=tool_id,
        name=default.name if default else tool_id,
        roots=roots,
        target_aliases=default.target_aliases if default else [tool_id],
        supported_scopes=default.supported_scopes if default else ["global"],
    )


def build_tool_projection(snapshot_dir: Path, adapters: Optional[Sequence[ToolAdapter]] = None) -> Dict[str, object]:
    snapshot_dir = snapshot_dir.expanduser()
    index = _load_snapshot_index(snapshot_dir)
    canonical_skills = [item for item in index.get("skills", []) if isinstance(item, dict)]
    tool_results = [_project_tool(canonical_skills, adapter) for adapter in (adapters or default_tool_adapters())]
    return {
        "record_type": "skill-sync-tool-projection",
        "snapshot_id": index.get("snapshot_id"),
        "canonical_total": len(canonical_skills),
        "tools": tool_results,
    }


def _load_snapshot_index(snapshot_dir: Path) -> Dict[str, object]:
    index_path = snapshot_dir / "index.json"
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProjectionError(f"snapshot index not found: {index_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectionError(f"snapshot index is unreadable: {index_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ProjectionError(f"snapshot index must contain a JSON object: {index_path}")
    return data


def _project_tool(canonical_skills: Sequence[dict], adapter: ToolAdapter) -> Dict[str, object]:
    installed = _installed_by_skill_id(adapter)
    items = [_project_skill(skill, adapter, installed.get(str(skill.get("skill_id") or ""))) for skill in canonical_skills]
    summary: Dict[str, int] = {}
    for item in items:
        summary[item["status"]] = summary.get(item["status"], 0) + 1
    canonical_ids = {str(skill.get("skill_id") or "") for skill in canonical_skills}
    extra_local = [
        {
            "skill_id": skill_id,
            "paths": [entry["path"] for entry in entries],
            "content_hashes": sorted({str(entry["content_hash"]) for entry in entries}),
        }
        for skill_id, entries in sorted(installed.items())
        if skill_id not in canonical_ids
    ]
    actionable = int(summary.get("missing", 0)) + int(summary.get("drift", 0))
    return {
        "id": adapter.tool_id,
        "name": adapter.name,
        "roots": [str(root) for root in adapter.roots],
        "target_aliases": adapter.target_aliases,
        "supported_scopes": adapter.supported_scopes,
        "installed_total": sum(len(entries) for entries in installed.values()),
        "canonical_targeted": sum(1 for item in items if item["status"] not in {"not_targeted"}),
        "actionable": actionable,
        "summary": dict(sorted(summary.items())),
        "items": items,
        "extra_local": extra_local,
    }


def _installed_by_skill_id(adapter: ToolAdapter) -> Dict[str, List[Dict[str, object]]]:
    root_specs = [f"{adapter.tool_id}-{index}={root}" for index, root in enumerate(adapter.roots) if root.exists()]
    if not root_specs:
        return {}
    summary = scan_roots(root_specs)
    installed: Dict[str, List[Dict[str, object]]] = {}
    for skill in summary.skills:
        installed.setdefault(skill.skill_id, []).append(
            {
                "path": str(skill.path),
                "content_hash": skill.content_hash,
                "risk_level": skill.risk_level,
            }
        )
    return installed


def _project_skill(skill: dict, adapter: ToolAdapter, installed_entries: Optional[List[Dict[str, object]]]) -> Dict[str, object]:
    skill_id = str(skill.get("skill_id") or "")
    scope = str(skill.get("scope") or "global")
    targets = [str(item) for item in _list_value(skill.get("targets"))]
    risk_level = str(skill.get("risk_level") or "unknown")
    content_hash = str(skill.get("content_hash") or "")
    installed_hashes = [str(entry.get("content_hash") or "") for entry in installed_entries or []]
    base = {
        "skill_id": skill_id,
        "scope": scope,
        "targets": targets,
        "risk_level": risk_level,
        "canonical_hash": content_hash,
        "installed_hashes": installed_hashes,
        "installed_paths": [str(entry.get("path") or "") for entry in installed_entries or []],
    }
    if not _targets_tool(targets, adapter):
        return {**base, "status": "not_targeted", "reason": f"manifest targets do not include {adapter.tool_id}"}
    if scope not in adapter.supported_scopes:
        return {**base, "status": "unsupported_scope", "reason": f"{scope}-scoped skills are not installed into {adapter.tool_id} global root"}
    if risk_level == "error":
        return {**base, "status": "blocked_error", "reason": "skill has validation errors"}
    if not installed_entries:
        return {**base, "status": "missing", "reason": "target tool does not have this canonical skill"}
    if content_hash in installed_hashes:
        return {**base, "status": "installed", "reason": "target tool has the canonical hash"}
    return {**base, "status": "drift", "reason": "target tool has the skill id but a different content hash"}


def _targets_tool(targets: Sequence[str], adapter: ToolAdapter) -> bool:
    normalized_targets = {target.strip().lower() for target in targets}
    aliases = {alias.strip().lower() for alias in adapter.target_aliases}
    return bool(normalized_targets & aliases)


def _list_value(value: object) -> Iterable[object]:
    return value if isinstance(value, list) else []

