"""Cursor 的写入目标必须是用户技能目录，不能是它的内置区。

判据取自 Cursor 本体而不是目录名（部署准则 22：读行为不读名字）。
`Cursor.app/.../cursor-agent-host/dist/main.js` 里声明：

    {configDir:".cursor", subdir:"skills-cursor", thirdParty:false, builtin:true}
    {configDir:".cursor", subdir:"skills",        thirdParty:false, builtin:false}

它的 .gitignore 模板把两者分别注释成「Built-in Cursor skills」和
「User's personal skills」。`~/.cursor/skills-cursor` 下还有一份
`.cursor-managed-skills-manifest.json` 在列 builtin/managed 名单。

Cursor **两个目录都会加载**，所以这从来不是「读不到」，是「放错了地方」：
往内置区写，等于把用户技能塞进 Cursor 托管的命名空间，随时可能被它清理。

2026-09-28 实测发现：六处写入目标全指向 skills-cursor，而 `~/.cursor/skills`
里那份存量停在更早的版本、没有任何 sidecar 备份记录——两边各存一份，谁都不知道
对方。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from skill_sync_sidecar import apply as apply_mod
from skill_sync_sidecar import local_skill, operator_executor
from skill_sync_sidecar.local_skill import _infer_scope
from skill_sync_sidecar.projection import default_tool_adapters
from skill_sync_sidecar.tool_status import DEFAULT_TOOL_ROOTS

BUILTIN = Path.home() / ".cursor" / "skills-cursor"
USER = Path.home() / ".cursor" / "skills"


def write_targets() -> list[tuple[str, Path]]:
    """所有「会往这里写文件」的 cursor 目标。读取/上报用的不算。"""
    out: list[tuple[str, Path]] = [
        ("local_skill.DEFAULT_LOCAL_TOOL_TARGETS", t.root)
        for t in local_skill.DEFAULT_LOCAL_TOOL_TARGETS if t.tool_id == "cursor"
    ]
    out.append(("apply.GLOBAL_TOOL_TARGETS[cursor-global]",
                Path(apply_mod.GLOBAL_TOOL_TARGETS["cursor-global"]["default_root"])))
    _, parts, _ = operator_executor.MAC_TOOL_INSTALL_TARGETS["cursor"]
    out.append(("operator_executor.MAC_TOOL_INSTALL_TARGETS", Path.home().joinpath(*parts)))
    return out


class CursorSkillRootTests(unittest.TestCase):

    def test_写入目标是用户技能目录(self):
        targets = write_targets()
        self.assertTrue(targets, "一个写入目标都没解析到，先查解析规则本身")
        for where, root in targets:
            self.assertEqual(root, USER, f"{where} 指向 {root}")

    def test_写入目标里不出现内置区(self):
        for where, root in write_targets():
            self.assertNotEqual(root, BUILTIN, f"{where} 仍在往 Cursor 内置区写")

    def test_识别范围仍覆盖存量的内置区(self):
        """改目标之前装进 skills-cursor 的那些，仍要被认成 global。"""
        self.assertEqual(_infer_scope(BUILTIN / "demo"), "global")
        self.assertEqual(_infer_scope(USER / "demo"), "global")

    def test_状态与预览两个目录都看(self):
        """Cursor 两个都加载，所以只报一个会漏掉存量。"""
        roots = next(r for r in DEFAULT_TOOL_ROOTS if r[0] == "cursor")[2]
        self.assertEqual(set(roots), {USER, BUILTIN}, roots)
        adapter = next(a for a in default_tool_adapters() if a.tool_id == "cursor")
        self.assertEqual(set(adapter.roots), {USER, BUILTIN}, adapter.roots)


if __name__ == "__main__":
    unittest.main(verbosity=2)
