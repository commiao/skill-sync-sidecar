import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from skill_sync_sidecar import cli
from skill_sync_sidecar import fanout
from skill_sync_sidecar.apply import _remove_path as apply_remove_path
from skill_sync_sidecar.fanout import FanoutError, FanoutTool, rollback_fanout, run_fanout, tool_lock
from skill_sync_sidecar.protect import is_readonly, make_writable, move_path


def _skill(root: Path, name: str, body: str = "body", manifest: dict = None) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} skill\n---\n\n{body}\n", encoding="utf-8")
    if manifest is not None:
        (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return d


def _declared(name: str, *tools: str) -> dict:
    return {"skill_id": name, "scope": "global", "targets": list(tools)}


class FanoutExecuteBase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.canonical = self.tmp / "canonical"
        self.legacy = self.tmp / "skillshub"
        self.state = self.tmp / "state"
        self.tool_root = self.tmp / "tool"
        self.tool_root.mkdir()
        self.patch = mock.patch.object(fanout, "ADOPTABLE_ROOTS", (self.legacy,))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        make_writable(self.tmp)  # 正本被设成只读了，先解除才能清理
        self._tmp.cleanup()

    def run_fanout(self, tool, **kwargs):
        return run_fanout(self.canonical, tools=[tool], yes=True, state_dir=self.state, **kwargs)


class LinkModeTest(FanoutExecuteBase):
    def setUp(self):
        super().setUp()
        self.tool = FanoutTool("codex", self.tool_root, "link", frozenset({"codex"}))
        _skill(self.canonical, "new", manifest=_declared("new", "codex"))
        _skill(self.canonical, "adopt-dir")
        _skill(self.canonical, "adopt-link")
        _skill(self.canonical, "diverged")
        _skill(self.tool_root, "adopt-dir")
        (self.tool_root / "adopt-link").symlink_to(_skill(self.legacy, "adopt-link"))
        _skill(self.tool_root, "diverged", body="user edit")
        (self.tool_root / "gone").symlink_to(self.canonical / "gone")

    def test_execute_places_links_and_leaves_conflicts(self):
        record = self.run_fanout(self.tool)

        for name in ("new", "adopt-dir", "adopt-link"):
            entry = self.tool_root / name
            self.assertTrue(entry.is_symlink(), name)
            self.assertEqual(Path(os.path.realpath(entry)), (self.canonical / name).resolve())
        self.assertFalse((self.tool_root / "diverged").is_symlink())
        self.assertIn("user edit", (self.tool_root / "diverged" / "SKILL.md").read_text())
        self.assertFalse((self.tool_root / "gone").is_symlink())
        report = record["tools"][0]
        self.assertEqual(report["failed"], [])
        self.assertEqual(report["conflicts"], ["diverged"])
        self.assertTrue(Path(record["record_path"]).exists())
        backup = next(i for i in report["executed"] if i["name"] == "adopt-dir")["backup_path"]
        self.assertTrue((Path(backup) / "SKILL.md").exists())
        self.assertNotIn(".skill-sync-backups", [p.name for p in self.tool_root.iterdir()])

    def test_canonical_becomes_read_only_and_edits_through_links_fail(self):
        self.run_fanout(self.tool)
        self.assertTrue(is_readonly(self.canonical / "new"))
        with self.assertRaises(PermissionError):
            (self.tool_root / "new" / "SKILL.md").write_text("edited in the tool", encoding="utf-8")
        with self.assertRaises(PermissionError):
            (self.tool_root / "new" / "extra.md").write_text("new file", encoding="utf-8")

    def test_sidecar_can_still_replace_and_move_protected_skills(self):
        self.run_fanout(self.tool)
        apply_remove_path(self.canonical / "adopt-dir")  # daemon 拉新版时的删除路径
        self.assertFalse((self.canonical / "adopt-dir").exists())
        move_path(self.canonical / "adopt-link", self.tmp / "elsewhere" / "adopt-link")
        self.assertTrue((self.tmp / "elsewhere" / "adopt-link" / "SKILL.md").exists())

    def test_protection_can_be_disabled(self):
        self.run_fanout(self.tool, protect=False)
        self.assertFalse(is_readonly(self.canonical / "new"))

    def test_rollback_restores_previous_state(self):
        record = self.run_fanout(self.tool)
        result = rollback_fanout(Path(record["record_path"]), yes=True, state_dir=self.state)

        self.assertFalse((self.tool_root / "new").exists() or (self.tool_root / "new").is_symlink())
        self.assertFalse((self.tool_root / "adopt-dir").is_symlink())
        self.assertTrue((self.tool_root / "adopt-dir" / "SKILL.md").exists())
        self.assertEqual(Path(os.path.realpath(self.tool_root / "adopt-link")), (self.legacy / "adopt-link").resolve())
        self.assertTrue((self.tool_root / "gone").is_symlink())
        self.assertTrue(all(i["result"] == "restored" for i in result["items"]))

    def test_rollback_skips_entries_changed_after_the_run(self):
        record = self.run_fanout(self.tool)
        (self.tool_root / "new").unlink()
        _skill(self.tool_root, "new", body="someone else put this here")
        result = rollback_fanout(Path(record["record_path"]), yes=True, state_dir=self.state)
        skipped = [i for i in result["items"] if i["name"] == "new"]
        self.assertTrue(skipped[0]["result"].startswith("skipped"))
        self.assertIn("someone else", (self.tool_root / "new" / "SKILL.md").read_text())


class CopyModeTest(FanoutExecuteBase):
    def test_copy_adopt_update_and_remove_with_ledger(self):
        tool = FanoutTool("cursor", self.tool_root, "copy", frozenset({"cursor"}))
        for name in ("new", "same"):
            _skill(self.canonical, name, body="v1", manifest=_declared(name, "cursor"))
        _skill(self.tool_root, "same", body="v1", manifest=_declared("same", "cursor"))

        first = run_fanout(self.canonical, tools=[tool], yes=True, state_dir=self.state, protect=False)
        self.assertEqual(first["tools"][0]["failed"], [])
        self.assertTrue((self.tool_root / "new" / "SKILL.md").exists())
        self.assertFalse((self.tool_root / "new").is_symlink())
        ledger = json.loads((self.state / "fanout" / "cursor.json").read_text())["entries"]
        self.assertEqual(sorted(ledger), ["new", "same"])

        # 正本更新 → 台账内且未被改过的复制品被更新；正本删掉 → 复制品移走
        _skill(self.canonical, "new", body="v2", manifest=_declared("new", "cursor"))
        make_writable(self.canonical / "same")
        for p in (self.canonical / "same").iterdir():
            p.unlink()
        (self.canonical / "same").rmdir()
        second = run_fanout(self.canonical, tools=[tool], yes=True, state_dir=self.state, protect=False)
        actions = {i["name"]: i["action"] for i in second["tools"][0]["executed"]}
        self.assertEqual(actions, {"new": "update_copy", "same": "remove_copy"})
        self.assertIn("v2", (self.tool_root / "new" / "SKILL.md").read_text())
        self.assertFalse((self.tool_root / "same").exists())


class CopyFromReadOnlyCanonicalTest(FanoutExecuteBase):
    def test_copy_actions_work_when_the_canonical_root_is_read_only(self):
        # 切换后正本只读；复制模式的临时目录若带着只读权限，macOS 上改名会被拒
        from skill_sync_sidecar.protect import make_readonly

        tool = FanoutTool("cursor", self.tool_root, "copy", frozenset({"cursor"}))
        for name in ("new", "linked", "stale"):
            _skill(self.canonical, name, body="v2", manifest=_declared(name, "cursor"))
        (self.tool_root / "linked").symlink_to(_skill(self.legacy, "linked", body="v2", manifest=_declared("linked", "cursor")))
        stale = _skill(self.tool_root, "stale", body="v1", manifest=_declared("stale", "cursor"))
        ledger_path = self.state / "fanout" / "cursor.json"
        ledger_path.parent.mkdir(parents=True)
        ledger_path.write_text(json.dumps({"entries": {"stale": {"content_hash": fanout._hash_of(stale)}}}), encoding="utf-8")
        make_readonly(self.canonical)

        record = run_fanout(self.canonical, tools=[tool], yes=True, state_dir=self.state)

        report = record["tools"][0]
        self.assertEqual(report["failed"], [])
        self.assertEqual({e["name"]: e["action"] for e in report["executed"]}, {"new": "copy", "linked": "replace_link_with_copy", "stale": "update_copy"})
        for name in ("new", "linked", "stale"):
            entry = self.tool_root / name
            self.assertFalse(entry.is_symlink())
            self.assertIn("v2", (entry / "SKILL.md").read_text())
            (entry / "SKILL.md").write_text("tool copies stay writable", encoding="utf-8")
        self.assertEqual([p.name for p in self.tool_root.iterdir() if "skill-sync-tmp" in p.name], [])


class LockAndRetentionTest(FanoutExecuteBase):
    def test_second_writer_times_out(self):
        with tool_lock("codex", self.state):
            with self.assertRaises(FanoutError):
                with tool_lock("codex", self.state, timeout=0.3):
                    pass

    def test_only_recent_run_records_are_kept(self):
        runs = self.state / "fanout" / "runs"
        runs.mkdir(parents=True)
        for i in range(205):
            (runs / f"20260101T{i:06d}Z.json").write_text("{}", encoding="utf-8")
        removed = fanout._prune_run_records(self.state, keep=200)
        self.assertEqual(removed, 5)
        names = sorted(p.name for p in runs.iterdir())
        self.assertEqual(len(names), 200)
        self.assertEqual(names[0], "20260101T000005Z.json")

    def test_only_recent_backup_runs_are_kept(self):
        backups = self.state / "fanout" / "backups"
        for i in range(8):
            (backups / f"2026010{i}T000000Z").mkdir(parents=True)
        pruned = fanout._prune_backups(self.state, keep=5)
        self.assertEqual(len(pruned), 3)
        self.assertEqual(len(list(backups.iterdir())), 5)


class FanoutCliModesTest(unittest.TestCase):
    def test_yes_and_dry_run_are_exclusive(self):
        parser = cli.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["fanout", "--dry-run", "--yes"])
        self.assertTrue(parser.parse_args(["fanout", "--yes"]).yes)
        self.assertTrue(parser.parse_args(["fanout-rollback", "--record", "r.json", "--dry-run"]).dry_run)


if __name__ == "__main__":
    unittest.main()
