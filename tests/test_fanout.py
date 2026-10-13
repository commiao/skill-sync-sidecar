import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from skill_sync_sidecar import cli
from skill_sync_sidecar import fanout
from skill_sync_sidecar.config import ConfigError
from skill_sync_sidecar.fanout import FanoutTool, build_fanout_plan, configured_tools


def _skill(root: Path, name: str, body: str = "body", manifest: dict = None) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} skill\n---\n\n{body}\n", encoding="utf-8")
    if manifest is not None:
        (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return d


def _items(plan: dict, tool_id: str) -> dict:
    tool = next(t for t in plan["tools"] if t["tool_id"] == tool_id)
    return {i["name"]: i for i in tool.get("items", [])}


class FanoutPlanTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.canonical = self.tmp / "canonical"
        self.legacy = self.tmp / "skillshub"
        self.foreign = self.tmp / "agents"
        self.tool_root = self.tmp / "tool"
        self.tool_root.mkdir()
        self.tool = FanoutTool("codex", self.tool_root, "link", frozenset({"codex"}))
        self.adoptable = mock.patch.object(fanout, "ADOPTABLE_ROOTS", (self.legacy,))
        self.adoptable.start()

    def tearDown(self):
        self.adoptable.stop()
        self._tmp.cleanup()

    def plan(self, tools=None, ledgers=None):
        return build_fanout_plan(self.canonical, tools=tools or [self.tool], ledgers=ledgers)

    def test_declared_missing_is_linked_and_undeclared_missing_is_left_alone(self):
        _skill(self.canonical, "declared", manifest={"skill_id": "declared", "scope": "global", "targets": ["codex"]})
        _skill(self.canonical, "undeclared")
        _skill(self.canonical, "other-tool", manifest={"skill_id": "other-tool", "scope": "global", "targets": ["cursor"]})
        plan = self.plan()
        items = _items(plan, "codex")
        self.assertEqual(items["declared"]["action"], "link")
        self.assertNotIn("undeclared", items)
        self.assertNotIn("other-tool", items)
        not_placed = plan["tools"][0]["not_placed"]
        self.assertEqual(not_placed, {"not_targeted": 1, "undeclared_not_present": 1})

    def test_existing_link_to_canonical_is_ok(self):
        src = _skill(self.canonical, "a")
        (self.tool_root / "a").symlink_to(src)
        self.assertEqual(_items(self.plan(), "codex")["a"]["action"], "ok")

    def test_link_into_adoptable_root_is_relinked_only_when_content_matches(self):
        _skill(self.canonical, "same")
        _skill(self.canonical, "diff")
        (self.tool_root / "same").symlink_to(_skill(self.legacy, "same"))
        (self.tool_root / "diff").symlink_to(_skill(self.legacy, "diff", body="older"))
        items = _items(self.plan(), "codex")
        self.assertEqual(items["same"]["action"], "relink")
        self.assertEqual(items["diff"]["action"], "conflict")

    def test_link_owned_by_another_manager_is_not_touched(self):
        _skill(self.canonical, "a")
        (self.tool_root / "a").symlink_to(_skill(self.foreign, "a"))
        self.assertEqual(_items(self.plan(), "codex")["a"]["action"], "foreign_link")

    def test_real_directory_becomes_link_only_when_content_matches(self):
        _skill(self.canonical, "same")
        _skill(self.canonical, "diff")
        _skill(self.tool_root, "same")
        _skill(self.tool_root, "diff", body="user edited")
        items = _items(self.plan(), "codex")
        self.assertEqual(items["same"]["action"], "replace_dir_with_link")
        self.assertEqual(items["diff"]["action"], "conflict")

    def test_dangling_link_into_canonical_is_removed(self):
        _skill(self.canonical, "keep")
        (self.tool_root / "gone").symlink_to(self.canonical / "gone")
        items = _items(self.plan(), "codex")
        self.assertEqual(items["gone"]["action"], "remove_dangling_link")

    def test_project_scope_and_nested_skills_are_not_placed(self):
        _skill(self.canonical, "proj", manifest={"skill_id": "proj", "scope": "project", "targets": ["codex"]})
        bundle = _skill(self.canonical, "bundle", manifest={"skill_id": "bundle", "scope": "global", "targets": ["codex"]})
        _skill(bundle / "skills", "child")
        plan = self.plan()
        self.assertEqual(plan["tools"][0]["not_placed"].get("project_scope"), 1)
        self.assertNotIn("child", _items(plan, "codex"))

    def test_tool_root_equal_to_canonical_is_skipped(self):
        _skill(self.canonical, "a")
        same = FanoutTool("cc-switch", self.canonical, "link", frozenset({"cc-switch"}))
        plan = self.plan(tools=[same])
        self.assertIn("canonical", plan["tools"][0]["skipped"])

    def test_copy_mode_uses_the_ledger(self):
        manifest = lambda n: {"skill_id": n, "scope": "global", "targets": ["cursor"]}
        for name in ("new", "same", "stale", "edited"):
            _skill(self.canonical, name, body="v2", manifest=manifest(name))
        _skill(self.tool_root, "same", body="v2", manifest=manifest("same"))
        stale = _skill(self.tool_root, "stale", body="v1", manifest=manifest("stale"))
        edited = _skill(self.tool_root, "edited", body="user", manifest=manifest("edited"))
        removed = _skill(self.tool_root, "removed", body="v1")
        ledger = {
            "stale": fanout._hash_of(stale),
            "edited": "recorded-hash-that-no-longer-matches",
            "removed": fanout._hash_of(removed),
        }
        cursor = FanoutTool("cursor", self.tool_root, "copy", frozenset({"cursor"}))
        items = _items(self.plan(tools=[cursor], ledgers={"cursor": ledger}), "cursor")
        self.assertEqual(items["new"]["action"], "copy")
        self.assertEqual(items["same"]["action"], "adopt")
        self.assertEqual(items["stale"]["action"], "update_copy")
        self.assertEqual(items["edited"]["action"], "conflict")
        self.assertEqual(items["removed"]["action"], "remove_copy")

    def test_plan_writes_nothing(self):
        _skill(self.canonical, "a", manifest={"skill_id": "a", "scope": "global", "targets": ["codex"]})
        before = sorted(p.name for p in self.tool_root.iterdir())
        self.plan()
        self.assertEqual(sorted(p.name for p in self.tool_root.iterdir()), before)


class FanoutConfigTest(unittest.TestCase):
    def test_mode_override_and_disable(self):
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"fanout": {"tools": {"cursor": {"mode": "link"}, "qoder": {"enabled": False}}}}), encoding="utf-8")
            tools = {t.tool_id: t for t in configured_tools(config)}
            self.assertEqual(tools["cursor"].mode, "link")
            self.assertNotIn("qoder", tools)
            self.assertEqual(tools["codex"].mode, "link")

    def test_defaults_put_cursor_on_copy(self):
        with TemporaryDirectory() as tmp:
            tools = {t.tool_id: t for t in configured_tools(Path(tmp) / "missing.json")}
            self.assertEqual(tools["cursor"].mode, "copy")
            self.assertEqual(tools["cursor"].root, Path.home() / ".cursor" / "skills")

    def test_bad_mode_is_an_error(self):
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"fanout": {"tools": {"codex": {"mode": "hardlink"}}}}), encoding="utf-8")
            with self.assertRaises(ConfigError):
                configured_tools(config)


class FanoutCliTest(unittest.TestCase):
    def test_dry_run_is_required(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["fanout"])

    def test_local_root_comes_from_config(self):
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"local_root": str(Path(tmp) / "canon")}), encoding="utf-8")
            seen = {}

            def fake_plan(root, tool_ids=None):
                seen["root"] = root
                return {"canonical_root": str(root), "skills": 0, "dry_run": True, "tools": []}

            with mock.patch.dict(os.environ, {"SKILL_SYNC_CONFIG": str(config)}), mock.patch.object(cli, "build_fanout_plan", fake_plan):
                self.assertEqual(cli.main(["fanout", "--dry-run"]), 0)
            self.assertEqual(seen["root"], Path(tmp) / "canon")


if __name__ == "__main__":
    unittest.main()
