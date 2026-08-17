import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from skill_sync_sidecar.local_skill import LocalSkillError, LocalToolTarget, analyze_local_skill, install_local_skill, publish_local_skill
from skill_sync_sidecar.remote import FileRemote, upload_snapshot
from skill_sync_sidecar.scanner import scan_roots
from skill_sync_sidecar.snapshot import write_snapshot


class LocalSkillTest(unittest.TestCase):
    def test_qoder_wired_into_tool_target_maps(self):
        from skill_sync_sidecar.local_skill import DEFAULT_LOCAL_TOOL_TARGETS
        from skill_sync_sidecar.operator_executor import MAC_TOOL_INSTALL_TARGETS
        from skill_sync_sidecar.apply import GLOBAL_TOOL_TARGETS
        from skill_sync_sidecar.cli import APPLY_TARGETS
        from skill_sync_sidecar.sync_apply import TARGET_ALIASES, TARGET_SCOPES

        local_ids = {target.tool_id for target in DEFAULT_LOCAL_TOOL_TARGETS}
        self.assertIn("qoder", local_ids)
        qoder_local = next(t for t in DEFAULT_LOCAL_TOOL_TARGETS if t.tool_id == "qoder")
        self.assertEqual(qoder_local.root.name, "skills")
        self.assertEqual(qoder_local.root.parent.name, ".qoder")

        self.assertIn("qoder", MAC_TOOL_INSTALL_TARGETS)
        self.assertEqual(MAC_TOOL_INSTALL_TARGETS["qoder"][0], "qoder-global")
        self.assertEqual(MAC_TOOL_INSTALL_TARGETS["qoder"][1], (".qoder", "skills"))

        self.assertIn("qoder-global", GLOBAL_TOOL_TARGETS)
        self.assertIn("qoder", GLOBAL_TOOL_TARGETS["qoder-global"]["aliases"])
        self.assertIn("qoder-global", APPLY_TARGETS)
        self.assertEqual(TARGET_SCOPES["qoder-global"], "global")
        self.assertIn("qoder", TARGET_ALIASES["qoder-global"])

    def test_deepseek_harness_wired_into_tool_target_maps(self):
        from skill_sync_sidecar.apply import GLOBAL_TOOL_TARGETS
        from skill_sync_sidecar.cli import APPLY_TARGETS
        from skill_sync_sidecar.local_skill import DEFAULT_LOCAL_TOOL_TARGETS, DEFAULT_GLOBAL_TARGETS
        from skill_sync_sidecar.operator_executor import MAC_TOOL_INSTALL_TARGETS
        from skill_sync_sidecar.sync_apply import TARGET_ALIASES, TARGET_SCOPES
        from skill_sync_sidecar.tool_status import DEFAULT_TOOL_ROOTS

        local_ids = {target.tool_id for target in DEFAULT_LOCAL_TOOL_TARGETS}
        self.assertIn("deepseek-harness", local_ids)
        deepseek_local = next(t for t in DEFAULT_LOCAL_TOOL_TARGETS if t.tool_id == "deepseek-harness")
        self.assertEqual(deepseek_local.name, "DeepSeek Harness")
        self.assertEqual(deepseek_local.root.name, "skills")
        self.assertEqual(deepseek_local.root.parent.name, ".deepseek-harness")
        self.assertEqual(deepseek_local.target_alias, "deepseek-harness")

        self.assertIn("deepseek-harness", DEFAULT_GLOBAL_TARGETS)
        self.assertIn("deepseek-harness", MAC_TOOL_INSTALL_TARGETS)
        self.assertEqual(MAC_TOOL_INSTALL_TARGETS["deepseek-harness"][0], "deepseek-harness-global")
        self.assertEqual(MAC_TOOL_INSTALL_TARGETS["deepseek-harness"][1], (".deepseek-harness", "skills"))

        self.assertIn("deepseek-harness-global", GLOBAL_TOOL_TARGETS)
        self.assertIn("deepseek-harness", GLOBAL_TOOL_TARGETS["deepseek-harness-global"]["aliases"])
        self.assertIn("deepseek", GLOBAL_TOOL_TARGETS["deepseek-harness-global"]["aliases"])
        self.assertIn("deepseek-harness-global", APPLY_TARGETS)
        self.assertEqual(TARGET_SCOPES["deepseek-harness-global"], "global")
        self.assertIn("deepseek-harness", TARGET_ALIASES["deepseek-harness-global"])
        self.assertIn("deepseek", TARGET_ALIASES["deepseek-harness-global"])

        status_ids = {item[0] for item in DEFAULT_TOOL_ROOTS}
        self.assertIn("deepseek-harness", status_ids)

    def test_analyze_generates_global_manifest_for_tool_root_skill(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex = root / ".codex" / "skills"
            source = codex / "read-wechat-article"
            tool_root = root / "cc-switch"
            self._write_skill(source, "read-wechat-article")
            tool_root.mkdir(parents=True)

            result = analyze_local_skill(
                source,
                tool_targets=[LocalToolTarget("cc-switch", "cc-switch", tool_root, "cc-switch")],
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["skill_id"], "read-wechat-article")
            self.assertEqual(result["scope"], "global")
            self.assertEqual(result["manifest_source"], "generated")
            self.assertIn("cc-switch", result["targets"])
            self.assertEqual(result["summary"]["install_new"], 1)
            self.assertEqual(result["summary"]["will_write"], 1)

    def test_install_dry_run_does_not_write_target(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source" / "demo"
            target_root = root / "target"
            self._write_skill(source, "demo")
            target_root.mkdir()

            result = install_local_skill(
                source,
                tool_targets=[LocalToolTarget("codex", "Codex", target_root, "codex")],
            )

            self.assertTrue(result["dry_run"])
            self.assertEqual(result["summary"]["will_write"], 1)
            self.assertFalse((target_root / "demo").exists())

    def test_install_writes_manifest_and_backup_record(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source" / "demo"
            target_root = root / "target"
            self._write_skill(source, "demo")
            target_root.mkdir()

            result = install_local_skill(
                source,
                tool_targets=[LocalToolTarget("codex", "Codex", target_root, "codex")],
                yes=True,
                allow_local_writes=True,
            )

            installed = target_root / "demo"
            self.assertFalse(result["dry_run"])
            self.assertTrue((installed / "SKILL.md").exists())
            manifest = json.loads((installed / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["skill_id"], "demo")
            self.assertEqual(manifest["scope"], "global")
            self.assertIn("codex", manifest["targets"])
            self.assertTrue(Path(result["record_path"]).exists())

    def test_replace_existing_skill_writes_backup(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source" / "demo"
            target_root = root / "target"
            target = target_root / "demo"
            self._write_skill(source, "demo", body="new")
            self._write_skill(target, "demo", body="old")

            result = install_local_skill(
                source,
                tool_targets=[LocalToolTarget("codex", "Codex", target_root, "codex")],
                yes=True,
                allow_local_writes=True,
            )

            item = result["items"][0]
            self.assertEqual(item["action"], "replace_with_backup")
            self.assertTrue(Path(item["backup_path"]).exists())
            self.assertIn("old", (Path(item["backup_path"]) / "SKILL.md").read_text(encoding="utf-8"))
            self.assertIn("new", (target / "SKILL.md").read_text(encoding="utf-8"))

    def test_existing_same_skill_without_manifest_gets_metadata_only(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source" / "demo"
            target_root = root / "target"
            target = target_root / "demo"
            self._write_skill(source, "demo", body="same")
            self._write_skill(target, "demo", body="same")

            result = install_local_skill(
                source,
                tool_targets=[LocalToolTarget("codex", "Codex", target_root, "codex")],
                yes=True,
                allow_local_writes=True,
            )

            item = result["items"][0]
            self.assertEqual(item["action"], "write_manifest")
            self.assertTrue((target / "manifest.json").exists())
            self.assertIsNone(item["backup_path"])

    def test_secret_like_file_blocks_local_install(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source" / "demo"
            target_root = root / "target"
            self._write_skill(source, "demo")
            (source / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            target_root.mkdir()

            preview = analyze_local_skill(
                source,
                tool_targets=[LocalToolTarget("codex", "Codex", target_root, "codex")],
            )

            self.assertEqual(preview["risk"]["level"], "error")
            self.assertEqual(preview["summary"]["will_write"], 0)
            self.assertFalse(preview["tools"][0]["allowed"])

            result = install_local_skill(
                source,
                tool_targets=[LocalToolTarget("codex", "Codex", target_root, "codex")],
                yes=True,
                allow_local_writes=True,
            )
            self.assertFalse((target_root / "demo").exists())
            self.assertEqual(result["summary"]["will_write"], 0)

    def test_publish_local_skill_merges_selected_new_skill(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_root = root / "local"
            canonical_root = root / "canonical"
            remote_snapshot = root / "cache"
            remote_root = root / "remote"
            self._write_skill(local_root / "demo", "demo", body="local")
            self._write_manifest(local_root / "demo", "demo")
            self._write_skill(canonical_root / "existing", "existing", body="remote")
            self._write_manifest(canonical_root / "existing", "existing")
            write_snapshot(scan_roots([f"canonical={canonical_root}"]), remote_snapshot, "remote-base")
            remote = FileRemote(remote_root)
            upload_snapshot(remote_snapshot, remote)

            preview = publish_local_skill(local_root, remote_snapshot, "demo", remote)
            self.assertTrue(preview["dry_run"])
            self.assertEqual(preview["item"]["plan_action"], "push_new")

            result = publish_local_skill(local_root, remote_snapshot, "demo", remote, yes=True)

            self.assertFalse(result["dry_run"])
            index = json.loads((remote_root / "index.json").read_text(encoding="utf-8"))
            self.assertIn("demo", {skill["skill_id"] for skill in index["skills"]})
            self.assertIn("existing", {skill["skill_id"] for skill in index["skills"]})

    def test_publish_local_skill_ignores_unrelated_duplicate_local_skill_ids(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_root = root / "local"
            canonical_root = root / "canonical"
            remote_snapshot = root / "cache"
            remote_root = root / "remote"
            self._write_skill(local_root / "demo", "demo", body="local")
            self._write_manifest(local_root / "demo", "demo")
            self._write_skill(local_root / "one", "duplicate", body="one")
            self._write_manifest(local_root / "one", "duplicate")
            self._write_skill(local_root / "two", "duplicate", body="two")
            self._write_manifest(local_root / "two", "duplicate")
            self._write_skill(canonical_root / "existing", "existing", body="remote")
            self._write_manifest(canonical_root / "existing", "existing")
            write_snapshot(scan_roots([f"canonical={canonical_root}"]), remote_snapshot, "remote-base")
            remote = FileRemote(remote_root)
            upload_snapshot(remote_snapshot, remote)

            preview = publish_local_skill(local_root, remote_snapshot, "demo", remote)

            self.assertTrue(preview["ok"])
            self.assertEqual(preview["skill_id"], "demo")
            self.assertEqual(preview["item"]["plan_action"], "push_new")

    def test_secret_like_file_blocks_central_publish(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_root = root / "local"
            canonical_root = root / "canonical"
            remote_snapshot = root / "cache"
            remote_root = root / "remote"
            self._write_skill(local_root / "demo", "demo", body="local")
            self._write_manifest(local_root / "demo", "demo")
            (local_root / "demo" / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            self._write_skill(canonical_root / "existing", "existing", body="remote")
            self._write_manifest(canonical_root / "existing", "existing")
            write_snapshot(scan_roots([f"canonical={canonical_root}"]), remote_snapshot, "remote-base")
            remote = FileRemote(remote_root)
            upload_snapshot(remote_snapshot, remote)

            with self.assertRaisesRegex(LocalSkillError, "shared-library save is blocked"):
                publish_local_skill(local_root, remote_snapshot, "demo", remote)

    def _write_skill(self, path: Path, skill_id: str, body: str = "body") -> None:
        path.mkdir(parents=True, exist_ok=True)
        (path / "SKILL.md").write_text(
            f"---\nname: {skill_id}\ndescription: {skill_id} skill\n---\n{body}\n",
            encoding="utf-8",
        )

    def _write_manifest(self, path: Path, skill_id: str) -> None:
        (path / "manifest.json").write_text(
            json.dumps(
                {
                    "protocol_version": 0,
                    "skill_id": skill_id,
                    "scope": "global",
                    "targets": ["cc-switch", "codex"],
                }
            )
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
