import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from skill_sync_sidecar.scanner import scan_roots
from skill_sync_sidecar.snapshot import write_snapshot


class OpenClawSyncProbeScriptTest(unittest.TestCase):
    def test_py36_probe_can_live_apply_sync_probe_to_target_root(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "openclaw-sync-probe-py36.py"

        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            source_root = base / "source"
            skill_dir = source_root / "sync-probe"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: sync-probe\ndescription: Probe skill\n---\n\nbody\n",
                encoding="utf-8",
            )
            (skill_dir / "notes.txt").write_text("hello\n", encoding="utf-8")

            remote_root = base / "remote"
            snapshot_dir = remote_root / "prefix"
            write_snapshot(scan_roots([f"cc-switch={source_root}"]), snapshot_dir, label="probe-test")

            settings = base / "settings.json"
            settings.write_text(
                json.dumps({"webdavSync": {"baseUrl": remote_root.as_uri() + "/"}}),
                encoding="utf-8",
            )

            apply_root = base / "target"
            apply_root.mkdir()
            report_path = base / "report.json"

            with report_path.open("w", encoding="utf-8") as handle:
                subprocess.check_call(
                    [
                        sys.executable,
                        str(script),
                        "--settings",
                        str(settings),
                        "--prefix",
                        "prefix",
                        "--out",
                        str(base / "out"),
                        "--apply-root",
                        str(apply_root),
                        "--yes-apply",
                    ],
                    stdout=handle,
                )

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(report["ok"])
            self.assertEqual(report["skill_id"], "sync-probe")
            self.assertEqual(report["actual_hash"], report["content_hash"])
            self.assertEqual(report["apply_result"]["actual_hash"], report["content_hash"])
            self.assertFalse(report["apply_result"]["previous_exists"])
            self.assertTrue((apply_root / "sync-probe" / "SKILL.md").is_file())
            self.assertTrue(Path(report["apply_result"]["apply_record"]).is_file())


if __name__ == "__main__":
    unittest.main()
