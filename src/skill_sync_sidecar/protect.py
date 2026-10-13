"""正本只读保护（T-0260 第 4 步）。

各工具以软链指向正本后，工具或 agent 在工具目录里「就地改技能」改的就是正本，
而 daemon 会在 5 分钟内把正本的改动推到中央、再扩散到所有设备。所以 sidecar
写完正本后把技能目录和文件的写权限去掉：就地改会当场失败，而不是静默上传。

sidecar 自己改正本时先解除。注意：在 macOS / Linux 上，把只读目录移到别的父目录
下也会失败（要改写它自己的 `..`），所以移动前同样要解除顶层目录。
"""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Iterable

WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


def _walk(path: Path) -> Iterable[Path]:
    yield path
    if path.is_dir() and not path.is_symlink():
        for root, dirs, files in os.walk(path):
            for name in dirs + files:
                yield Path(root) / name


def make_readonly(path: Path) -> int:
    """去掉 path 及其下所有文件、目录的写权限（不跟随软链）。返回改动的条目数。"""
    changed = 0
    for item in _walk(path):
        if item.is_symlink():
            continue
        mode = item.stat().st_mode
        if mode & WRITE_BITS:
            os.chmod(item, mode & ~WRITE_BITS)
            changed += 1
    return changed


def make_writable(path: Path, recursive: bool = True) -> None:
    """恢复属主写权限；recursive=False 只处理 path 本身（移动目录前够用）。"""
    items = _walk(path) if recursive else [path]
    for item in items:
        if item.is_symlink() or not item.exists():
            continue
        mode = item.stat().st_mode
        if not mode & stat.S_IWUSR:
            os.chmod(item, mode | stat.S_IWUSR)


def is_readonly(path: Path) -> bool:
    return not any(not item.is_symlink() and item.stat().st_mode & WRITE_BITS for item in _walk(path))


def remove_path(path: Path) -> None:
    """删除文件、软链或目录树；目录树里有只读条目也能删。"""
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        make_writable(path)
        shutil.rmtree(path)
    else:
        path.unlink()


def move_path(source: Path, target: Path) -> None:
    """移动目录或文件；只读的顶层目录先解除，内容保持原权限。"""
    if source.is_dir() and not source.is_symlink():
        make_writable(source, recursive=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))


def protect_skill_dirs(root: Path) -> int:
    """把 root 下每个顶层技能目录设为只读（不动 root 本身和 .skill-sync-* 等点目录）。"""
    changed = 0
    if not root.is_dir():
        return 0
    for entry in sorted(root.iterdir()):
        if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
            continue
        if not (entry / "SKILL.md").exists():
            continue
        changed += make_readonly(entry)
    return changed
