import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from skill_sync_sidecar.cli import build_parser
from skill_sync_sidecar.monitor import build_monitor_report, render_monitor_brief, render_monitor_report, run_monitor_loop, write_monitor_report


class MonitorSummaryTest(unittest.TestCase):
    def test_monitor_report_is_green_when_devices_are_fresh(self):
        report = build_monitor_report(_summary())

        self.assertTrue(report["ok"])
        self.assertEqual(report["health"], "green")
        self.assertEqual(report["alerts"], [])
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["info"][0]["code"], "all_clear")
        self.assertEqual(report["next_action"], "同步链路正常；继续观察 Mac / OpenClaw 自动周期。")
        self.assertIn("no operator action", render_monitor_report(report))
        self.assertIn("Skill Sync: GREEN - NO ACTION", render_monitor_brief(report))
        self.assertIn("deferred: win=本阶段跳过", render_monitor_brief(report))

    def test_monitor_report_lists_blocked_items_as_info_without_email_noise(self):
        summary = _summary(health="yellow", blocked=1)
        summary["dashboard"]["operator"]["top_issue"] = {
            "peer_id": "oc-vps",
            "peer_name": "oc-vps / OpenClaw",
            "skill_id": "hebei-recruitment",
            "status_action": "push",
            "category": "writer_policy",
            "reason": "writer policy pull-only blocks push",
            "action": "先在 Mac 运行 OpenClaw approved-push dry-run 审核 hebei-recruitment，确认后再 --yes 发布。",
            "command": "scripts/openclaw-approved-push-batch.sh hebei-recruitment",
        }
        summary["dashboard"]["blocked_items"] = [
            {
                "peer_id": "oc-vps",
                "peer_name": "oc-vps / OpenClaw",
                "skill_id": "hebei-recruitment",
                "status_action": "push",
                "category": "writer_policy",
                "reason": "writer policy pull-only blocks push",
                "recommendation": "Review before approved-push.",
            }
        ]

        report = build_monitor_report(summary)

        self.assertTrue(report["ok"])
        self.assertEqual(report["health"], "green")
        self.assertEqual(report["blocked"], 1)
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["info"][0]["code"], "operator_top_issue")
        self.assertEqual(report["info"][0]["command"], "scripts/openclaw-approved-push-batch.sh hebei-recruitment")
        self.assertTrue(any(item["code"] == "blocked_item" for item in report["info"]))
        text = render_monitor_report(report)
        self.assertIn("hebei-recruitment", text)
        self.assertIn("approved-push", text)
        brief = render_monitor_brief(report)
        self.assertIn("Skill Sync: GREEN - TRACKING ONLY", brief)
        self.assertIn("top_issue: [operator_top_issue]", brief)
        self.assertIn("command: scripts/openclaw-approved-push-batch.sh hebei-recruitment", brief)

    def test_monitor_report_keeps_conflicts_as_alerts(self):
        summary = _summary(health="yellow", blocked=1)
        summary["dashboard"]["blocked_items"] = [
            {
                "peer_id": "mac",
                "peer_name": "Mac 本机",
                "skill_id": "demo-conflict",
                "status_action": "conflict",
                "category": "conflict",
                "reason": "conflict or unknown state requires manual resolution",
            }
        ]

        report = build_monitor_report(summary)

        self.assertFalse(report["ok"])
        self.assertEqual(report["health"], "red")
        self.assertTrue(any(item["code"] == "blocked_item" for item in report["alerts"]))

    def test_monitor_report_explains_delete_review_as_restore_first(self):
        summary = _summary(health="yellow", blocked=1)
        summary["dashboard"]["operator"]["top_issue"] = {
            "peer_id": "oc-vps",
            "peer_name": "oc-vps / OpenClaw",
            "skill_id": "session-knowledge-manager",
            "status_action": "local_deleted",
            "category": "delete_review",
            "reason": "local deletion requires --allow-delete before remote deletion",
            "action": "先处理 oc-vps / OpenClaw / session-knowledge-manager 缺失项；建议先从共享仓库找回，确认废弃时再单独删除。",
        }
        summary["dashboard"]["blocked_items"] = [
            {
                "peer_id": "oc-vps",
                "peer_name": "oc-vps / OpenClaw",
                "skill_id": "session-knowledge-manager",
                "status_action": "local_deleted",
                "category": "delete_review",
                "reason": "local deletion requires --allow-delete before remote deletion",
            }
        ]

        report = build_monitor_report(summary)

        self.assertFalse(report["ok"])
        self.assertEqual(report["health"], "red")
        actions = [item.get("action") for item in report["alerts"] if item.get("code") == "blocked_item"]
        self.assertTrue(any("先从共享仓库找回" in action for action in actions))
        self.assertFalse(any("Inspect the item" in action for action in actions))

    def test_monitor_report_detects_stale_and_snapshot_mismatch(self):
        summary = _summary()
        summary["dashboard"]["devices"][1]["freshness"] = {"state": "stale", "label": "2 小时前", "age_seconds": 7200}
        summary["dashboard"]["devices"][2]["snapshot_id"] = "old-snapshot"

        report = build_monitor_report(summary, stale_after_seconds=1800)

        codes = {item["code"] for item in report["alerts"]}
        self.assertIn("device_stale", codes)
        self.assertIn("snapshot_mismatch", codes)

    def test_monitor_report_tracks_missing_tools_without_warning(self):
        summary = _summary()
        summary["dashboard"]["device_tools"][0]["reported"] = False

        report = build_monitor_report(summary)

        self.assertTrue(report["ok"])
        self.assertEqual(report["health"], "green")
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["info"][0]["code"], "device_tools_missing")

    def test_monitor_report_warns_on_stale_summary_cache(self):
        summary = _summary()
        summary["summary_cache"] = {"state": "stale", "age_seconds": 900, "last_error": "timed out"}

        report = build_monitor_report(summary)

        self.assertTrue(report["ok"])
        self.assertEqual(report["health"], "yellow")
        self.assertTrue(any(item["code"] == "summary_cache" for item in report["warnings"]))

    def test_monitor_report_ignores_short_stale_cache_without_error(self):
        summary = _summary()
        summary["summary_cache"] = {"state": "stale", "age_seconds": 900, "last_error": None}

        report = build_monitor_report(summary, stale_after_seconds=1800)

        self.assertTrue(report["ok"])
        self.assertEqual(report["health"], "green")
        self.assertEqual(report["warnings"], [])

    def test_monitor_report_alerts_when_summary_cache_has_no_payload(self):
        summary = _summary(health="red")
        summary["summary_cache"] = {"state": "miss", "last_error": "summary refresh timed out"}

        report = build_monitor_report(summary)

        self.assertFalse(report["ok"])
        self.assertEqual(report["health"], "red")
        self.assertTrue(any(item["code"] == "summary_cache" for item in report["alerts"]))

    def test_monitor_summary_parser_and_fetch_failure_output(self):
        parser = build_parser()
        args = parser.parse_args(["monitor-summary", "--url", "http://127.0.0.1:1/missing", "--timeout-seconds", "0.01", "--json"])
        output = StringIO()

        with redirect_stdout(output):
            result = args.func(args)

        payload = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["alerts"][0]["code"], "summary_fetch_failed")

        args = parser.parse_args(["monitor-summary", "--url", "http://127.0.0.1:1/missing", "--timeout-seconds", "0.01", "--fail-on-alert"])
        with redirect_stdout(StringIO()):
            self.assertEqual(args.func(args), 3)

    def test_monitor_summary_parser_supports_brief_output(self):
        parser = build_parser()
        args = parser.parse_args(["monitor-summary", "--url", "http://127.0.0.1:1/missing", "--timeout-seconds", "0.01", "--brief"])
        output = StringIO()

        with redirect_stdout(output):
            result = args.func(args)

        self.assertEqual(result, 0)
        self.assertIn("Skill Sync: RED - ACTION REQUIRED", output.getvalue())

    def test_write_monitor_report_creates_operator_artifacts(self):
        with TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            report = build_monitor_report(_summary())

            paths = write_monitor_report(report, out_dir)

            self.assertTrue(Path(paths["json"]).exists())
            self.assertTrue(Path(paths["text"]).exists())
            self.assertTrue(Path(paths["events"]).exists())
            self.assertEqual(json.loads(Path(paths["json"]).read_text(encoding="utf-8"))["health"], "green")
            self.assertIn("Skill Sync Monitor", Path(paths["text"]).read_text(encoding="utf-8"))
            self.assertEqual(len(Path(paths["events"]).read_text(encoding="utf-8").splitlines()), 1)

    def test_monitor_loop_one_shot_writes_fetch_failure_report(self):
        with TemporaryDirectory() as tmp:
            report = run_monitor_loop(
                "http://127.0.0.1:1/missing",
                Path(tmp),
                timeout_seconds=0.01,
                max_iterations=1,
                print_status=False,
            )

            self.assertEqual(report["health"], "red")
            self.assertEqual(report["alerts"][0]["code"], "summary_fetch_failed")
            self.assertTrue((Path(tmp) / "last-report.json").exists())

    def test_monitor_loop_parser_accepts_runtime_arguments(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "monitor-loop",
                "--url",
                "http://127.0.0.1:1/missing",
                "--out-dir",
                "/tmp/monitor",
                "--interval-seconds",
                "10",
                "--timeout-seconds",
                "1",
            ]
        )

        self.assertEqual(args.command, "monitor-loop")
        self.assertEqual(args.interval_seconds, 10)
        self.assertEqual(args.timeout_seconds, 1)


