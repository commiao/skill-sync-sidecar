import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from skill_sync_sidecar.scanner import scan_roots
from skill_sync_sidecar.snapshot import write_snapshot


class AdmissionScriptsTest(unittest.TestCase):
    def test_filter_snapshot_and_admission_report(self):
        repo_root = Path(__file__).resolve().parents[1]
        filter_script = repo_root / "scripts" / "filter-snapshot.py"
        admission_script = repo_root / "scripts" / "openclaw-admission-report.py"

        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            source_root = base / "source"
            for name in ["small-one", "big-browser"]:
                skill = source_root / name
                skill.mkdir(parents=True)
                skill.joinpath("SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: {name} browser deploy\n---\n\nbody\n",
                    encoding="utf-8",
                )
            snapshot = base / "snapshot"
            write_snapshot(scan_roots([f"cc-switch={source_root}"]), snapshot, label="source-snapshot")

            filtered = base / "filtered"
            subprocess.check_call(
                [
                    sys.executable,
                    str(filter_script),
                    "--source",
                    str(snapshot),
                    "--out",
                    str(filtered),
                    "--label",
                    "filtered-snapshot",
                    "--skill-id",
                    "small-one",
                ]
            )
            filtered_index = json.loads((filtered / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(filtered_index["total"], 1)
            self.assertEqual(filtered_index["skills"][0]["skill_id"], "small-one")

            reconcile = base / "reconcile.json"
            reconcile.write_text(
                json.dumps(
                    {
                        "items": [
                            {"skill_id": "small-one", "status": "remote_new"},
                            {"skill_id": "big-browser", "status": "remote_new"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            out_md = base / "admission.md"
            out_json = base / "admission.json"
            subprocess.check_call(
                [
                    sys.executable,
                    str(admission_script),
                    "--reconcile-report",
                    str(reconcile),
                    "--remote-index",
                    str(snapshot / "index.json"),
                    "--out-md",
                    str(out_md),
                    "--out-json",
                    str(out_json),
                ]
            )
            report = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(report["remote_new_total"], 2)
            self.assertEqual({row["skill_id"] for row in report["rows"]}, {"small-one", "big-browser"})
            self.assertTrue(out_md.read_text(encoding="utf-8").startswith("# OpenClaw Admission Report"))

    def test_openclaw_writable_rehearsal_is_gated_and_finite(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "openclaw-writable-rehearsal.sh"
        text = script.read_text(encoding="utf-8")

        subprocess.check_call(["bash", "-n", str(script)])
        help_text = subprocess.check_output(["bash", str(script), "--help"], text=True)

        self.assertIn("openclaw-gate", text)
        self.assertIn("--require-complete", text)
        self.assertIn("--fail-on-blocked", text)
        self.assertIn("sync-daemon", text)
        self.assertIn("--yes", text)
        self.assertIn("--max-cycles 1", text)
        self.assertIn("--interval-seconds 0", text)
        self.assertIn("--writer-policy", text)
        self.assertIn("SKILL_SYNC_WRITER_POLICY:-pull-only", text)
        self.assertNotIn("systemctl", text)
        self.assertNotIn("service", text.lower().replace("long-running service", ""))
        self.assertIn("one-cycle writable", help_text)

    def test_service_templates_make_writer_policy_explicit(self):
        repo_root = Path(__file__).resolve().parents[1]
        openclaw_unit = (repo_root / "examples" / "systemd" / "openclaw-skill-sync-sidecar-dryrun.service").read_text(encoding="utf-8")
        generic_unit = (repo_root / "examples" / "systemd" / "skill-sync-sidecar.service").read_text(encoding="utf-8")
        launchd = (repo_root / "examples" / "launchd" / "com.skill-sync-sidecar.plist").read_text(encoding="utf-8")
        installer = (repo_root / "scripts" / "install-current-launchd.sh").read_text(encoding="utf-8")

        self.assertIn("--writer-policy pull-only", openclaw_unit)
        self.assertIn("--writer-policy push-pull", generic_unit)
        self.assertIn("<string>--writer-policy</string>", launchd)
        self.assertIn("<string>push-pull</string>", launchd)
        self.assertIn("SKILL_SYNC_WRITER_POLICY:-push-pull", installer)
        self.assertIn("WRITER_POLICY", installer)

    def test_openclaw_peer_status_refresh_scripts_are_read_only(self):
        repo_root = Path(__file__).resolve().parents[1]
        refresh = repo_root / "scripts" / "refresh-openclaw-peer-status.sh"
        installer = repo_root / "scripts" / "install-openclaw-peer-status-launchd.sh"
        refresh_text = refresh.read_text(encoding="utf-8")
        installer_text = installer.read_text(encoding="utf-8")

        subprocess.check_call(["bash", "-n", str(refresh)])
        subprocess.check_call(["bash", "-n", str(installer)])

        self.assertIn("ssh", refresh_text)
        self.assertIn("ops-status", refresh_text)
        self.assertIn("--writer-policy pull-only", refresh_text)
        self.assertIn("peer status JSON does not contain health", refresh_text)
        self.assertNotIn("sync-apply", refresh_text)
        self.assertNotIn("sync-cycle", refresh_text)
        self.assertNotIn("systemctl", refresh_text)

        self.assertIn("refresh-openclaw-peer-status.sh", installer_text)
        self.assertIn("StartInterval", installer_text)
        self.assertIn("com.skill-sync-sidecar.openclaw-peer-status", installer_text)
        self.assertIn("launchctl bootstrap", installer_text)


if __name__ == "__main__":
    unittest.main()
