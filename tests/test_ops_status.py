import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from skill_sync_sidecar.ops_status import build_ops_status, reconcile_summary, render_ops_status_text
from skill_sync_sidecar.scanner import scan_roots
from skill_sync_sidecar.snapshot import write_snapshot


class OpsStatusTest(unittest.TestCase):
    def test_build_ops_status_summarizes_clean_sync_state(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_root = root / "skills"
            snapshot_dir = root / "remote"
            base_record = root / "base-record.json"
            state_file = root / "state.json"

            self._write_skill(local_root / "demo", "Demo", "Demo skill")
            index = write_snapshot(scan_roots([f"cc-switch={local_root}"]), snapshot_dir, "snap-1")
            self._write_base_record(base_record, index)
            state_file.write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "daemon_status": "running",
                        "updated_at": "2026-06-14T00:00:00Z",
                        "cycles_run": 3,
                        "current_base_record": str(base_record),
                        "cycles": [
                            {
                                "status": "complete",
                                "reason": "sync actions applied",
                                "snapshot_id": "snap-1",
                                "summary": {"noop": 1},
                                "blocked": 0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            status = build_ops_status(local_root, snapshot_dir, base_record=base_record, state_file=state_file)

            self.assertTrue(status["ok"])
            self.assertEqual(status["remote_snapshot"]["snapshot_id"], "snap-1")
            self.assertEqual(status["remote_snapshot"]["total"], 1)
            self.assertEqual(status["base_record"]["applied_count"], 1)
            self.assertEqual(status["daemon_state"]["cycles_run"], 3)
            self.assertEqual(status["sync_plan"]["summary"], {"noop": 1})
            self.assertTrue(status["sync_plan"]["safe_to_apply"])

    def test_reconcile_summary_extracts_openclaw_adoption_signals(self):
        with TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "reconcile-report.json"
            report_path.write_text(
                json.dumps(
                    {
                        "label": "after-drift",
                        "local_total": 92,
                        "remote_total": 92,
                        "safe_to_auto_apply": True,
                        "summary": {"same_without_base": 32, "remote_new": 60},
                        "changed_since_previous": {"changed_count": 0, "changed": []},
                    }
                ),
                encoding="utf-8",
            )

            summary = reconcile_summary(report_path)

            self.assertTrue(summary["ok"])
            self.assertTrue(summary["safe_to_auto_apply"])
            self.assertEqual(summary["summary"]["remote_new"], 60)
            self.assertEqual(summary["changed_since_previous"]["changed_count"], 0)

    def test_build_ops_status_finds_latest_openclaw_reconcile_report(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_root = root / "skills"
            snapshot_dir = root / "remote"
            base_record = root / "base-record.json"
            reconcile_report = root / "openclaw" / "run" / "reconcile" / "reconcile-report.json"

            self._write_skill(local_root / "demo", "Demo", "Demo skill")
            index = write_snapshot(scan_roots([f"cc-switch={local_root}"]), snapshot_dir, "snap-1")
            self._write_base_record(base_record, index)
            reconcile_report.parent.mkdir(parents=True)
            reconcile_report.write_text(
                json.dumps(
                    {
                        "label": "openclaw-latest",
                        "created_at": "2026-06-14T00:00:00+00:00",
                        "local_total": 1,
                        "remote_total": 1,
                        "safe_to_auto_apply": True,
                        "summary": {"same_without_base": 1},
                        "changed_since_previous": {"changed_count": 0, "changed": []},
                    }
                ),
                encoding="utf-8",
            )

            status = build_ops_status(
                local_root,
                snapshot_dir,
                base_record=base_record,
                openclaw_reconcile_root=root / "openclaw",
            )

            self.assertTrue(status["openclaw_gate"]["ok"])
            self.assertEqual(status["openclaw_gate"]["selected_by"], "latest")
            self.assertEqual(status["openclaw_reconcile"]["label"], "openclaw-latest")

    def test_blocked_openclaw_gate_is_not_counted_as_artifact_error(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_root = root / "skills"
            snapshot_dir = root / "remote"
            base_record = root / "base-record.json"
            reconcile_report = root / "openclaw" / "run" / "reconcile" / "reconcile-report.json"

            self._write_skill(local_root / "demo", "Demo", "Demo skill")
            index = write_snapshot(scan_roots([f"cc-switch={local_root}"]), snapshot_dir, "snap-1")
            self._write_base_record(base_record, index)
            reconcile_report.parent.mkdir(parents=True)
            reconcile_report.write_text(
                json.dumps(
                    {
                        "label": "openclaw-blocked",
                        "created_at": "2026-06-14T00:00:00+00:00",
                        "local_total": 1,
                        "remote_total": 1,
                        "safe_to_auto_apply": False,
                        "summary": {"conflict": 1},
                        "changed_since_previous": {"changed_count": 1, "changed": ["demo"]},
                    }
                ),
                encoding="utf-8",
            )

            status = build_ops_status(
                local_root,
                snapshot_dir,
                base_record=base_record,
                openclaw_reconcile_root=root / "openclaw",
            )

            self.assertFalse(status["ok"])
            self.assertEqual(status["error_count"], 0)
            self.assertIn("conflict=1", status["openclaw_gate"]["blockers"])

    def test_render_ops_status_text_includes_key_operational_lines(self):
        status = {
            "ok": True,
            "local_root": "/tmp/skills",
            "remote_snapshot": {"ok": True, "snapshot_id": "snap-1", "total": 1, "created_at": "now"},
            "base_record": {"ok": True, "sync_id": "base-1", "snapshot_id": "snap-1", "applied_count": 1},
            "daemon_state": {
                "ok": True,
                "daemon_status": "running",
                "cycles_run": 2,
                "updated_at": "now",
                "last_cycle": {"status": "complete", "snapshot_id": "snap-1", "blocked": 0, "summary": {"noop": 1}},
            },
            "sync_plan": {
                "ok": True,
                "safe_to_apply": True,
                "blocked": 0,
                "allowed": 1,
                "summary": {"noop": 1},
                "status_summary": {"unchanged": 1},
            },
            "openclaw_reconcile": {
                "ok": True,
                "safe_to_auto_apply": True,
                "local_total": 92,
                "remote_total": 92,
                "summary": {"same_without_base": 32},
                "changed_since_previous": {"changed_count": 0},
            },
            "openclaw_gate": {"available": True, "ok": True, "blockers": [], "selected_by": "latest"},
        }

        text = render_ops_status_text(status)

        self.assertIn("remote_snapshot: snap-1 total=1", text)
        self.assertIn("sync_plan: safe_to_apply=True blocked=0", text)
        self.assertIn("openclaw_reconcile: safe_to_auto_apply=True", text)
        self.assertIn("openclaw_gate: ok=True", text)
        self.assertIn("overall_ok: True", text)

    def _write_skill(self, skill: Path, name: str, description: str):
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n",
            encoding="utf-8",
        )

    def _write_base_record(self, record_path: Path, index: dict):
        record_path.write_text(
            json.dumps(
                {
                    "protocol_version": 0,
                    "record_type": "skill-sync-base",
                    "sync_id": "base-1",
                    "created_at": "2026-06-14T00:00:00Z",
                    "snapshot_id": index["snapshot_id"],
                    "applied": [
                        {
                            "skill_id": skill["skill_id"],
                            "content_hash": skill["content_hash"],
                        }
                        for skill in index["skills"]
                    ],
                }
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
