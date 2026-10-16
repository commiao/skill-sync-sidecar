"""正本 → 各工具的扇出（T-0260 第 4 步）。build_fanout_plan 只出计划；run_fanout 执行。

写入方式按工具配置：
- link：工具目录里放一个指向正本技能目录的软链。「指向正本的软链」就是 sidecar
  管的标志，不需要台账；正本更新各工具即时生效；正本删掉后清理失效链接。
- copy：复制一份实体目录（Cursor 在验证能加载软链技能之前用这个）。复制品是不是
  sidecar 装的，只能靠台账（状态目录里的 fanout/<tool>.json）判断。

不碰的东西：正本以外来源的技能、指向其他管理者（如 ~/.agents/skills）的软链、
内容与正本不同的同名技能（只报告），以及 project scope 的技能。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .config import ConfigError, LEGACY_LOCAL_ROOT, _read_config, default_config_path, sidecar_state_dir
from .model import SkillRecord
from .protect import make_writable, move_path, protect_skill_dirs, remove_path
from .scanner import scan_roots

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
    """整树哈希：技能自身 + 嵌套子技能。

    scanner 的 content_hash 不含嵌套子技能（如 gstack/ 下那批），只比它会把子技能
    改过的复制品判成「一致」，复制模式就永远不更新了。
    """
    resolved = path.resolve()
    if not (resolved / "SKILL.md").is_file():
        return None
    pairs = sorted(
        (str(skill.path.relative_to(resolved)), skill.content_hash)
        for skill in scan_roots([f"fanout={resolved}"]).skills
    )
    return hashlib.sha256(json.dumps(pairs).encode("utf-8")).hexdigest()


# 认领（把工具里已有的同名技能换成指向正本的软链或认作复制品）时，这些差异不算
# 「内容不同」：manifest.json 是 sidecar 写的元数据（skillshub 迁移工具比较时同样
# 忽略它），点文件是打包残留（.gitignore、.npmignore 等），技能本身一字未变。
# 2026-10-16 实测：当时的 4 个「冲突」全是这种情况。被替换的东西照样先备份。
ADOPTION_IGNORED_FILES = {"manifest.json"}
ADOPTION_IGNORED_DIRS = {"__pycache__", "node_modules"}


def _adoption_hash(path: Path) -> Optional[str]:
    resolved = path.resolve()
    if not (resolved / "SKILL.md").is_file():
        return None
    entries = []
    for root, dirs, files in os.walk(resolved):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in ADOPTION_IGNORED_DIRS)
        for name in sorted(files):
            if name.startswith(".") or name.endswith(".pyc") or name in ADOPTION_IGNORED_FILES:
                continue
            file_path = Path(root) / name
            entries.append((str(file_path.relative_to(resolved)), hashlib.sha256(file_path.read_bytes()).hexdigest()))
    return hashlib.sha256(json.dumps(entries).encode("utf-8")).hexdigest()


def _same_content(entry: Path, canonical_hash: Optional[str], source: Path) -> bool:
    if _hash_of(entry) == canonical_hash:
        return True
    return _adoption_hash(entry) == _adoption_hash(source)


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
    canonical_hash = _hash_of(source)
    item: Dict[str, object] = {
        "name": entry.name,
        "skill_id": skill.skill_id,
        "risk_level": skill.risk_level,
        "content_hash": canonical_hash,
        "source_path": str(source),
    }
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
        same = _same_content(entry, canonical_hash, source)
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
        item["action"] = "replace_dir_with_link" if _same_content(entry, canonical_hash, source) else "conflict"
        if item["action"] == "conflict":
            item["reason"] = "existing directory differs from canonical"
        return item
    recorded = ledger.get(entry.name)
    if current == canonical_hash or (recorded is None and _same_content(entry, canonical_hash, source)):
        # 认领时台账记的是复制品自己的哈希，之后正本一变就按 update_copy 跟上
        item["action"] = "ok" if recorded == current else "adopt"
        item["content_hash"] = current
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


# ---- 执行（PR B）----------------------------------------------------------------
#
# 每个工具在锁内重新出计划再执行，计划与执行之间不留可被别的进程插入的窗口。
# 备份放在 sidecar 状态目录（不放工具目录，免得被工具当技能扫到），每次运行写
# runs/<run_id>.json，可用 rollback_fanout 按记录撤销。


NOOP_ACTIONS = {"ok", "conflict", "foreign_link"}
KEEP_BACKUP_RUNS = 5
# daemon 每小时例行跑一次就写一份记录（约 10KB）；只留最近这么多份，约一周
KEEP_RUN_RECORDS = 200


class FanoutError(RuntimeError):
    pass


def fanout_dir(state_dir: Optional[Path] = None) -> Path:
    return (state_dir or sidecar_state_dir()) / "fanout"


@contextmanager
def tool_lock(tool_id: str, state_dir: Optional[Path] = None, timeout: float = 30.0):
    lock_path = fanout_dir(state_dir) / "locks" / f"{tool_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise FanoutError(f"another fanout is writing {tool_id}; gave up after {timeout:.0f}s")
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _save_ledger(tool_id: str, ledger: Dict[str, str], state_dir: Optional[Path]) -> None:
    entries = {name: {"content_hash": h} for name, h in sorted(ledger.items())}
    _write_json(fanout_dir(state_dir) / f"{tool_id}.json", {"tool_id": tool_id, "entries": entries})


def _swap_symlink(entry: Path, target: str, run_id: str) -> None:
    """原子地让 entry 成为指向 target 的软链（entry 不存在或本身是软链时）。"""
    tmp = entry.parent / f".{entry.name}.skill-sync-tmp-{run_id}"
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, entry)


def _copy_into(source: Path, entry: Path, run_id: str) -> Path:
    tmp = entry.parent / f".{entry.name}.skill-sync-tmp-{run_id}"
    if tmp.exists() or tmp.is_symlink():
        remove_path(tmp)
    shutil.copytree(source, tmp, symlinks=True)
    # 正本是只读的，copytree 连权限一起复制；macOS 上只读目录连同一父目录内改名都会
    # 被拒（2026-10-16 Cursor 的 kg-use 就这样失败）。复制品属于工具，不需要只读
    make_writable(tmp)
    return tmp


def _execute_item(tool: FanoutTool, item: Dict[str, object], ledger: Dict[str, str], run_id: str, backup_root: Path) -> Dict[str, object]:
    action = str(item["action"])
    entry = tool.root.expanduser() / str(item["name"])
    source = Path(str(item.get("source_path", "")))
    done = {"name": item["name"], "action": action, "content_hash": item.get("content_hash")}
    backup = backup_root / str(item["name"])
    if action == "link":
        if entry.exists() or entry.is_symlink():
            raise FanoutError(f"{entry} appeared since planning")
        _swap_symlink(entry, str(source), run_id)
    elif action == "relink":
        done["old_link_target"] = os.readlink(entry)
        _swap_symlink(entry, str(source), run_id)
    elif action == "replace_dir_with_link":
        move_path(entry, backup)
        done["backup_path"] = str(backup)
        try:
            _swap_symlink(entry, str(source), run_id)
        except OSError:
            move_path(backup, entry)
            raise
    elif action == "copy":
        tmp = _copy_into(source, entry, run_id)
        if entry.exists() or entry.is_symlink():
            remove_path(tmp)
            raise FanoutError(f"{entry} appeared since planning")
        try:
            tmp.rename(entry)
        except OSError:
            remove_path(tmp)
            raise
        ledger[entry.name] = str(item["content_hash"])
    elif action == "update_copy":
        tmp = _copy_into(source, entry, run_id)
        try:
            move_path(entry, backup)
            done["backup_path"] = str(backup)
            tmp.rename(entry)
        except OSError:
            if not entry.exists() and backup.exists():
                move_path(backup, entry)
            remove_path(tmp)
            raise
        ledger[entry.name] = str(item["content_hash"])
    elif action == "adopt":
        ledger[entry.name] = str(item["content_hash"])  # 复制品当前的整树哈希（见 _plan_entry）
    elif action == "replace_link_with_copy":
        done["old_link_target"] = os.readlink(entry)
        tmp = _copy_into(source, entry, run_id)
        entry.unlink()
        try:
            tmp.rename(entry)
        except OSError:
            os.symlink(str(done["old_link_target"]), entry)
            remove_path(tmp)
            raise
        ledger[entry.name] = str(item["content_hash"])
    elif action == "remove_dangling_link":
        done["old_link_target"] = os.readlink(entry)
        entry.unlink()
    elif action == "remove_copy":
        move_path(entry, backup)
        done["backup_path"] = str(backup)
        ledger.pop(entry.name, None)
    else:
        raise FanoutError(f"unknown fanout action: {action}")
    return done


def _prune_backups(state_dir: Optional[Path], keep: int = KEEP_BACKUP_RUNS) -> List[str]:
    root = fanout_dir(state_dir) / "backups"
    if not root.is_dir():
        return []
    runs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
    pruned = []
    for old in runs[:-keep] if keep else runs:
        remove_path(old)
        pruned.append(old.name)
    return pruned


def _prune_run_records(state_dir: Optional[Path], keep: int = KEEP_RUN_RECORDS) -> int:
    root = fanout_dir(state_dir) / "runs"
    if not root.is_dir():
        return 0
    records = sorted((p for p in root.iterdir() if p.suffix == ".json"), key=lambda p: p.name)
    old = records[:-keep] if keep else records
    for path in old:
        path.unlink()
    return len(old)


def run_fanout(
    canonical_root: Path,
    *,
    tool_ids: Optional[Sequence[str]] = None,
    tools: Optional[Sequence[FanoutTool]] = None,
    yes: bool = False,
    protect: bool = True,
    state_dir: Optional[Path] = None,
) -> Dict[str, object]:
    """yes=False 等同 build_fanout_plan；yes=True 逐工具加锁执行并写运行记录。"""
    canonical = canonical_root.expanduser()
    tools = list(tools if tools is not None else configured_tools())
    if not yes:
        return build_fanout_plan(canonical, tools=tools, tool_ids=tool_ids)
    if tool_ids:
        unknown = set(tool_ids) - {t.tool_id for t in tools}
        if unknown:
            raise ConfigError(f"unknown or disabled fanout tool: {', '.join(sorted(unknown))}")
        tools = [t for t in tools if t.tool_id in set(tool_ids)]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    record: Dict[str, object] = {
        "record_type": "skill-sync-fanout-run",
        "run_id": run_id,
        "canonical_root": str(canonical),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "tools": [],
    }
    record_path = fanout_dir(state_dir) / "runs" / f"{run_id}.json"
    linked_any = False
    try:
        for tool in tools:
            with tool_lock(tool.tool_id, state_dir):
                ledger = load_ledger(tool.tool_id, fanout_dir(state_dir) / f"{tool.tool_id}.json") if tool.mode == "copy" else {}
                plan = build_fanout_plan(canonical, tools=[tool], ledgers={tool.tool_id: dict(ledger)})["tools"][0]
                report: Dict[str, object] = {"tool_id": tool.tool_id, "mode": tool.mode, "root": plan["root"]}
                if plan.get("skipped"):
                    report["skipped"] = plan["skipped"]
                    record["tools"].append(report)
                    continue
                report["ledger_before"] = dict(ledger)
                report["executed"], report["failed"] = [], []
                backup_root = fanout_dir(state_dir) / "backups" / run_id / tool.tool_id
                for item in plan["items"]:
                    if item["action"] in NOOP_ACTIONS:
                        continue
                    try:
                        report["executed"].append(_execute_item(tool, item, ledger, run_id, backup_root))
                    except (OSError, FanoutError) as exc:
                        report["failed"].append({"name": item["name"], "action": item["action"], "error": str(exc)})
                if tool.mode == "copy":
                    _save_ledger(tool.tool_id, ledger, state_dir)
                if tool.mode == "link" and any(i.get("action") in {"ok", "link", "relink", "replace_dir_with_link"} for i in plan["items"]):
                    linked_any = True
                report["summary"] = plan["summary"]
                report["conflicts"] = sorted(str(i["name"]) for i in plan["items"] if i["action"] == "conflict")
                record["tools"].append(report)
    finally:
        if linked_any and protect:
            # 工具软链指向正本后，正本就得只读：就地改技能会当场失败，而不是被 daemon 推到中央
            record["protected_entries"] = protect_skill_dirs(canonical)
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        record["pruned_backup_runs"] = _prune_backups(state_dir)
        record["record_path"] = str(record_path)
        _write_json(record_path, record)
        _prune_run_records(state_dir)
    return record


def rollback_fanout(record_path: Path, *, yes: bool = False, state_dir: Optional[Path] = None) -> Dict[str, object]:
    """按运行记录逆序撤销。只撤销「现在仍是那次运行留下的样子」的条目，其余跳过。"""
    record = json.loads(record_path.read_text(encoding="utf-8"))
    canonical = Path(record["canonical_root"])
    results = []
    for tool_report in reversed(record.get("tools", [])):
        if tool_report.get("skipped"):
            continue
        root = Path(tool_report["root"])
        tool_id = tool_report["tool_id"]
        with tool_lock(tool_id, state_dir):
            for done in reversed(tool_report.get("executed", [])):
                entry = root / str(done["name"])
                action = done["action"]
                outcome = {"tool_id": tool_id, "name": done["name"], "action": action}
                if action in {"link", "relink", "replace_dir_with_link"}:
                    still_ours = entry.is_symlink() and _within(Path(os.path.realpath(entry)), canonical.resolve())
                elif action in {"copy", "update_copy", "replace_link_with_copy"}:
                    # 复制品被人改过就不删：那已经是别人的东西了
                    still_ours = entry.is_dir() and not entry.is_symlink() and _hash_of(entry) == done.get("content_hash")
                else:  # adopt / remove_*：撤销只是把东西放回去，不会覆盖现有内容
                    still_ours = not (entry.exists() or entry.is_symlink()) or action == "adopt"
                if not still_ours:
                    outcome["result"] = "skipped: entry changed since the run"
                    results.append(outcome)
                    continue
                if yes:
                    if action in {"link", "relink", "replace_dir_with_link"}:
                        entry.unlink()
                    if action in {"copy", "update_copy", "replace_link_with_copy"} and entry.exists():
                        remove_path(entry)
                    if done.get("backup_path") and Path(done["backup_path"]).exists():
                        move_path(Path(done["backup_path"]), entry)
                    if done.get("old_link_target") and not entry.exists() and not entry.is_symlink():
                        os.symlink(str(done["old_link_target"]), entry)
                outcome["result"] = "restored" if yes else "would restore"
                results.append(outcome)
            if yes and tool_report.get("mode") == "copy" and "ledger_before" in tool_report:
                _save_ledger(tool_id, dict(tool_report["ledger_before"]), state_dir)
    return {"record_path": str(record_path), "run_id": record.get("run_id"), "dry_run": not yes, "items": results}


def fanout_protect_canonical(config_path: Optional[Path] = None) -> bool:
    """config.json 的 fanout.protect_canonical（默认 true）。"""
    value = _read_config(config_path or default_config_path()).get("fanout", {}).get("protect_canonical", True)
    if not isinstance(value, bool):
        raise ConfigError("sidecar config fanout.protect_canonical must be true or false")
    return value


# ---- daemon 钩子（PR C）--------------------------------------------------------
#
# daemon 每轮同步后调 FanoutScheduler.maybe_run()。为免每 5 分钟把所有工具目录
# 哈希一遍，只在以下情况才真的扇出：正本指纹变了（拉到新版、本地安装）、距上次
# 超过 interval（修复有人手动动过工具目录）、或上次有失败项。扇出出错只记进本轮
# 结果，不影响同步本身。


def canonical_fingerprint(canonical_root: Path) -> str:
    """正本里所有技能（含嵌套子技能）的路径与哈希；任何一个变了指纹就变。"""
    canonical = canonical_root.expanduser().resolve()
    pairs = sorted(
        (str(skill.path.resolve().relative_to(canonical)), skill.content_hash)
        for skill in scan_roots([f"canonical={canonical}"]).skills
    )
    return hashlib.sha256(json.dumps(pairs).encode("utf-8")).hexdigest()


class FanoutScheduler:
    def __init__(
        self,
        canonical_root: Path,
        *,
        yes: bool,
        interval_seconds: float = 3600.0,
        protect: bool = True,
        state_dir: Optional[Path] = None,
        runner=None,
        planner=None,
        clock=time.monotonic,
    ) -> None:
        self.canonical = canonical_root.expanduser()
        self.yes = yes
        self.interval_seconds = interval_seconds
        self.protect = protect
        self.state_dir = state_dir
        self.runner = runner or run_fanout
        self.planner = planner or build_fanout_plan
        self.clock = clock
        self.last_fingerprint: Optional[str] = None
        self.last_at: Optional[float] = None
        self.last_failed = False

    def _due(self, fingerprint: str) -> Optional[str]:
        if self.last_at is None:
            return "first run"
        if self.last_failed:
            return "previous run had failures"
        if fingerprint != self.last_fingerprint:
            return "canonical changed"
        if self.clock() - self.last_at >= self.interval_seconds:
            return "periodic check"
        return None

    def maybe_run(self) -> Dict[str, object]:
        try:
            fingerprint = canonical_fingerprint(self.canonical)
            reason = self._due(fingerprint)
            if reason is None:
                return {"status": "skipped", "reason": "canonical unchanged"}
            if not self.yes:
                plan = self.planner(self.canonical)
                changes = sum(
                    count
                    for tool in plan["tools"]
                    for action, count in tool.get("summary", {}).items()
                    if action not in NOOP_ACTIONS
                )
                result = {"status": "planned", "reason": reason, "changes": changes}
                failed = 0
            else:
                record = self.runner(self.canonical, yes=True, protect=self.protect, state_dir=self.state_dir)
                failed = sum(len(t.get("failed", [])) for t in record["tools"])
                result = {
                    "status": "ran",
                    "reason": reason,
                    "run_id": record["run_id"],
                    "executed": sum(len(t.get("executed", [])) for t in record["tools"]),
                    "failed": failed,
                    "conflicts": sum(len(t.get("conflicts", [])) for t in record["tools"]),
                    "record_path": record["record_path"],
                }
        except Exception as exc:  # 扇出失败不能拖垮同步
            self.last_failed = True
            self.last_at = self.clock()
            return {"status": "error", "error": f"{exc.__class__.__name__}: {exc}"}
        self.last_fingerprint = fingerprint
        self.last_at = self.clock()
        self.last_failed = failed > 0
        return result
