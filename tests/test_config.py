import json
import os
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from skill_sync_sidecar import cli
from skill_sync_sidecar.config import (
    ConfigError,
    default_config_path,
    init_config_from_cc_switch,
    load_sidecar_webdav_settings,
    load_webdav_settings,
)

CC_SWITCH_SETTINGS = {
    "webdavSync": {"baseUrl": "https://cc.example/dav", "username": "u", "password": "cc-secret-123"}
}


def _write_json(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _no_keychain(service: str, account: str) -> str:
    raise AssertionError("keychain must not be read in this test")


class SidecarConfigTest(unittest.TestCase):
    def test_keychain_password_source(self):
        with TemporaryDirectory() as tmp:
            config = _write_json(
                Path(tmp) / "config.json",
                {"webdav": {"base_url": "https://nas.example/dav", "username": "me",
                            "password_source": {"keychain": {"account": "me"}}}},
            )
            calls = []

            def reader(service, account):
                calls.append((service, account))
                return "secret"

            settings = load_sidecar_webdav_settings(config, keychain_reader=reader)

            self.assertEqual(settings.base_url, "https://nas.example/dav")
            self.assertEqual(settings.username, "me")
            self.assertEqual(settings.password, "secret")
            self.assertEqual(calls, [("skill-sync-sidecar-webdav", "me")])

    def test_env_password_source(self):
        with TemporaryDirectory() as tmp:
            config = _write_json(
                Path(tmp) / "config.json",
                {"webdav": {"base_url": "https://nas.example/dav", "password_source": {"env": "T0260_PW"}}},
            )
            with mock.patch.dict(os.environ, {"T0260_PW": "from-env"}):
                self.assertEqual(load_sidecar_webdav_settings(config, _no_keychain).password, "from-env")
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ConfigError):
                    load_sidecar_webdav_settings(config, _no_keychain)

    def test_file_password_source_requires_private_mode(self):
        with TemporaryDirectory() as tmp:
            pw = Path(tmp) / "pw"
            pw.write_text("from-file\n", encoding="utf-8")
            config = _write_json(
                Path(tmp) / "config.json",
                {"webdav": {"base_url": "https://nas.example/dav", "password_source": {"file": str(pw)}}},
            )
            os.chmod(pw, 0o644)
            with self.assertRaises(ConfigError):
                load_sidecar_webdav_settings(config, _no_keychain)
            os.chmod(pw, 0o600)
            self.assertEqual(load_sidecar_webdav_settings(config, _no_keychain).password, "from-file")

    def test_plaintext_password_rejected(self):
        with TemporaryDirectory() as tmp:
            config = _write_json(
                Path(tmp) / "config.json",
                {"webdav": {"base_url": "https://nas.example/dav", "password": "plain"}},
            )
            with self.assertRaises(ConfigError) as ctx:
                load_sidecar_webdav_settings(config, _no_keychain)
            self.assertIn("plaintext", str(ctx.exception))

    def test_config_path_env_override(self):
        with mock.patch.dict(os.environ, {"SKILL_SYNC_CONFIG": "/tmp/x/config.json"}):
            self.assertEqual(default_config_path(), Path("/tmp/x/config.json"))


class LoadWebdavSettingsTest(unittest.TestCase):
    def test_prefers_sidecar_config(self):
        with TemporaryDirectory() as tmp:
            config = _write_json(Path(tmp) / "config.json", {"webdav": {"base_url": "https://nas.example/dav"}})
            cc = _write_json(Path(tmp) / "cc.json", CC_SWITCH_SETTINGS)
            warnings = []
            settings = load_webdav_settings(config, cc, _no_keychain, warnings.append)
            self.assertEqual(settings.base_url, "https://nas.example/dav")
            self.assertEqual(warnings, [])

    def test_falls_back_to_cc_switch_with_warning(self):
        with TemporaryDirectory() as tmp:
            cc = _write_json(Path(tmp) / "cc.json", CC_SWITCH_SETTINGS)
            warnings = []
            settings = load_webdav_settings(Path(tmp) / "missing.json", cc, _no_keychain, warnings.append)
            self.assertEqual(settings.base_url, "https://cc.example/dav")
            self.assertEqual(settings.password, "cc-secret-123")
            self.assertEqual(len(warnings), 1)
            self.assertIn("config-init", warnings[0])

    def test_broken_sidecar_config_does_not_fall_back(self):
        # 写错的配置若静默回退到 cc-switch，就永远不会被发现
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text("{not json", encoding="utf-8")
            cc = _write_json(Path(tmp) / "cc.json", CC_SWITCH_SETTINGS)
            with self.assertRaises(ConfigError):
                load_webdav_settings(config, cc, _no_keychain, lambda msg: None)


