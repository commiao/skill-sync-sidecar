from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Iterable, Mapping, Optional

from .scanner import scan_roots


ToolRoot = tuple[str, str, tuple[Path, ...], str]


DEFAULT_TOOL_ROOTS: tuple[ToolRoot, ...] = (
    ("cc-switch", "cc-switch", (Path.home() / ".cc-switch" / "skills",), "主同步目录"),
    ("skillshub", "skillshub", (Path.home() / ".skillshub",), "工具技能目录"),
    ("codex", "Codex", (Path.home() / ".codex" / "skills", Path.home() / ".agents" / "skills"), "Codex 可发现目录"),
    ("cursor", "Cursor", (Path.home() / ".cursor" / "skills-cursor",), "Cursor 技能目录"),
    ("claude-code", "Claude Code", (Path.home() / ".claude" / "skills",), "Claude Code 技能目录"),
    ("qoder", "Qoder", (Path.home() / ".qoder" / "skills",), "Qoder 技能目录"),
    ("deepseek-harness", "DeepSeek Harness", (Path.home() / ".dsh" / "skills",), "DeepSeek Harness 技能目录"),
)


def select_device_tool_roots(root_overrides: Mapping[str, Iterable[Path]]) -> tuple[ToolRoot, ...]:
    """Build a restricted tool list for an Agent that owns only selected roots."""
    defaults = {tool_id for tool_id, _name, _roots, _role in DEFAULT_TOOL_ROOTS}
    unknown = sorted(set(root_overrides) - defaults)
    if unknown:
        raise ValueError(f"unknown tool id(s): {', '.join(unknown)}")

    selected: list[ToolRoot] = []
    for tool_id, name, _default_roots, role in DEFAULT_TOOL_ROOTS:
        if tool_id not in root_overrides:
            continue
        roots = tuple(Path(root).expanduser() for root in root_overrides[tool_id])
        if not roots:
            raise ValueError(f"tool root list cannot be empty: {tool_id}")
        selected.append((tool_id, name, roots, role))
    return tuple(selected)


def build_device_tool_status(
    tool_roots: Optional[Iterable[tuple[str, str, Iterable[Path], str]]] = None,
    *,
    plugin_manifests: Optional[Mapping[str, Path]] = None,
    measured_at: Optional[str] = None,
) -> list[dict]:
    measured = measured_at or datetime.now(timezone.utc).isoformat()
    tools = []
    for tool_id, name, roots, role in tool_roots or DEFAULT_TOOL_ROOTS:
        root_paths = [Path(root).expanduser() for root in roots]
        installed_paths = [path for path in root_paths if path.exists()]
        plugins = _tool_plugin_inventory((plugin_manifests or {}).get(tool_id))
        installed = bool(installed_paths) or bool(plugins and plugins.get("state") == "detected")
        count = 0
        risk = {"ok": 0, "warning": 0, "error": 0}
        skill_items: list[dict] = []
        if installed:
            try:
                for index, path in enumerate(installed_paths):
                    data = scan_roots([f"{tool_id}-{index}={path}"], follow_symlinks=True).to_dict()
                    count += int(data.get("total", 0))
                    by_risk = dict(data.get("by_risk", {}))
                    for key in risk:
                        risk[key] += int(by_risk.get(key, 0))
                    skill_items.extend(_tool_skill_items(data))
            except Exception as exc:  # pragma: no cover - dashboard telemetry should stay best-effort
                tools.append(
                    {
                        "id": tool_id,
                        "name": name,
                        "roots": [str(path) for path in root_paths],
                        "path": ", ".join(str(path) for path in root_paths),
                        "role": role,
                        "installed": True,
                        "state": "error",
                        "skills": 0,
                        "skill_items": [],
                        "risk": {},
                        "plugins": plugins,
                        "measured_at": measured,
                        "note": str(exc),
                    }
                )
                continue
        tools.append(
            {
                "id": tool_id,
                "name": name,
                "roots": [str(path) for path in root_paths],
                "path": ", ".join(str(path) for path in root_paths),
                "role": role,
                "installed": installed,
                "state": "detected" if installed else "not_found",
                "skills": count,
                "skill_items": sorted(skill_items, key=lambda item: str(item.get("skill_id") or "")),
                "risk": risk,
                "plugins": plugins,
                "measured_at": measured,
                "note": _tool_status_note(installed_paths, plugins),
            }
        )
    return tools


def build_device_status(peer_id: str, *, name: Optional[str] = None, measured_at: Optional[str] = None) -> dict:
    return {
        "id": peer_id,
        "name": name or _device_name(peer_id),
        "kind": "agent",
        "measured_at": measured_at or datetime.now(timezone.utc).isoformat(),
    }


def build_peer_capabilities(*, sync_status: bool = True, blocked_report: bool = True) -> dict:
    return {
        "tool_status": True,
        "sync_status": sync_status,
        "blocked_report": blocked_report,
    }


def _device_name(peer_id: str) -> str:
    if peer_id == "mac":
        return "Mac 本机"
    if peer_id in {"oc-vps", "openclaw"}:
        return "oc-vps / OpenClaw"
    if peer_id == "win":
        return "Windows"
    return peer_id


def _tool_skill_items(scan_data: dict) -> list[dict]:
    items: list[dict] = []
    for skill in scan_data.get("skills", []):
        if not isinstance(skill, dict):
            continue
        items.append(
            {
                "skill_id": skill.get("skill_id"),
                "name": skill.get("name"),
                "description": skill.get("description"),
                "scope": skill.get("scope"),
                "path": skill.get("path"),
                "content_hash": skill.get("content_hash"),
                "risk_level": skill.get("risk_level"),
                "targets": skill.get("targets") or [],
            }
        )
    return items


def _tool_plugin_inventory(manifest_path: Optional[Path]) -> Optional[dict]:
    if manifest_path is None:
        return None
    path = Path(manifest_path).expanduser()
    payload = {"path": str(path), "state": "not_found", "count": 0, "items": [], "note": "未检测到插件清单"}
    if not path.is_file():
        return payload
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        dependencies = data.get("dependencies") if isinstance(data, dict) else None
        if not isinstance(dependencies, dict):
            raise ValueError("dependencies is not an object")
        items = [
            {"name": str(name), "version": str(version)}
            for name, version in dependencies.items()
            if isinstance(name, str) and isinstance(version, (str, int, float))
        ]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        payload.update({"state": "error", "note": f"无法读取插件清单：{exc}"})
        return payload
    payload.update({"state": "detected", "count": len(items), "items": sorted(items, key=lambda item: item["name"]), "note": "已读取插件清单"})
    return payload


def _tool_status_note(installed_paths: list[Path], plugins: Optional[dict]) -> str:
    plugin_count = plugins.get("count") if isinstance(plugins, dict) else None
    plugin_detected = isinstance(plugin_count, int) and plugins.get("state") == "detected"
    if installed_paths and plugin_detected:
        return f"已检测到技能目录和 {plugin_count} 个插件"
    if installed_paths:
        return "已检测到目录"
    if plugin_detected:
        return f"已检测到 {plugin_count} 个插件；未检测到技能目录"
    return "未检测到目录"
