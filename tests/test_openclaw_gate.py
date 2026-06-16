import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional
import unittest

from skill_sync_sidecar.openclaw_gate import build_openclaw_gate, render_openclaw_gate_text, select_latest_reconcile_report


class OpenClawGateTest(unittest.TestCase):
    def test_select_latest_reconcile_report_uses_report_created_at(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_report = root / "old" / "reconcile" / "reconcile-report.json"
            new_report = root / "new" / "reconcile" / "reconcile-report.json"
            self._write_report(old_report, created_at="2026-06-13T00:00:00+00:00")
            self._write_report(new_report, created_at="2026-06-14T00:00:00+00:00")

            selected = select_latest_reconcile_report(root)

            self.assertEqual(selected, new_report)

    def test_gate_allows_clean_peer_writer_report(self):
        with TemporaryDirectory() as tmp:
            report = Path(tmp) / "reconcile-report.json"
            self._write_report(
                report,
                safe_to_auto_apply=True,
                summary={"same_without_base": 32, "remote_new": 60},
                changed_since_previous={"changed_count": 0, "changed": []},
            )

            gate = build_openclaw_gate(report_path=report)

            self.assertTrue(gate["ok"])
            self.assertEqual(gate["blockers"], [])
            self.assertEqual(gate["changed_count"], 0)

    def test_gate_require_complete_blocks_unreviewed_remote_new(self):
        with TemporaryDirectory() as tmp:
            report = Path(tmp) / "reconcile-report.json"
            self._write_report(
                report,
                safe_to_auto_apply=True,
                summary={"same_without_base": 90, "remote_new": 2},
                changed_since_previous={"changed_count": 0, "changed": []},
            )

            gate = build_openclaw_gate(report_path=report, require_complete=True)

            self.assertFalse(gate["ok"])
            self.assertTrue(gate["require_complete"])
            self.assertIn("remote_new=2", gate["blockers"])

    def test_gate_require_complete_allows_fully_admitted_report(self):
        with TemporaryDirectory() as tmp:
            report = Path(tmp) / "reconcile-report.json"
            self._write_report(
                report,
                safe_to_auto_apply=True,
                summary={"same_without_base": 92},
                changed_since_previous={"changed_count": 0, "changed": []},
            )

            gate = build_openclaw_gate(report_path=report, require_complete=True)

            self.assertTrue(gate["ok"])
            self.assertEqual(gate["blockers"], [])

    def test_gate_blocks_conflict_local_new_and_fresh_drift(self):
        with TemporaryDirectory() as tmp:
            report = Path(tmp) / "reconcile-report.json"
            self._write_report(
                report,
                safe_to_auto_apply=False,
                summary={"conflict": 2, "local_new": 1, "remote_new": 60},
                changed_since_previous={"changed_count": 3, "changed": ["a", "b", "c"]},
            )

            gate = build_openclaw_gate(report_path=report)

            self.assertFalse(gate["ok"])
            self.assertIn("safe_to_auto_apply=false", gate["blockers"])
            self.assertIn("conflict=2", gate["blockers"])
            self.assertIn("local_new=1", gate["blockers"])
            self.assertIn("changed_since_previous=3", gate["blockers"])

    def test_render_gate_text_surfaces_blockers(self):
        text = render_openclaw_gate_text(
            {
                "available": True,
                "ok": False,
                "safe_to_auto_apply": False,
                "path": "/tmp/report.json",
                "summary": {"conflict": 1},
                "changed_count": 0,
                "blockers": ["conflict=1"],
            }
        )

        self.assertIn("openclaw_gate: ok=False", text)
        self.assertIn("require_complete: False", text)
        self.assertIn("blockers: conflict=1", text)

    def _write_report(
        self,
        path: Path,
        created_at: str = "2026-06-14T00:00:00+00:00",
        safe_to_auto_apply: bool = True,
        summary: Optional[dict] = None,
        changed_since_previous: Optional[dict] = None,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "report_type": "skill-sync-reconcile",
                    "label": "openclaw-test",
                    "created_at": created_at,
                    "local_total": 32,
                    "remote_total": 92,
                    "safe_to_auto_apply": safe_to_auto_apply,
                    "summary": summary or {"same_without_base": 32},
                    "changed_since_previous": changed_since_previous,
                    "items": [],
                }
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
