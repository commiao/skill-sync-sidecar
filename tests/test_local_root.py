import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from skill_sync_sidecar import cli
from skill_sync_sidecar import local_root as local_root_module
from skill_sync_sidecar.config import (
    LEGACY_LOCAL_ROOT,
    ConfigError,
    configured_local_root,
    write_config_local_root,
)
from skill_sync_sidecar.local_root import LocalRootMigrationError, migrate_local_root
from skill_sync_sidecar.local_skill import default_local_tool_targets
from skill_sync_sidecar.scanner import scan_roots
from skill_sync_sidecar.snapshot import write_snapshot


def _write_skill(root: Path, name: str, body: str = "body") -> None:
    skill = root / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} skill\n---\n\n{body}\n", encoding="utf-8")


def _hashes(root: Path) -> dict:
    return {s.skill_id: s.content_hash for s in scan_roots([f"x={root}"]).skills}


class ConfiguredLocalRootTest(unittest.TestCase):
    def test_defaults_to_cc_switch_without_config(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(configured_local_root(Path(tmp) / "missing.json"), LEGACY_LOCAL_ROOT)

    def test_reads_local_root_from_config(self):
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"local_root": "~/somewhere"}), encoding="utf-8")
            self.assertEqual(configured_local_root(config), Path("~/somewhere").expanduser())

    def test_invalid_config_is_an_error(self):
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text("{broken", encoding="utf-8")
            with self.assertRaises(ConfigError):
                configured_local_root(config)

    def test_write_keeps_other_keys_and_is_private(self):
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"webdav": {"base_url": "https://nas.example/dav"}}), encoding="utf-8")
            write_config_local_root(Path(tmp) / "skills", config)
            data = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(data["webdav"], {"base_url": "https://nas.example/dav"})
            self.assertEqual(data["local_root"], str(Path(tmp) / "skills"))
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)


class MigrateLocalRootTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.source = self.tmp / "cc" / "skills"
        _write_skill(self.source, "alpha")
        _write_skill(self.source, "beta")
        (self.source / ".skill-sync-backups" / "old").mkdir(parents=True)
        (self.source / ".skill-sync-local-overrides.json").write_text('{"skills": {}}', encoding="utf-8")
        self.remote = self.tmp / "remote"
        write_snapshot(scan_roots([f"cc-switch={self.source}"]), self.remote, "remote")
        # 本地有一处待推送的改动：迁移后它必须原样还在
        _write_skill(self.source, "beta", "changed locally")
        self.config = self.tmp / "app" / "config.json"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({"local_root": str(self.source)}), encoding="utf-8")
        self.dest = self.tmp / "app" / "skills"

    def tearDown(self):
        self._tmp.cleanup()

    def migrate(self, **kwargs):
        return migrate_local_root(self.source, kwargs.pop("dest", self.dest), self.remote, None, config_path=self.config, **kwargs)

    def test_dry_run_writes_nothing(self):
        result = self.migrate()
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["skills"], 2)
        self.assertFalse(self.dest.exists())
        self.assertEqual(configured_local_root(self.config), self.source)

    def test_yes_copies_verifies_and_switches_config(self):
        result = self.migrate(yes=True)

        self.assertTrue(result["config_updated"])
        self.assertEqual(configured_local_root(self.config), self.dest)
        self.assertEqual(_hashes(self.dest), _hashes(self.source))
        self.assertTrue((self.dest / ".skill-sync-local-overrides.json").exists())
        self.assertFalse((self.dest / ".skill-sync-backups").exists())
        # 旧根原样保留（迁移后它只是 cc-switch 这个工具的目录）
        self.assertTrue((self.source / "alpha" / "SKILL.md").exists())
        # beta 在旧根上有待推送的改动；新根算出的同步动作必须一样，不是全 unchanged
        self.assertNotEqual(set(result["sync_actions"]), {"unchanged"})

    def test_refuses_non_empty_destination(self):
        _write_skill(self.dest, "stray")
        with self.assertRaises(LocalRootMigrationError):
            self.migrate(yes=True)
        self.assertEqual(configured_local_root(self.config), self.source)

    def test_refuses_when_already_migrated(self):
        self.migrate(yes=True)
        with self.assertRaises(LocalRootMigrationError):
            self.migrate(dest=self.tmp / "other", yes=True)

    def test_refuses_destination_that_would_scan_as_project_scope(self):
        (self.dest.parent / "AGENTS.md").write_text("x", encoding="utf-8")
        with self.assertRaises(LocalRootMigrationError) as ctx:
            self.migrate()
        self.assertIn("project scope", str(ctx.exception))

    def test_verification_failure_rolls_back(self):
        real = local_root_module._hashes
        calls = []

        def flaky(root):
            calls.append(root)
            value = real(root)
            if len(calls) == 2:  # 第二次是复制后的新根
                value = dict(value, alpha="tampered")
            return value

        with mock.patch.object(local_root_module, "_hashes", side_effect=flaky):
            with self.assertRaises(LocalRootMigrationError):
                self.migrate(yes=True)
        self.assertFalse(self.dest.exists())
        self.assertEqual(list(self.dest.parent.glob("skills.migrating-*")), [])
        self.assertEqual(configured_local_root(self.config), self.source)


class DefaultToolTargetsTest(unittest.TestCase):
    def test_no_extra_target_before_migration(self):
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            with mock.patch.dict(os.environ, {"SKILL_SYNC_CONFIG": str(config)}):
                ids = [t.tool_id for t in default_local_tool_targets()]
            self.assertNotIn("sidecar", ids)
            self.assertIn("cc-switch", ids)

    def test_sidecar_root_is_an_install_target_after_migration(self):
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"local_root": str(Path(tmp) / "skills")}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"SKILL_SYNC_CONFIG": str(config)}):
                targets = default_local_tool_targets()
            self.assertEqual(targets[0].tool_id, "sidecar")
            self.assertEqual(targets[0].root, Path(tmp) / "skills")


class CliLocalRootDefaultTest(unittest.TestCase):
    def test_commands_take_local_root_from_config(self):
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"local_root": str(Path(tmp) / "skills")}), encoding="utf-8")
            seen = {}

            def fake(args):
                seen["local_root"] = args.local_root
                return 0

            with mock.patch.dict(os.environ, {"SKILL_SYNC_CONFIG": str(config)}), mock.patch.object(cli, "cmd_ops_status", fake):
                self.assertEqual(cli.main(["ops-status"]), 0)
            self.assertEqual(seen["local_root"], str(Path(tmp) / "skills"))

    def test_explicit_local_root_wins(self):
        seen = {}

        def fake(args):
            seen["local_root"] = args.local_root
            return 0

        with mock.patch.object(cli, "cmd_ops_status", fake):
            cli.main(["ops-status", "--local-root", "/explicit"])
        self.assertEqual(seen["local_root"], "/explicit")

    def test_broken_config_fails_the_command(self):
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text("{broken", encoding="utf-8")
            with mock.patch.dict(os.environ, {"SKILL_SYNC_CONFIG": str(config)}), mock.patch.object(cli, "cmd_ops_status", lambda a: 0):
                self.assertEqual(cli.main(["ops-status"]), 2)


if __name__ == "__main__":
    unittest.main()