class InitConfigTest(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "sub" / "config.json"
            cc = _write_json(Path(tmp) / "cc.json", CC_SWITCH_SETTINGS)

            def writer(*args):
                raise AssertionError("dry run must not touch Keychain")

            plan = init_config_from_cc_switch(config, cc, keychain_writer=writer, dry_run=True)

            self.assertFalse(config.exists())
            self.assertTrue(plan["dry_run"])
            self.assertNotIn("cc-secret-123", json.dumps(plan))

    def test_yes_writes_keychain_and_private_config(self):
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "sub" / "config.json"
            cc = _write_json(Path(tmp) / "cc.json", CC_SWITCH_SETTINGS)
            stored = {}

            def writer(service, account, password):
                stored[(service, account)] = password

            init_config_from_cc_switch(config, cc, keychain_writer=writer, dry_run=False)

            self.assertEqual(stored, {("skill-sync-sidecar-webdav", "u"): "cc-secret-123"})
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            written = json.loads(config.read_text(encoding="utf-8"))
            self.assertNotIn("password", written["webdav"])
            self.assertEqual(written["webdav"]["base_url"], "https://cc.example/dav")
            # 写出来的配置要能被读回，且读到的就是存进 Keychain 的那份密码
            settings = load_sidecar_webdav_settings(config, keychain_reader=lambda s, a: stored[(s, a)])
            self.assertEqual(settings.password, "cc-secret-123")

    def test_refuses_to_overwrite_existing_config(self):
        with TemporaryDirectory() as tmp:
            config = _write_json(Path(tmp) / "config.json", {"webdav": {"base_url": "https://keep.example/dav"}})
            cc = _write_json(Path(tmp) / "cc.json", CC_SWITCH_SETTINGS)
            with self.assertRaises(ConfigError):
                init_config_from_cc_switch(config, cc, keychain_writer=lambda *a: None, dry_run=False)
            self.assertIn("keep.example", config.read_text(encoding="utf-8"))


class CliWebdavConfigTest(unittest.TestCase):
    def test_flag_accepted_and_exclusive_with_legacy(self):
        parser = cli.build_parser()
        args = parser.parse_args(["sync-daemon", "--local-root", "/x", "--cache-dir", "/c", "--work-dir", "/w", "--webdav-config", "--prefix", "p", "--dry-run"])
        self.assertTrue(args.webdav_config)
        with self.assertRaises(SystemExit):
            parser.parse_args(["remote-status", "--webdav-config", "--cc-switch-webdav"])

    def test_open_remote_uses_sidecar_config(self):
        with TemporaryDirectory() as tmp:
            config = _write_json(
                Path(tmp) / "config.json",
                {"webdav": {"base_url": "https://nas.example/dav", "username": "me",
                            "password_source": {"env": "T0260_PW"}}},
            )
            args = Namespace(webdav_config=True, cc_switch_webdav=False, remote=None, prefix="skill-sync-sidecar-dev/t",
                             username_env="SKILL_SYNC_WEBDAV_USER", password_env="SKILL_SYNC_WEBDAV_PASSWORD")
            with mock.patch.dict(os.environ, {"SKILL_SYNC_CONFIG": str(config), "T0260_PW": "pw"}):
                with mock.patch.object(cli, "open_remote") as opener:
                    cli.open_remote_from_args(args)
                    cli.guard_http_upload(args)
            opener.assert_called_once_with("https://nas.example/dav", "SKILL_SYNC_WEBDAV_USER", "SKILL_SYNC_WEBDAV_PASSWORD",
                                           username="me", password="pw")
            self.assertEqual(cli._remote_label(args), "sidecar-webdav-config")


if __name__ == "__main__":
    unittest.main()
