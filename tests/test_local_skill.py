import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from skill_sync_sidecar.local_skill import LocalSkillError, LocalToolTarget, analyze_local_skill, install_local_skill, publish_local_skill
from skill_sync_sidecar.remote import FileRemote, upload_snapshot
from skill_sync_sidecar.scanner import scan_roots
from skill_sync_sidecar.snapshot import write_snapshot


class LocalSkillTest(unittest.TestCase):
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

            with self.assertRaises(LocalSkillError):
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
