from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .config import ConfigError, configured_local_root, write_config_local_root
from .scanner import scan_roots
from .sync_state import build_sync_status

# 迁移只搬技能内容和 sidecar 读的覆盖文件；备份与历史基线留在旧根，daemon 用的
# 基线是 Application Support 里的 base-record.json，不是这两个目录。
SKIPPED_TOP_LEVEL = {".skill-sync-backups", ".skill-sync-bases"}
PROJECT_MARKERS = ("AGENTS.md", ".git")


class LocalRootMigrationError(RuntimeError):
    pass


def _hashes(root: Path) -> Dict[str, str]:
    summary = scan_roots([f"local={root}"])
    hashes: Dict[str, str] = {}
    duplicates: List[str] = []
    for skill in summary.skills:
        if skill.skill_id in hashes:
            duplicates.append(skill.skill_id)
        hashes[skill.skill_id] = skill.content_hash
    if duplicates:
        raise LocalRootMigrationError(f"duplicate skill ids under {root}: {', '.join(sorted(set(duplicates)))}")
    return hashes


def _sync_outcome(root: Path, remote_snapshot: Path, base_record: Optional[Path]) -> Dict[str, str]:
    status = build_sync_status(root, remote_snapshot, base_record)
    return {item["skill_id"]: item["action"] for item in status["items"]}


def _preflight(source: Path, dest: Path, config_path: Optional[Path]) -> List[str]:
    problems = []
    if not source.is_dir():
        problems.append(f"source root does not exist: {source}")
    current = configured_local_root(config_path)
    if current.expanduser().resolve() != source.resolve():
        problems.append(f"configured local_root is {current}, not {source}; already migrated?")
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        problems.append(f"destination is not empty: {dest}")
    src_r, dst_r = source.resolve(), dest.resolve()
    if src_r == dst_r or src_r in dst_r.parents or dst_r in src_r.parents:
        problems.append("source and destination must not contain each other")
    # scanner 把「上一级叫 skills、再上一级有 AGENTS.md/.git」的技能判成 project scope，
    # scope 会进中央 index，别的设备可能因此卡住
    if dest.name == "skills":
        hits = [m for m in PROJECT_MARKERS if (dest.parent / m).exists()]
        if hits:
            problems.append(f"{dest.parent} contains {', '.join(hits)}; skills under {dest} would be scanned as project scope")
    return problems


def migrate_local_root(
    source: Path,
    dest: Path,
    remote_snapshot: Path,
    base_record: Optional[Path],
    *,
    config_path: Optional[Path] = None,
    yes: bool = False,
) -> dict:
    """把本机正本从 source 复制到 dest，校验后才把 config 的 local_root 指向 dest。

    校验两条，都过才写配置，否则删掉复制出来的目录：
    - 逐技能 content_hash 与旧根完全一致（换根不改变任何技能的内容）；
    - 对同一份中央快照和基线，新根算出的同步动作与旧根完全一致（换根不改变 daemon
      下一轮要做的事；旧根此刻有待推送的改动，新根照样有，不会被吞掉也不会多出来）。
    不碰 launchd：显式传了 --local-root 的服务要另行切换。
    """
    source, dest = source.expanduser(), dest.expanduser()
    problems = _preflight(source, dest, config_path)
    if problems:
        raise LocalRootMigrationError("; ".join(problems))

    source_hashes = _hashes(source)
    source_outcome = _sync_outcome(source, remote_snapshot, base_record)
    result = {
        "source": str(source),
        "dest": str(dest),
        "skills": len(source_hashes),
        "sync_actions": _count(source_outcome),
        "skipped": sorted(name for name in SKIPPED_TOP_LEVEL if (source / name).exists()),
        "dry_run": not yes,
        "config_updated": False,
    }
    if not yes:
        return result

    renamed = False
    staging = dest.with_name(f"{dest.name}.migrating-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    try:
        shutil.copytree(
            source,
            staging,
            symlinks=True,
            ignore=lambda directory, names: [n for n in names if Path(directory) == source and n in SKIPPED_TOP_LEVEL],
        )
        if dest.exists():
            dest.rmdir()  # preflight 已确认是空目录
        staging.rename(dest)
        renamed = True
        dest_hashes = _hashes(dest)
        if dest_hashes != source_hashes:
            changed = sorted(k for k in set(source_hashes) | set(dest_hashes) if source_hashes.get(k) != dest_hashes.get(k))
            raise LocalRootMigrationError(f"content hashes differ after copy: {', '.join(changed[:10])}")
        dest_outcome = _sync_outcome(dest, remote_snapshot, base_record)
        if dest_outcome != source_outcome:
            changed = sorted(k for k in set(source_outcome) | set(dest_outcome) if source_outcome.get(k) != dest_outcome.get(k))
            raise LocalRootMigrationError(f"sync actions differ after copy: {', '.join(changed[:10])}")
    except (OSError, LocalRootMigrationError) as exc:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if renamed and dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        if isinstance(exc, LocalRootMigrationError):
            raise
        raise LocalRootMigrationError(f"copy failed: {exc}") from exc

    try:
        result["config_path"] = str(write_config_local_root(dest, config_path))
    except ConfigError:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    result["config_updated"] = True
    return result


def _count(outcome: Dict[str, str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for action in outcome.values():
        counts[action] = counts.get(action, 0) + 1
    return dict(sorted(counts.items()))