def _summary(health: str = "green", blocked: int = 0) -> dict:
    snapshot_id = "snap-1"
    return {
        "health": health,
        "remote_snapshot": {"snapshot_id": snapshot_id, "total": 96},
        "dashboard": {
            "health": health,
            "blocked": blocked,
            "operator": {
                "headline": "同步正常，无待处理项",
                "next_action": "同步链路正常；继续观察 Mac / OpenClaw 自动周期。",
            },
            "blocked_items": [],
            "devices": [
                {"id": "gateway", "name": "Gateway / NAS", "health": "green", "skills": 96, "blocked": 0, "snapshot_id": snapshot_id, "freshness": {"state": "fresh", "label": "刚刚", "age_seconds": 1}},
                {"id": "mac", "name": "Mac 本机", "health": "green", "skills": 96, "blocked": 0, "snapshot_id": snapshot_id, "freshness": {"state": "fresh", "label": "刚刚", "age_seconds": 1}},
                {"id": "oc-vps", "name": "oc-vps / OpenClaw", "health": "green", "skills": 96, "blocked": 0, "snapshot_id": snapshot_id, "freshness": {"state": "fresh", "label": "刚刚", "age_seconds": 1}},
            ],
            "planned_devices": [
                {"id": "win", "name": "Windows", "health": "not_configured", "skills": None, "blocked": None, "policy": "本阶段跳过", "snapshot_id": None, "freshness": {"state": "unknown", "label": "未知", "age_seconds": None}},
            ],
            "device_tools": [
                {"device_id": "mac", "device_name": "Mac 本机", "reported": True, "tools": [{"id": "cc-switch", "state": "detected", "skills": 96}]},
                {"device_id": "oc-vps", "device_name": "oc-vps / OpenClaw", "reported": True, "tools": [{"id": "openclaw", "state": "detected", "skills": 97}]},
            ],
        },
    }


if __name__ == "__main__":
    unittest.main()
