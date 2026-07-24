import json
import os
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

    def test_openclaw_restore_from_central_is_gated_and_non_destructive(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "openclaw-restore-from-central.sh"
        text = script.read_text(encoding="utf-8")

        subprocess.check_call(["bash", "-n", str(script)])
        help_text = subprocess.check_output(["bash", str(script), "--help"], text=True)

        self.assertIn("--dry-run", help_text)
        self.assertIn("--yes", help_text)
        self.assertIn("Restore selected skills", help_text)
        self.assertIn("pull-cache", text)
        self.assertIn("stage", text)
        self.assertIn("apply", text)
        self.assertIn("--target", text)
        self.assertIn("mixed-scope-root", text)
        self.assertIn("--target-root", text)
        self.assertIn("/home/admin/clawd/skills", text)
        self.assertNotIn("systemctl", text)
        self.assertNotIn("rm -rf /home/admin/clawd/skills", text)

    def test_openclaw_conflict_package_is_read_only(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "openclaw-conflict-package.sh"
        text = script.read_text(encoding="utf-8")

        subprocess.check_call(["bash", "-n", str(script)])
        help_text = subprocess.check_output(["bash", str(script), "--help"], text=True)

        self.assertIn("Generate read-only conflict", help_text)
        self.assertIn("pull-cache", text)
        self.assertIn("conflict-package", text)
        self.assertIn("--local-root", text)
        self.assertIn("/home/admin/clawd/skills", text)
        self.assertIn("--remote-snapshot", text)
        self.assertIn("--last-applied-record", text)
        self.assertNotIn("approved-push", text)
        self.assertNotIn("sync-apply", text)
        self.assertNotIn("systemctl", text)
        self.assertNotIn("rm -rf /home/admin/clawd/skills", text)

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
        self.assertIn("SKILL_SYNC_TARGET:-mixed-scope-root", installer)
        self.assertIn("SYNC_TARGET", installer)
        self.assertIn("SKILL_SYNC_CONTINUE_ON_BLOCKED:-1", installer)
        self.assertIn("--continue-on-blocked", installer)

    def test_openclaw_peer_status_refresh_scripts_are_read_only(self):
        repo_root = Path(__file__).resolve().parents[1]
        refresh = repo_root / "scripts" / "refresh-openclaw-peer-status.sh"
        publish = repo_root / "scripts" / "publish-openclaw-peer-status.sh"
        installer = repo_root / "scripts" / "install-openclaw-peer-status-launchd.sh"
        local_publish = repo_root / "scripts" / "publish-openclaw-local-peer-status.sh"
        systemd_installer = repo_root / "scripts" / "install-openclaw-peer-status-systemd.sh"
        refresh_text = refresh.read_text(encoding="utf-8")
        publish_text = publish.read_text(encoding="utf-8")
        installer_text = installer.read_text(encoding="utf-8")
        local_publish_text = local_publish.read_text(encoding="utf-8")
        systemd_installer_text = systemd_installer.read_text(encoding="utf-8")

        subprocess.check_call(["bash", "-n", str(refresh)])
        subprocess.check_call(["bash", "-n", str(publish)])
        subprocess.check_call(["bash", "-n", str(installer)])
        subprocess.check_call(["bash", "-n", str(local_publish)])
        subprocess.check_call(["bash", "-n", str(systemd_installer)])
        systemd_help = subprocess.check_output(["bash", str(systemd_installer), "--help"], text=True)
        systemd_dry_run = subprocess.check_output(["bash", str(systemd_installer), "--dry-run"], text=True)

        self.assertIn("ssh", refresh_text)
        self.assertIn("ops-status", refresh_text)
        self.assertIn("mktemp", refresh_text)
        self.assertIn("trap 'rm -f", refresh_text)
        self.assertIn("--writer-policy pull-only", refresh_text)
        self.assertIn("peer_status_version", refresh_text)
        self.assertIn("build_device_tool_status", refresh_text)
        self.assertIn("publishing legacy ops status", refresh_text)
        self.assertIn("peer status JSON does not contain health", refresh_text)
        self.assertNotIn("--blocked-report", refresh_text)
        self.assertNotIn("sync-apply", refresh_text)
        self.assertNotIn("sync-cycle", refresh_text)
        self.assertNotIn("systemctl", refresh_text)

        self.assertIn("refresh-openclaw-peer-status.sh", publish_text)
        self.assertIn("publish-peer-status", publish_text)
        self.assertIn("--status-file", publish_text)
        self.assertNotIn("sync-apply", publish_text)
        self.assertNotIn("sync-cycle", publish_text)

        self.assertIn("publish-openclaw-peer-status.sh", installer_text)
        self.assertIn("StartInterval", installer_text)
        self.assertIn("com.skill-sync-sidecar.openclaw-peer-status", installer_text)
        self.assertIn("launchctl bootstrap", installer_text)

        self.assertIn("ops-status", local_publish_text)
        self.assertIn("--writer-policy pull-only", local_publish_text)
        self.assertIn("/home/admin/clawd/skills", local_publish_text)
        self.assertIn("OpenClaw 实际使用目录", local_publish_text)
        self.assertIn("publish-peer-status", local_publish_text)
        self.assertIn("--status-file", local_publish_text)
        self.assertNotIn("ssh", local_publish_text)
        self.assertNotIn("sync-apply", local_publish_text)
        self.assertNotIn("sync-cycle", local_publish_text)
        self.assertNotIn("systemctl", local_publish_text)

        self.assertIn('mode="--dry-run"', systemd_installer_text)
        self.assertIn("openclaw-skill-sync-peer-status.service", systemd_installer_text)
        self.assertIn("openclaw-skill-sync-peer-status.timer", systemd_installer_text)
        self.assertIn("publish-openclaw-local-peer-status.sh", systemd_installer_text)
        self.assertIn("systemctl enable --now openclaw-skill-sync-peer-status.timer", systemd_installer_text)
        self.assertIn("status openclaw-skill-sync-peer-status.service | sed -n '1,30p' || true", systemd_installer_text)
        self.assertNotIn("openclaw-skill-sync-sidecar-pullonly.service", systemd_installer_text)
        self.assertNotIn("openclaw-skill-sync-sidecar-dryrun.service", systemd_installer_text)
        self.assertNotIn("openclaw-gateway", systemd_installer_text)
        self.assertIn("peer-status publisher", systemd_help)
        self.assertIn("openclaw_peer_status_systemd_mode=dry-run", systemd_dry_run)
        self.assertIn("OnUnitActiveSec=300", systemd_dry_run)

    def test_ops_watch_is_observation_only(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "ops-watch.sh"
        text = script.read_text(encoding="utf-8")

        subprocess.check_call(["bash", "-n", str(script)])

        self.assertIn("monitor-summary", text)
        self.assertIn("launchctl print", text)
        self.assertIn("skill-sync-mac-peer-status.out.log", text)
        self.assertIn("skill-sync-openclaw-peer-status.out.log", text)
        self.assertNotIn("approved-push", text)
        self.assertNotIn("sync-cycle", text)
        self.assertNotIn("sync-apply", text)
        self.assertNotIn("launchctl bootstrap", text)
        self.assertNotIn("launchctl kickstart", text)

    def test_operator_status_is_brief_and_read_only(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "operator-status.sh"
        text = script.read_text(encoding="utf-8")

        subprocess.check_call(["bash", "-n", str(script)])

        self.assertIn("monitor-summary", text)
        self.assertIn("--brief", text)
        self.assertIn("/api/overview", text)
        self.assertNotIn("approved-push", text)
        self.assertNotIn("sync-cycle", text)
        self.assertNotIn("sync-apply", text)
        self.assertNotIn("push --yes", text)

    def test_nas_gateway_deploy_verifier_is_read_only(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "verify-nas-gateway-deploy.sh"
        text = script.read_text(encoding="utf-8")

        subprocess.check_call(["bash", "-n", str(script)])
        self.assertIn("deployed-commit.txt", text)
        self.assertIn("/healthz", text)
        self.assertIn("/api/overview", text)
        self.assertIn("last-report.json", text)
        self.assertIn("普通待审", text)
        self.assertIn(".simple-action-panel", text)
        self.assertNotIn(" compose ", text)
        self.assertNotIn(" up -d", text)
        self.assertNotIn("docker restart", text)
        self.assertNotIn("docker rm", text)
        self.assertNotIn("sync-cycle", text)
        self.assertNotIn("sync-apply", text)

    def test_blocked_queue_script_is_read_only_and_renders_items(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "blocked-queue.sh"
        text = script.read_text(encoding="utf-8")

        subprocess.check_call(["bash", "-n", str(script)])
        self.assertIn("/api/overview", text)
        self.assertIn("blocked_items", text)
        self.assertNotIn("approved-push --yes", text)
        self.assertNotIn("sync-cycle", text)
        self.assertNotIn("sync-apply", text)

        with TemporaryDirectory() as tmp:
            summary = Path(tmp) / "summary.json"
            summary.write_text(
                json.dumps(
                    {
                        "health": "yellow",
                        "remote_snapshot": {"snapshot_id": "snap-queue", "total": 96},
                        "dashboard": {
                            "health": "yellow",
                            "blocked_items": [
                                {
                                    "peer_name": "oc-vps / OpenClaw",
                                    "skill_id": "hebei-recruitment",
                                    "status_action": "push",
                                    "plan_action": "blocked",
                                    "category": "writer_policy",
                                    "reason": "writer policy pull-only blocks push",
                                    "base_hash": "base",
                                    "local_hash": "local",
                                    "remote_hash": "remote",
                                    "recommendation": "Review before approved-push.",
                                    "source": "blocked_report",
                                }
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )

            output = subprocess.check_output(
                ["bash", str(script)],
                env={**os.environ, "SKILL_SYNC_BLOCKED_QUEUE_SUMMARY_FILE": str(summary)},
                text=True,
            )

        self.assertIn("blocked: 1", output)
        self.assertIn("hebei-recruitment", output)
        self.assertIn("local_hash: local", output)

    def test_openclaw_approved_push_batch_is_dry_run_first(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "openclaw-approved-push-batch.sh"
        text = script.read_text(encoding="utf-8")

        subprocess.check_call(["bash", "-n", str(script)])
        help_text = subprocess.check_output(["bash", str(script), "--help"], text=True)

        self.assertIn('mode="--dry-run"', text)
        self.assertIn("--yes", help_text)
        self.assertIn("approved-push", text)
        self.assertIn("sync-cycle", text)
        self.assertIn("pull-cache", text)
        self.assertIn("--writer-policy", text)
        self.assertIn("pull-only", text)
        self.assertIn("--allow-new", text)
        self.assertIn("--allow-conflict-local-wins", help_text)
        self.assertIn("allow_conflict_local_wins", text)
        self.assertIn("SKILL_ID", help_text)
        self.assertNotIn("systemctl", text)
        self.assertNotIn("launchctl", text)

    def test_approved_push_runbook_documents_safe_flow(self):
        repo_root = Path(__file__).resolve().parents[1]
        runbook = repo_root / "docs" / "approved-push-runbook.md"
        text = runbook.read_text(encoding="utf-8")

        self.assertIn("pull-only", text)
        self.assertIn("scripts/openclaw-approved-push-batch.sh", text)
        self.assertIn("--yes", text)
        self.assertIn("scripts/publish-openclaw-peer-status.sh", text)
        self.assertIn("scripts/publish-mac-peer-status.sh", text)
        self.assertIn("monitor-summary", text)
        self.assertIn("blocked: 0", text)
        self.assertIn("Do not switch OpenClaw to `push-pull`", text)


if __name__ == "__main__":
    unittest.main()
