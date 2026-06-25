import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from skill_sync_sidecar.hub_import import (
    build_hub_import_diagnosis,
    build_hub_import_preview_package,
    execute_hub_import_apply,
    parse_hub_source_spec,
)
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
            self.assertEqual(items[("agents", "same")]["status_label"], "已在 Hub")
            self.assertEqual(items[("agents", "stale")]["operator_action"], "先看差异再更新")
            self.assertEqual(items[("agents", "fresh")]["reason_label"], "Hub 中没有这个 skill ID。")
            self.assertIn("resolves", items[("codex", "same")]["reason"])
            self.assertEqual(diagnosis["items"][0]["status"], "importable")
            action_summary = diagnosis["action_plan"]["summary"]
            self.assertEqual(action_summary["preview_import"], 1)
            self.assertEqual(action_summary["review_update"], 1)
            self.assertEqual(action_summary["skip_existing"], 2)
            self.assertFalse(diagnosis["action_plan"]["safe_to_apply_automatically"])
            actions = {(action["source"], action["skill_id"]): action for action in diagnosis["action_plan"]["actions"]}
            self.assertEqual(actions[("agents", "fresh")]["action"], "preview_import")
            self.assertFalse(actions[("agents", "fresh")]["writes_files"])
            self.assertEqual(actions[("agents", "stale")]["action"], "review_update")
            self.assertTrue(actions[("agents", "stale")]["requires_review"])

    def test_parse_hub_source_spec(self):
        source_id, path = parse_hub_source_spec("agents=~/skills")

        self.assertEqual(source_id, "agents")
        self.assertEqual(path, Path("~/skills").expanduser())

    def test_hub_import_plan_requires_review_for_duplicate_import_sources(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub = root / "hub"
            agents = root / "agents"
            codex = root / "codex"

            hub.mkdir(parents=True)
            self._write_skill(agents / "fresh", "fresh", "global", ["skillshub"], body="agents")
            self._write_skill(codex / "fresh", "fresh", "global", ["skillshub"], body="codex")

            diagnosis = build_hub_import_diagnosis(hub, [("agents", agents), ("codex", codex)])

            self.assertEqual(diagnosis["summary"]["importable"], 2)
            self.assertEqual(diagnosis["action_plan"]["summary"]["review_duplicate_import"], 2)
            self.assertEqual(diagnosis["action_plan"]["review_required"], 2)
            for action in diagnosis["action_plan"]["actions"]:
                self.assertEqual(action["action"], "review_duplicate_import")
                self.assertTrue(action["requires_review"])
                self.assertFalse(action["writes_files"])

    def test_hub_import_preview_package_writes_auditable_dry_run_files(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub = root / "hub"
            agents = root / "agents"
            out = root / "preview"

            self._write_skill(hub / "stale", "stale", "global", ["skillshub"], body="old")
            self._write_skill(agents / "stale", "stale", "global", ["skillshub"], body="new")
            self._write_skill(agents / "fresh", "fresh", "global", ["skillshub"], body="fresh")

            package = build_hub_import_preview_package(hub, [("agents", agents)], out_dir=out)

            self.assertEqual(package["mode"], "dry_run")
            self.assertFalse(package["writes_files"])
            self.assertTrue((out / "preview.json").exists())
            self.assertTrue((out / "preview.md").exists())
            self.assertEqual(package["action_summary"]["preview_import"], 1)
            self.assertEqual(package["action_summary"]["review_update"], 1)
            actions = {action["skill_id"]: action for action in package["actions"]}
            self.assertEqual(actions["fresh"]["action"], "preview_import")
            self.assertEqual(actions["stale"]["action"], "review_update")
            self.assertTrue(actions["stale"]["skill_md_diff"]["ok"])
            self.assertIn("-old", "\n".join(actions["stale"]["skill_md_diff"]["lines"]))
            self.assertIn("+new", "\n".join(actions["stale"]["skill_md_diff"]["lines"]))
            self.assertIn("Skillshub Import Preview", (out / "preview.md").read_text(encoding="utf-8"))

    def test_hub_import_skips_skills_not_compatible_with_skillshub(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub = root / "hub"
            agents = root / "agents"
            out = root / "preview"

            self._write_skill(agents / "global-hub", "global-hub", "global", ["skillshub"], body="hub")
            self._write_skill(agents / "project-only", "project-only", "project", ["codex", "cursor"], body="project")
            self._write_skill(agents / "global-codex", "global-codex", "global", ["codex"], body="codex")

            diagnosis = build_hub_import_diagnosis(hub, [("agents", agents)])

            self.assertEqual(diagnosis["summary"], {"importable": 1, "not_compatible": 2})
            actions = {action["skill_id"]: action for action in diagnosis["action_plan"]["actions"]}
            self.assertEqual(actions["global-hub"]["action"], "preview_import")
            self.assertEqual(actions["project-only"]["action"], "skip_incompatible")
            self.assertEqual(actions["global-codex"]["action"], "skip_incompatible")

            package = build_hub_import_preview_package(hub, [("agents", agents)], out_dir=out)
            self.assertEqual(package["action_summary"], {"preview_import": 1, "skip_incompatible": 2})
            self.assertEqual([action["skill_id"] for action in package["actions"]], ["global-hub"])

    def test_hub_import_uses_canonical_snapshot_when_source_manifest_is_missing(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub = root / "hub"
            source = root / "source"
            snapshot = root / "snapshot"

            self._write_skill(source / "project-only", "project-only", "global", ["skillshub"], body="local")
            (source / "project-only" / "manifest.json").unlink()
            snapshot.mkdir(parents=True)
            (snapshot / "index.json").write_text(
                json.dumps(
                    {
                        "protocol_version": 0,
                        "snapshot_id": "canonical",
                        "skills": [
                            {
                                "skill_id": "project-only",
                                "scope": "project",
                                "targets": ["codex", "cursor"],
                                "content_hash": "abc",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            diagnosis = build_hub_import_diagnosis(hub, [("source", source)], canonical_snapshot=snapshot)

            self.assertEqual(diagnosis["summary"], {"not_compatible": 1})
            action = diagnosis["action_plan"]["actions"][0]
            self.assertEqual(action["skill_id"], "project-only")
            self.assertEqual(action["action"], "skip_incompatible")

    def test_hub_import_apply_only_imports_new_preview_actions_with_yes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub = root / "hub"
            agents = root / "agents"
            preview_out = root / "preview"
            apply_out = root / "apply"

            self._write_skill(hub / "stale", "stale", "global", ["skillshub"], body="old")
            self._write_skill(agents / "stale", "stale", "global", ["skillshub"], body="new")
            self._write_skill(agents / "fresh", "fresh", "global", ["skillshub"], body="fresh")

            package = build_hub_import_preview_package(hub, [("agents", agents)], out_dir=preview_out)
            preview_json = Path(package["preview_json"])

            plan = execute_hub_import_apply(preview_json)
            self.assertTrue(plan["dry_run"])
            self.assertEqual(plan["allowed"], 1)
            self.assertEqual(plan["blocked"], 1)
            self.assertFalse((hub / "fresh").exists())

            record = execute_hub_import_apply(preview_json, yes=True, out_dir=apply_out)
            self.assertFalse(record["dry_run"])
            self.assertEqual(record["imported"], 1)
            self.assertEqual(record["blocked"], 1)
            self.assertTrue((hub / "fresh" / "SKILL.md").exists())
            self.assertIn("fresh", (hub / "fresh" / "SKILL.md").read_text(encoding="utf-8"))
            self.assertIn("old", (hub / "stale" / "SKILL.md").read_text(encoding="utf-8"))
            self.assertTrue((apply_out / "hub-import-apply-record.json").exists())

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
