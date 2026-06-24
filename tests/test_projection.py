import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from skill_sync_sidecar.hub_import import build_hub_import_diagnosis, parse_hub_source_spec
from skill_sync_sidecar.projection import ToolAdapter, build_tool_projection, parse_tool_adapter_spec
from skill_sync_sidecar.scanner import scan_roots
from skill_sync_sidecar.snapshot import write_snapshot


class ToolProjectionTest(unittest.TestCase):
    def test_projection_classifies_tool_install_gaps(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "canonical"
            snapshot = root / "snapshot"
            codex_root = root / "codex"
            cursor_root = root / "cursor"

            self._write_skill(canonical / "shared", "shared", "global", ["codex", "cursor"], body="same")
            self._write_skill(canonical / "stale", "stale", "global", ["codex"], body="remote")
            self._write_skill(canonical / "cursor-missing", "cursor-missing", "global", ["cursor"], body="missing")
            self._write_skill(canonical / "project-codex", "project-codex", "project", ["codex"], body="project")
            self._write_skill(canonical / "hub-only", "hub-only", "global", ["skillshub"], body="hub")

            self._write_skill(codex_root / "shared", "shared", "global", ["codex", "cursor"], body="same")
            self._write_skill(codex_root / "stale", "stale", "global", ["codex"], body="local")
            self._write_skill(codex_root / "local-extra", "local-extra", "global", ["codex"], body="extra")

            write_snapshot(scan_roots([f"canonical={canonical}"]), snapshot, "snap-projection")

            projection = build_tool_projection(
                snapshot,
                adapters=[
                    ToolAdapter("codex", "Codex", [codex_root], ["codex"], ["global"]),
                    ToolAdapter("cursor", "Cursor", [cursor_root], ["cursor"], ["global"]),
                ],
            )
            tools = {tool["id"]: tool for tool in projection["tools"]}

            self.assertEqual(projection["canonical_total"], 5)
            self.assertEqual(tools["codex"]["canonical_targeted"], 3)
            self.assertEqual(tools["codex"]["summary"]["installed"], 1)
            self.assertEqual(tools["codex"]["summary"]["drift"], 1)
            self.assertEqual(tools["codex"]["summary"]["unsupported_scope"], 1)
            self.assertEqual(tools["codex"]["summary"]["not_targeted"], 2)
            self.assertEqual(tools["codex"]["extra_local"][0]["skill_id"], "local-extra")
            self.assertEqual(tools["cursor"]["summary"]["missing"], 2)
            self.assertEqual(tools["cursor"]["summary"]["not_targeted"], 3)

    def test_parse_tool_adapter_spec_uses_default_metadata(self):
        adapter = parse_tool_adapter_spec("codex=/tmp/a,/tmp/b")

        self.assertEqual(adapter.tool_id, "codex")
        self.assertEqual(adapter.name, "Codex")
        self.assertEqual(adapter.target_aliases, ["codex"])
        self.assertEqual([str(path) for path in adapter.roots], ["/tmp/a", "/tmp/b"])

    def test_hub_import_diagnosis_classifies_duplicates_updates_and_imports(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub = root / "hub"
            agents = root / "agents"
            codex = root / "codex"

            self._write_skill(hub / "same", "same", "global", ["skillshub"], body="same")
            self._write_skill(agents / "same", "same", "global", ["skillshub"], body="same")
            self._write_skill(hub / "stale", "stale", "global", ["skillshub"], body="old")
            self._write_skill(agents / "stale", "stale", "global", ["skillshub"], body="new")
            self._write_skill(agents / "fresh", "fresh", "global", ["skillshub"], body="fresh")
            codex.mkdir(parents=True)
            (codex / "same-link").symlink_to(hub / "same", target_is_directory=True)

            diagnosis = build_hub_import_diagnosis(hub, [("agents", agents), ("codex", codex)])
            items = {(item["source"], item["skill_id"]): item for item in diagnosis["items"]}

            self.assertEqual(diagnosis["summary"]["already_in_hub"], 2)
            self.assertEqual(diagnosis["summary"]["update_available"], 1)
            self.assertEqual(diagnosis["summary"]["importable"], 1)
            self.assertEqual(items[("agents", "same")]["status"], "already_in_hub")
            self.assertEqual(items[("agents", "stale")]["status"], "update_available")
            self.assertEqual(items[("agents", "fresh")]["status"], "importable")
            self.assertEqual(items[("codex", "same")]["status"], "already_in_hub")
            self.assertIn("resolves", items[("codex", "same")]["reason"])

    def test_parse_hub_source_spec(self):
        source_id, path = parse_hub_source_spec("agents=~/skills")

        self.assertEqual(source_id, "agents")
        self.assertEqual(path, Path("~/skills").expanduser())

    def _write_skill(self, skill: Path, skill_id: str, scope: str, targets: list[str], body: str):
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {skill_id}\ndescription: {skill_id} skill\n---\n{body}\n",
            encoding="utf-8",
        )
        (skill / "manifest.json").write_text(
            json.dumps(
                {
                    "protocol_version": 0,
                    "skill_id": skill_id,
                    "scope": scope,
                    "targets": targets,
                }
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
