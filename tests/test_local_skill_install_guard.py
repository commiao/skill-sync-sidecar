import contextlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from skill_sync_sidecar import cli


class _FakeInstall:
    """替身：记下调用，不写任何目录。闸被改坏时用例也不会真的装进本机工具。"""

    def __init__(self):
        self.calls = []

    def __call__(self, path, *, tool_ids, yes, allow_local_writes):
        self.calls.append({"path": path, "yes": yes})
        return {"mode": "install" if yes else "dry-run", "skill_id": "demo", "summary": {"will_write": 0}, "items": []}


class LocalSkillInstallGuardTest(unittest.TestCase):
    def _run(self, root: Path, *extra: str):
        fake = _FakeInstall()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "CODE_ROOT", root), mock.patch.object(cli, "install_local_skill", fake):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main(["local-skill-install", "--path", str(root / "demo"), *extra])
        return rc, fake.calls, out.getvalue(), err.getvalue()

    def test_code_root_is_the_package_checkout(self):
        self.assertTrue((cli.CODE_ROOT / "src" / "skill_sync_sidecar" / "cli.py").is_file())

    def test_yes_from_dev_tree_is_refused(self):
        with TemporaryDirectory() as tmp:
            rc, calls, _, err = self._run(Path(tmp), "--yes")
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])
        self.assertIn("local-skill-install refused: running from a development checkout", err)

    def test_yes_from_dev_tree_with_override_installs(self):
        with TemporaryDirectory() as tmp:
            rc, calls, out, _ = self._run(Path(tmp), "--yes", "--allow-dev-tree")
        self.assertEqual(rc, 0)
        self.assertEqual([call["yes"] for call in calls], [True])
        self.assertIn("code: development checkout", out)

    def test_yes_from_release_installs_and_names_the_commit(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "RELEASE.json").write_text(json.dumps({"commit": "426d4927ec31d8ea14d11506b9bc3f4bc19bf0de"}), encoding="utf-8")
            rc, calls, out, _ = self._run(root, "--yes")
        self.assertEqual(rc, 0)
        self.assertEqual([call["yes"] for call in calls], [True])
        self.assertIn("code: release 426d4927ec31 at", out)

    def test_unreadable_release_meta_counts_as_dev_tree(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "RELEASE.json").write_text("not json", encoding="utf-8")
            rc, calls, _, _ = self._run(root, "--yes")
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])

    def test_dry_run_from_dev_tree_still_runs_and_says_where_it_runs(self):
        with TemporaryDirectory() as tmp:
            rc, calls, out, _ = self._run(Path(tmp), "--dry-run")
        self.assertEqual(rc, 0)
        self.assertEqual([call["yes"] for call in calls], [False])
        self.assertIn("code: development checkout", out)


if __name__ == "__main__":
    unittest.main()
