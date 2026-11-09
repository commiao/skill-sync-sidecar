"""Exercise the desktop tools through install, inventory and managed fanout."""

import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from skill_sync_sidecar import cli, operator_executor
from skill_sync_sidecar.apply import GLOBAL_TOOL_TARGETS, build_apply_plan, execute_apply_plan
from skill_sync_sidecar.fanout import DEFAULT_TOOLS, run_fanout
from skill_sync_sidecar.local_skill import DEFAULT_LOCAL_TOOL_TARGETS, install_local_skill
from skill_sync_sidecar.projection import build_tool_projection, default_tool_adapters
from skill_sync_sidecar.scanner import scan_roots
from skill_sync_sidecar.snapshot import write_snapshot
from skill_sync_sidecar.stage import stage_snapshot
from skill_sync_sidecar.sync_apply import build_sync_apply_preview
from skill_sync_sidecar.tool_status import DEFAULT_TOOL_ROOTS, build_device_tool_status


DESKTOP_TOOLS = {
    "doubao": ("豆包", ("Doubao", "skills")),
    "workbuddy": ("WorkBuddy", (".workbuddy", "skills")),
}


def write_skill(root, name, *, targets=None, scope="global", body="version one"):
    skill = root / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Desktop example\n---\n\n{body}\n",
        encoding="utf-8",
    )
    if targets is not None:
        (skill / "manifest.json").write_text(json.dumps({
            "skill_id": name, "scope": scope, "targets": targets,
        }), encoding="utf-8")
    return skill


class DesktopToolTests(unittest.TestCase):
    def test_default_roots_match_installed_clients(self):
        for tool_id, (name, parts) in DESKTOP_TOOLS.items():
            with self.subTest(tool=tool_id):
                root = Path.home().joinpath(*parts)
                local = next(t for t in DEFAULT_LOCAL_TOOL_TARGETS if t.tool_id == tool_id)
                adapter = next(t for t in default_tool_adapters() if t.tool_id == tool_id)
                status = next(t for t in DEFAULT_TOOL_ROOTS if t[0] == tool_id)
                fanout = next(t for t in DEFAULT_TOOLS if t.tool_id == tool_id)
                self.assertEqual(local.root, root)
                self.assertEqual(local.name, name)
                self.assertEqual(adapter.roots, [root])
                self.assertEqual(status[2], (root,))
                self.assertEqual(fanout.root, root)
                self.assertEqual(fanout.mode, "copy")
                self.assertEqual(GLOBAL_TOOL_TARGETS[f"{tool_id}-global"]["default_root"], root)
                self.assertIn(f"{tool_id}-global", cli.TOOL_GLOBAL_TARGETS)

    def test_local_install_is_detected_by_inventory_and_projection(self):
        for tool_id, (name, _parts) in DESKTOP_TOOLS.items():
            with self.subTest(tool=tool_id), TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = write_skill(root / "source", "demo")
                target = replace(next(t for t in DEFAULT_LOCAL_TOOL_TARGETS if t.tool_id == tool_id), root=root / "tool")
                target.root.mkdir()
                result = install_local_skill(source, tool_ids=[tool_id], tool_targets=[target], yes=True, allow_local_writes=True)
                self.assertTrue(result["ok"])
                self.assertEqual(result["summary"]["will_write"], 1)
                status = build_device_tool_status([(tool_id, name, [target.root], "user skills")])[0]
                self.assertEqual(status["state"], "detected")
                self.assertEqual(status["skills"], 1)
                write_snapshot(scan_roots([f"local={target.root}"]), root / "snapshot", "desktop")
                adapter = replace(next(t for t in default_tool_adapters() if t.tool_id == tool_id), roots=[target.root])
                projection = build_tool_projection(root / "snapshot", [adapter])["tools"][0]
                self.assertEqual(projection["summary"], {"installed": 1})

    def test_central_apply_enforces_tool_targets_and_global_scope(self):
        for tool_id in DESKTOP_TOOLS:
            with self.subTest(tool=tool_id), TemporaryDirectory() as tmp:
                root = Path(tmp)
                write_skill(root / "canonical", "wanted", targets=[tool_id])
                write_skill(root / "canonical", "other", targets=["codex"])
                write_skill(root / "canonical", "project", targets=[tool_id], scope="project")
                write_snapshot(scan_roots([f"canonical={root / 'canonical'}"]), root / "snapshot", "desktop")
                (root / "tool").mkdir()
                preview = build_sync_apply_preview(root / "tool", root / "snapshot", allow_new=True, target=f"{tool_id}-global")
                self.assertEqual(preview["expected_scope"], "global")
                self.assertEqual({i["skill_id"] for i in preview["items"] if i["sync_apply_action"] == "install_or_replace"}, {"wanted"})
                stage_snapshot(root / "snapshot", root / "stage")
                # The public CLI accepts the new global apply target.
                cli.build_parser().parse_args(["apply", "--staged", str(root / "stage" / "desktop"), "--target", f"{tool_id}-global", "--dry-run"])
                plan = build_apply_plan(root / "stage" / "desktop", f"{tool_id}-global", target_root=root / "tool")
                self.assertEqual({i["skill_id"] for i in plan["items"] if i["allowed"]}, {"wanted"})
                execute_apply_plan(plan)
                self.assertTrue((root / "tool" / "wanted" / "SKILL.md").is_file())
                self.assertFalse((root / "tool" / "other").exists())
                self.assertFalse((root / "tool" / "project").exists())

    def test_fanout_updates_managed_copy_and_preserves_user_edits(self):
        for tool_id in DESKTOP_TOOLS:
            with self.subTest(tool=tool_id), TemporaryDirectory() as tmp:
                root = Path(tmp)
                canonical = root / "canonical"
                target = root / "tool"
                target.mkdir()
                tool = replace(next(t for t in DEFAULT_TOOLS if t.tool_id == tool_id), root=target)
                write_skill(canonical, "demo", targets=[tool_id])
                write_skill(canonical, "undeclared")
                def run():
                    report = run_fanout(canonical, tools=[tool], yes=True, state_dir=root / "state")["tools"][0]
                    self.assertEqual(report["failed"], [])
                    return report
                run()
                installed = target / "demo" / "SKILL.md"
                self.assertTrue(installed.is_file())
                self.assertFalse((target / "demo").is_symlink())
                self.assertFalse((target / "undeclared").exists())
                write_skill(canonical, "demo", targets=[tool_id], body="version two")
                run()
                self.assertIn("version two", installed.read_text())
                installed.write_text("user edits\n")
                write_skill(canonical, "demo", targets=[tool_id], body="version three")
                report = run()
                self.assertEqual(report["conflicts"], ["demo"])
                self.assertEqual(installed.read_text(), "user edits\n")

    def test_dashboard_install_executor_routes_to_each_client_root(self):
        for tool_id, (name, parts) in DESKTOP_TOOLS.items():
            with self.subTest(tool=tool_id), mock.patch.object(operator_executor, "restore_from_central", return_value={"ok": True}) as restore:
                result = operator_executor.run_mac_tool_install_from_central(tool_id, ["demo"])
                self.assertEqual(restore.call_args.args[0], Path.home().joinpath(*parts))
                self.assertEqual(restore.call_args.kwargs["target"], f"{tool_id}-global")
                self.assertEqual(result["tool_name"], name)


if __name__ == "__main__":
    unittest.main()
