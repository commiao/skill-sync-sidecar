import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from skill_sync_sidecar import operator_executor
from skill_sync_sidecar.config import (
    ConfigError,
    configured_base_record,
    configured_remote_prefix,
    configured_remote_snapshot,
)

# 第 3 步之前 operator_executor 里写死的值：没有配置时必须一字不差
LEGACY_SNAPSHOT = Path.home() / "public-sync" / "skill-sync-sidecar-dev" / "current-mac"
LEGACY_PREFIX = "skill-sync-sidecar-dev/current-mac"
LEGACY_BASE = Path.home() / "Library" / "Application Support" / "skill-sync-sidecar" / "base-record.json"


def _env_without_prefix():
    env = dict(os.environ)
    env.pop("SKILL_SYNC_PREFIX", None)
    return env


class ConfiguredPathsTest(unittest.TestCase):
    def test_defaults_match_previous_hardcoded_values(self):
        with TemporaryDirectory() as tmp, mock.patch.dict(os.environ, _env_without_prefix(), clear=True):
            missing = Path(tmp) / "missing.json"
            self.assertEqual(configured_remote_snapshot(missing), LEGACY_SNAPSHOT)
            self.assertEqual(configured_remote_prefix(missing), LEGACY_PREFIX)
            if sys.platform == "darwin":
                self.assertEqual(configured_base_record(missing), LEGACY_BASE)

    def test_config_overrides(self):
        with TemporaryDirectory() as tmp, mock.patch.dict(os.environ, _env_without_prefix(), clear=True):
            config = Path(tmp) / "config.json"
            config.write_text(
                json.dumps({"remote_snapshot": "~/snap", "remote_prefix": "team/dev", "base_record": "~/base.json"}),
                encoding="utf-8",
            )
            self.assertEqual(configured_remote_snapshot(config), Path("~/snap").expanduser())
            self.assertEqual(configured_remote_prefix(config), "team/dev")
            self.assertEqual(configured_base_record(config), Path("~/base.json").expanduser())

    def test_env_prefix_wins_over_config(self):
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"remote_prefix": "team/dev"}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"SKILL_SYNC_PREFIX": "from/env"}):
                self.assertEqual(configured_remote_prefix(config), "from/env")

    def test_invalid_values_are_errors(self):
        with TemporaryDirectory() as tmp, mock.patch.dict(os.environ, _env_without_prefix(), clear=True):
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"remote_snapshot": 3, "remote_prefix": "/"}), encoding="utf-8")
            with self.assertRaises(ConfigError):
                configured_remote_snapshot(config)
            with self.assertRaises(ConfigError):
                configured_remote_prefix(config)


class ExecutorUsesConfigTest(unittest.TestCase):
    def test_central_deprecate_preview_reads_configured_snapshot(self):
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"remote_snapshot": str(Path(tmp) / "snap")}), encoding="utf-8")
            seen = {}

            def fake_preview(snapshot_dir, skill_ids, **kwargs):
                seen["snapshot_dir"] = snapshot_dir
                return {"ok": True}

            with mock.patch.dict(os.environ, {"SKILL_SYNC_CONFIG": str(config)}), mock.patch.object(
                operator_executor, "build_central_deprecate_preview", fake_preview
            ):
                operator_executor.run_central_deprecate(["demo"])
            self.assertEqual(seen["snapshot_dir"], Path(tmp) / "snap")

    def test_central_restore_reads_configured_paths(self):
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(
                json.dumps({
                    "local_root": str(Path(tmp) / "skills"),
                    "remote_snapshot": str(Path(tmp) / "snap"),
                    "remote_prefix": "team/dev",
                    "base_record": str(Path(tmp) / "base.json"),
                }),
                encoding="utf-8",
            )
            seen = {}

            def fake_restore(local_root, snapshot_dir, skill_ids, **kwargs):
                seen.update(local_root=local_root, snapshot_dir=snapshot_dir, **kwargs)
                return {"ok": True}

            env = _env_without_prefix()
            env["SKILL_SYNC_CONFIG"] = str(config)
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(operator_executor, "restore_from_central", fake_restore):
                operator_executor.run_mac_central_restore(["demo"])
            self.assertEqual(seen["local_root"], Path(tmp) / "skills")
            self.assertEqual(seen["snapshot_dir"], Path(tmp) / "snap")
            self.assertEqual(seen["remote_prefix"], "team/dev")
            self.assertEqual(seen["base_record_out"], Path(tmp) / "base.json")


if __name__ == "__main__":
    unittest.main()
