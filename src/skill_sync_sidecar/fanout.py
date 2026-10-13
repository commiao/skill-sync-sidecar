"""正本 → 各工具的扇出（T-0260 第 4 步）。本模块目前只出计划，不写任何文件。

写入方式按工具配置：
- link：工具目录里放一个指向正本技能目录的软链。「指向正本的软链」就是 sidecar
  管的标志，不需要台账；正本更新各工具即时生效；正本删掉后清理失效链接。
- copy：复制一份实体目录（Cursor 在验证能加载软链技能之前用这个）。复制品是不是
  sidecar 装的，只能靠台账（状态目录里的 fanout/<tool>.json）判断。

不碰的东西：正本以外来源的技能、指向其他管理者（如 ~/.agents/skills）的软链、
内容与正本不同的同名技能（只报告），以及 project scope 的技能。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .config import ConfigError, LEGACY_LOCAL_ROOT, _read_config, default_config_path, sidecar_state_dir
from .model import SkillRecord
from .scanner import scan_roots, scan_skill

HOME = Path.home()


@dataclass(frozen=True)
class FanoutTool:
    tool_id: str
    root: Path
    mode: str  # "link" | "copy"
    aliases: frozenset


DEFAULT_TOOLS: Sequence[FanoutTool] = (
    FanoutTool("codex", HOME / ".codex" / "skills", "link", frozenset({"codex"})),
    # 用户技能目录；~/.cursor/skills-cursor 是 Cursor 自己的内置区，不写
    FanoutTool("cursor", HOME / ".cursor" / "skills", "copy", frozenset({"cursor"})),
    FanoutTool("claude-code", HOME / ".claude" / "skills", "link", frozenset({"claude-code", "claude"})),
    FanoutTool("qoder", HOME / ".qoder" / "skills", "link", frozenset({"qoder"})),
    FanoutTool("deepseek-harness", HOME / ".dsh" / "skills", "link", frozenset({"deepseek-harness", "deepseek"})),
    # 过渡期：cc-switch 不再是正本后仍有工具软链到它的目录，扇出让它继续收到更新
    FanoutTool("cc-switch", LEGACY_LOCAL_ROOT, "link", frozenset({"cc-switch"})),
)

# manifest 没写 targets 的技能只维护它已经在的工具（保持现状），不往新工具里加：
# 2026-10-13 实测，按「全部工具」默认会让每个工具一次多出 44~46 个技能
#（docs/cross-tool-projection-20260624.md 也写了不要批量投放）。要进新工具就在
# manifest 里显式写 targets。

# 这些根下的同内容技能可以被「认领」：换成指向正本的软链（或复制品）
ADOPTABLE_ROOTS = (HOME / ".skillshub", LEGACY_LOCAL_ROOT)


def configured_tools(config_path: Optional[Path] = None) -> List[FanoutTool]:
    """config.json 的 fanout.tools.<tool_id> 可设 {"mode": "link"|"copy", "enabled": false}。"""
    fanout = _read_config(config_path or default_config_path()).get("fanout", {})
    if not isinstance(fanout, dict):
        raise ConfigError("sidecar config fanout must be an object")
    overrides = fanout.get("tools", {})
    if not isinstance(overrides, dict):
        raise ConfigError("sidecar config fanout.tools must be an object")
    tools = []
    for tool in DEFAULT_TOOLS:
        override = overrides.get(tool.tool_id) or {}
        if not isinstance(override, dict):
            raise ConfigError(f"sidecar config fanout.tools.{tool.tool_id} must be an object")
        if override.get("enabled") is False:
            continue
        mode = override.get("mode", tool.mode)
        if mode not in {"link", "copy"}:
            raise ConfigError(f"sidecar config fanout.tools.{tool.tool_id}.mode must be link or copy")
        tools.append(FanoutTool(tool.tool_id, tool.root, mode, tool.aliases))
    return tools


def ledger_path(tool_id: str) -> Path:
    return sidecar_state_dir() / "fanout" / f"{tool_id}.json"


def load_ledger(tool_id: str, path: Optional[Path] = None) -> Dict[str, str]:
    """copy 模式的台账：{条目名: 装进去时的 content_hash}。"""
    target = path or ledger_path(tool_id)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ConfigError(f"fanout ledger is not valid JSON: {target}") from exc
    entries = data.get("entries", {}) if isinstance(data, dict) else {}
    return {str(k): str(v.get("content_hash")) for k, v in entries.items() if isinstance(v, dict)}


def _declared_targets(skill: SkillRecord) -> Optional[List[str]]:
    if not skill.manifest_path:
        return None
    try:
        data = json.loads(Path(skill.manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    targets = data.get("targets") if isinstance(data, dict) else None
    return [str(t) for t in targets] if isinstance(targets, list) and targets else None


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _hash_of(path: Path) -> Optional[str]:
    resolved = path.resolve()
    if not (resolved / "SKILL.md").is_file():
        return None
    return scan_skill("fanout", resolved, resolved / "SKILL.md").content_hash


def _skill_decision(skill: SkillRecord, canonical: Path, tool: FanoutTool, entry: Path) -> Optional[str]:
    """返回不投放的原因；None 表示要投放到这个工具。"""
    if skill.path.parent.resolve() != canonical.resolve():
        return "inside_another_skill"
    if skill.scope != "global":
        return "project_scope"
    if skill.risk_level == "error":
        return "risk_error"
    declared = _declared_targets(skill)
    if declared is None:
        present = entry.exists() or entry.is_symlink()
        return None if present else "undeclared_not_present"
    return None if tool.aliases & set(declared) else "not_targeted"


def _plan_entry(skill: SkillRecord, entry: Path, canonical: Path, tool: FanoutTool, ledger: Dict[str, str]) -> Dict[str, object]:
    source = skill.path.resolve()
    item: Dict[str, object] = {"name": entry.name, "skill_id": skill.skill_id, "risk_level": skill.risk_level}
    if not entry.exists() and not entry.is_symlink():
        item["action"] = "link" if tool.mode == "link" else "copy"
        return item
    if entry.is_symlink():
        target = Path(os.path.realpath(entry))
        item["link_target"] = os.readlink(entry)
        if target == source:
            item["action"] = "ok" if tool.mode == "link" else "replace_link_with_copy"
            return item
        if _within(target, canonical.resolve()):
            item["action"] = "relink" if tool.mode == "link" else "replace_link_with_copy"
            return item
        adoptable = any(_within(target, root.resolve()) for root in ADOPTABLE_ROOTS if root.exists())
        if not adoptable:
            item["action"] = "foreign_link"
            return item
        same = _hash_of(entry) == skill.content_hash
        item["action"] = ("relink" if tool.mode == "link" else "replace_link_with_copy") if same else "conflict"
        if not same:
            item["reason"] = "linked content differs from canonical"
        return item
    if not entry.is_dir():
        item["action"] = "conflict"
        item["reason"] = "a non-directory is in the way"
        return item
    current = _hash_of(entry)
    if tool.mode == "link":
        item["action"] = "replace_dir_with_link" if current == skill.content_hash else "conflict"
        if item["action"] == "conflict":
            item["reason"] = "existing directory differs from canonical"
        return item
    recorded = ledger.get(entry.name)
    if current == skill.content_hash:
        item["action"] = "ok" if recorded == current else "adopt"
    elif recorded is not None and recorded == current:
        item["action"] = "update_copy"
    else:
        item["action"] = "conflict"
        item["reason"] = "existing directory differs from canonical and is not a copy the sidecar recorded"
    return item


def _removals(tool: FanoutTool, canonical: Path, wanted: Iterable[str], ledger: Dict[str, str]) -> List[Dict[str, object]]:
    wanted = set(wanted)
    items = []
    if not tool.root.exists():
        return items
    for entry in sorted(tool.root.iterdir()):
        if entry.name.startswith(".") or entry.name in wanted:
            continue
        if entry.is_symlink():
            raw = Path(os.readlink(entry))
            target = raw if raw.is_absolute() else (entry.parent / raw)
            if _within(Path(os.path.normpath(target)), canonical) or _within(Path(os.path.normpath(target)), canonical.resolve()):
                if entry.exists():
                    continue
                items.append({"name": entry.name, "action": "remove_dangling_link", "link_target": str(raw)})
        elif tool.mode == "copy" and entry.name in ledger and entry.is_dir():
            if _hash_of(entry) == ledger[entry.name]:
                items.append({"name": entry.name, "action": "remove_copy"})
            else:
                items.append({"name": entry.name, "action": "conflict", "reason": "recorded copy was modified; not removed"})
    return items


def build_fanout_plan(
    canonical_root: Path,
    tools: Optional[Sequence[FanoutTool]] = None,
    tool_ids: Optional[Sequence[str]] = None,
    ledgers: Optional[Dict[str, Dict[str, str]]] = None,
) -> Dict[str, object]:
    canonical = canonical_root.expanduser()
    tools = list(tools if tools is not None else configured_tools())
    if tool_ids:
        unknown = set(tool_ids) - {t.tool_id for t in tools}
        if unknown:
            raise ConfigError(f"unknown or disabled fanout tool: {', '.join(sorted(unknown))}")
        tools = [t for t in tools if t.tool_id in set(tool_ids)]
    skills = scan_roots([f"canonical={canonical}"]).skills
    result_tools = []
    for tool in tools:
        root = tool.root.expanduser()
        report: Dict[str, object] = {"tool_id": tool.tool_id, "root": str(root), "mode": tool.mode}
        if root.resolve() == canonical.resolve():
            report["skipped"] = "tool root is the canonical root"
            result_tools.append(report)
            continue
        if not root.exists():
            report["skipped"] = "tool root not found on this device"
            result_tools.append(report)
            continue
        ledger = (ledgers or {}).get(tool.tool_id)
        if ledger is None:
            ledger = load_ledger(tool.tool_id) if tool.mode == "copy" else {}
        items, not_placed, wanted = [], {}, set()
        for skill in skills:
            entry = root / skill.path.name
            reason = _skill_decision(skill, canonical, tool, entry)
            if reason:
                not_placed[reason] = not_placed.get(reason, 0) + 1
                continue
            wanted.add(entry.name)
            items.append(_plan_entry(skill, entry, canonical, tool, ledger))
        items.extend(_removals(tool, canonical, wanted, ledger))
        summary: Dict[str, int] = {}
        for item in items:
            summary[str(item["action"])] = summary.get(str(item["action"]), 0) + 1
        report.update(
            summary=dict(sorted(summary.items())),
            not_placed=dict(sorted(not_placed.items())),
            warnings=sorted(str(i["name"]) for i in items if i.get("risk_level") == "warning" and i["action"] not in {"ok", "foreign_link", "conflict"}),
            items=items,
        )
        result_tools.append(report)
    return {"canonical_root": str(canonical), "skills": len(skills), "dry_run": True, "tools": result_tools}
