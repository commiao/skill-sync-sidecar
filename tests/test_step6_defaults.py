import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from skill_sync_sidecar.apply import build_apply_plan
from skill_sync_sidecar.local_skill import DEFAULT_GLOBAL_TARGETS, default_local_tool_targets
from skill_sync_sidecar.operator_executor import MAC_TOOL_INSTALL_TARGETS
from skill_sync_sidecar.projection import default_tool_adapters
from skill_sync_sidecar.tool_status import DEFAULT_TOOL_ROOTS


class Step6DefaultsTest(unittest.TestCase):
    def test_skillshub_is_no_longer_a_default_destination(self):
        self.assertNotIn("skillshub", DEFAULT_GLOBAL_TARGETS)
        self.assertNotIn("skillshub", MAC_TOOL_INSTALL_TARGETS)
        self.assertNotIn("skillshub", [a.tool_id for a in default_tool_adapters()])
        with TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"SKILL_SYNC_CONFIG": str(Path(tmp) / "c.json")}):
            ids = [t.tool_id for t in default_local_tool_targets()]
        self.assertEqual(ids[0], "sidecar")
        self.assertNotIn("skillshub", ids)
        self.assertNotIn("cc-switch", ids)

    def test_tool_status_keeps_ids_for_peers_but_describes_the_new_roles(self):
        roles = {tool_id: role for tool_id, _name, _roots, role in DEFAULT_TOOL_ROOTS}
        self.assertIn("sidecar", roles)
        self.assertIn("fanout", roles["cc-switch"])
        self.assertIn("退役", roles["skillshub"])

    def test_apply_into_a_tool_skips_links_into_the_canonical_root(self):
        # dashboard「从中央装到某工具」走 apply；照常替换会把 fanout 的软链换成实体副本
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "canonical"
            (canonical / "linked").mkdir(parents=True)
            staged = root / "stage"
            for name in ("linked", "plain"):
                (staged / name).mkdir(parents=True)
                (staged / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\n", encoding="utf-8")
            (staged / ".stage-index.json").write_text(json.dumps({"skills": [
                {"skill_id": n, "key": n, "content_hash": "h", "output_path": str(staged / n), "scope": "global", "targets": ["codex"]}
                for n in ("linked", "plain")
            ]}), encoding="utf-8")
            tool = root / "tool"
            tool.mkdir()
            (tool / "linked").symlink_to(canonical / "linked")
            config = root / "config.json"
            config.write_text(json.dumps({"local_root": str(canonical)}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"SKILL_SYNC_CONFIG": str(config)}):
                plan = build_apply_plan(staged, "codex-global", target_root=tool)
            items = {i["skill_id"]: i for i in plan["items"]}
            self.assertFalse(items["linked"]["allowed"])
            self.assertIn("fanout", items["linked"]["reason"])
            self.assertTrue(items["plain"]["allowed"])


if __name__ == "__main__":
    unittest.main()
