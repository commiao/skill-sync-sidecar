import json
import os
import time
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from skill_sync_sidecar.cli import build_parser, parse_peer_status_files, parse_remote_peer_status_paths
from skill_sync_sidecar.dashboard import (
    DASHBOARD_HTML,
    DashboardConfig,
    DashboardSummaryCache,
    RemoteSnapshotCache,
    build_dashboard_status,
    build_dashboard_summary,
    build_gateway_status,
    build_hub_import_preview_response,
)
from skill_sync_sidecar.remote import FileRemote
from skill_sync_sidecar.operator_executor import OperatorExecutorError, run_openclaw_approved_push_batch
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
                        "target": "mixed-scope-root",
                        "writer_policy": "push-pull",
                        "interval_seconds": 300,
                        "stop_on_blocked": False,
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
            self.assertEqual(status["health"], "green")
            self.assertEqual(status["remote_snapshot"]["snapshot_id"], "snap-1")
            self.assertEqual(status["remote_snapshot"]["total"], 1)
            self.assertEqual(status["base_record"]["applied_count"], 1)
            self.assertEqual(status["daemon_state"]["cycles_run"], 3)
            self.assertEqual(status["daemon_state"]["target"], "mixed-scope-root")
            self.assertEqual(status["daemon_state"]["writer_policy"], "push-pull")
            self.assertEqual(status["daemon_state"]["interval_seconds"], 300)
            self.assertFalse(status["daemon_state"]["stop_on_blocked"])
            self.assertEqual(status["sync_plan"]["summary"], {"noop": 1})
            self.assertTrue(status["sync_plan"]["safe_to_apply"])

    def test_build_ops_status_reports_blocked_queue_as_yellow(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_root = root / "skills"
            snapshot_dir = root / "remote"
            base_record = root / "base-record.json"
            blocked_report = root / "blocked-report.json"

            self._write_skill(local_root / "demo", "Demo", "Demo skill")
            index = write_snapshot(scan_roots([f"cc-switch={local_root}"]), snapshot_dir, "snap-1")
            self._write_base_record(base_record, index)
            blocked_report.write_text(
                json.dumps(
                    {
                        "record_type": "skill-sync-blocked-report",
                        "created_at": "2026-06-23T00:00:00Z",
                        "writer_policy": "pull-only",
                        "total": 1,
                        "summary": {"writer_policy": 1},
                        "items": [
                            {
                                "skill_id": "demo",
                                "category": "writer_policy",
                                "status_action": "push",
                                "plan_action": "blocked",
                                "reason": "writer policy pull-only blocks push",
                                "recommendation": "Review before approved-push.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            status = build_ops_status(
                local_root,
                snapshot_dir,
                base_record=base_record,
                blocked_report=blocked_report,
                writer_policy="pull-only",
            )

            self.assertFalse(status["ok"])
            self.assertEqual(status["health"], "yellow")
            self.assertEqual(status["blocked_report"]["total"], 1)
            self.assertEqual(status["blocked_report"]["items"][0]["skill_id"], "demo")

            text = render_ops_status_text(status)

            self.assertIn("health: yellow", text)
            self.assertIn("blocked_report: total=1 writer_policy=pull-only", text)
            self.assertIn("blocked_item: demo", text)

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
            "health": "green",
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
            "blocked_report": None,
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
        self.assertIn("health: green", text)
        self.assertIn("blocked_report: none", text)
        self.assertIn("sync_plan: safe_to_apply=True blocked=0", text)
        self.assertIn("openclaw_reconcile: safe_to_auto_apply=True", text)
        self.assertIn("openclaw_gate: ok=True", text)
        self.assertIn("overall_ok: True", text)

    def test_ops_status_parser_command_emits_json(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_root = root / "skills"
            snapshot_dir = root / "remote"
            base_record = root / "base-record.json"

            self._write_skill(local_root / "demo", "Demo", "Demo skill")
            index = write_snapshot(scan_roots([f"cc-switch={local_root}"]), snapshot_dir, "snap-cli-ops")
            self._write_base_record(base_record, index)

            parser = build_parser()
            args = parser.parse_args(
                [
                    "ops-status",
                    "--local-root",
                    str(local_root),
                    "--remote-snapshot",
                    str(snapshot_dir),
                    "--base-record",
                    str(base_record),
                    "--json",
                ]
            )
            output = StringIO()

            with redirect_stdout(output):
                result = args.func(args)

            payload = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(payload["remote_snapshot"]["snapshot_id"], "snap-cli-ops")
            self.assertEqual(payload["sync_plan"]["summary"], {"noop": 1})

    def test_ops_status_exposes_live_blocked_plan_items(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_root = root / "skills"
            snapshot_dir = root / "remote"
            base_record = root / "base-record.json"

            self._write_skill(local_root / "demo", "Demo", "Base skill")
            index = write_snapshot(scan_roots([f"cc-switch={local_root}"]), snapshot_dir, "snap-live-blocked")
            self._write_base_record(base_record, index)
            self._write_skill(local_root / "demo", "Demo", "Local change")

            status = build_ops_status(
                local_root,
                snapshot_dir,
                base_record=base_record,
                writer_policy="pull-only",
            )

            self.assertEqual(status["health"], "yellow")
            self.assertEqual(status["sync_plan"]["blocked"], 1)
            self.assertEqual(status["sync_plan"]["blocked_items"][0]["skill_id"], "demo")
            self.assertEqual(status["sync_plan"]["blocked_items"][0]["category"], "writer_policy")
            self.assertIn("approved push", status["sync_plan"]["blocked_items"][0]["recommendation"])

    def test_dashboard_status_reuses_ops_status_model(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_root = root / "skills"
            snapshot_dir = root / "remote"
            base_record = root / "base-record.json"
            peer_status = root / "openclaw-status.json"

            self._write_skill(local_root / "demo", "Demo", "Demo skill")
            index = write_snapshot(scan_roots([f"cc-switch={local_root}"]), snapshot_dir, "snap-1")
            self._write_base_record(base_record, index)
            peer_status.write_text(
                json.dumps(
                    {
                        "published_at": datetime.now(timezone.utc).isoformat(),
                        "health": "yellow",
                        "writer_policy": "pull-only",
                        "remote_snapshot": {"total": 95},
                        "sync_plan": {
                            "writer_policy": "pull-only",
                            "blocked": 1,
                            "local_overrides": {"total": 2, "skills": ["disk-cleanup", "lark-cli-adapter"]},
                        },
                        "blocked_report": {
                            "total": 1,
                            "items": [
                                {
                                    "skill_id": "beijing-recruitment",
                                    "status_action": "push",
                                    "category": "writer_policy",
                                    "reason": "writer policy pull-only blocks push",
                                }
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )

            status = build_dashboard_status(
                DashboardConfig(
                    local_root=local_root,
                    remote_snapshot=snapshot_dir,
                    base_record=base_record,
                    allow_new=True,
                    peer_status_files={"oc-vps": peer_status},
                )
            )
            devices = {device["id"]: device for device in status["dashboard"]["devices"]}

            self.assertEqual(status["health"], "green")
            self.assertEqual(status["sync_plan"]["summary"], {"noop": 1})
            self.assertIn("dashboard", status)
            self.assertEqual(status["dashboard"]["health"], "yellow")
            self.assertEqual(status["dashboard"]["blocked"], 1)
            self.assertEqual(status["dashboard"]["operator"]["headline"], "存在待审批同步项")
            self.assertIn("approved-push", status["dashboard"]["operator"]["next_action"])
            self.assertEqual(status["dashboard"]["operator"]["blocked_count"], 1)
            self.assertEqual(status["dashboard"]["operator"]["top_issue"]["peer_id"], "oc-vps")
            self.assertEqual(status["dashboard"]["operator"]["top_issue"]["skill_id"], "beijing-recruitment")
            self.assertEqual(status["dashboard"]["operator"]["top_issue"]["category"], "writer_policy")
            self.assertIn("dry-run", status["dashboard"]["operator"]["top_issue"]["action"])
            guide = status["dashboard"]["operator"]["action_guide"]
            self.assertEqual(guide["title"], "现在需要人工审核")
            self.assertIn("OpenClaw 有 1 个本地 skill 变更", guide["summary"])
            self.assertEqual(guide["skills"], ["beijing-recruitment"])
            self.assertEqual(
                guide["steps"][0]["command"],
                "scripts/openclaw-approved-push-batch.sh beijing-recruitment",
            )
            self.assertEqual(
                guide["steps"][1]["command"],
                "scripts/openclaw-approved-push-batch.sh --yes beijing-recruitment",
            )
            self.assertEqual(
                status["dashboard"]["operator"]["top_issue"]["command"],
                "scripts/openclaw-approved-push-batch.sh beijing-recruitment",
            )
            self.assertIn("beijing-recruitment", status["dashboard"]["operator"]["next_action"])
            self.assertIn("approved-push", status["dashboard"]["operator"]["next_action"])
            self.assertIn("OpenClaw", status["dashboard"]["operator"]["sync_path"])
            self.assertEqual(status["dashboard"]["blocked_items"][0]["peer_id"], "oc-vps")
            self.assertEqual(status["dashboard"]["blocked_items"][0]["skill_id"], "beijing-recruitment")
            self.assertIn("dry-run", status["dashboard"]["blocked_items"][0]["operator_action"])
            self.assertEqual(
                status["dashboard"]["blocked_items"][0]["operator_command"],
                "scripts/openclaw-approved-push-batch.sh beijing-recruitment",
            )
            self.assertEqual(devices["oc-vps"]["health"], "yellow")
            self.assertEqual(devices["oc-vps"]["blocked"], 1)
            self.assertEqual(devices["oc-vps"]["skills"], 95)
            self.assertEqual(devices["oc-vps"]["local_policy"], ["disk-cleanup", "lark-cli-adapter"])
            self.assertEqual(devices["oc-vps"]["freshness"]["state"], "fresh")
            self.assertIsNotNone(devices["oc-vps"]["last_seen_at"])
            self.assertNotIn("win", devices)
            self.assertEqual(status["dashboard"]["planned_devices"][0]["id"], "win")
            self.assertEqual(status["dashboard"]["planned_devices"][0]["policy"], "本阶段跳过")
            self.assertNotIn("windows", status["dashboard"]["operator"]["devices"])
            self.assertIn("deferred_devices", status["dashboard"]["operator"])
            self.assertIn("freshness=", status["dashboard"]["operator"]["devices"]["openclaw"])
            self.assertTrue(any(tool["id"] == "cc-switch" for tool in status["dashboard"]["tools"]))
            self.assertEqual(status["dashboard"]["local_workspace"]["scope"], "local")
            self.assertEqual(status["dashboard"]["local_workspace"]["device_id"], "mac")
            self.assertFalse(status["dashboard"]["local_workspace"]["operations"]["operate_other_devices"])
            self.assertEqual(status["dashboard"]["central_repository"]["scope"], "central")
            self.assertFalse(status["dashboard"]["central_repository"]["operations"]["direct_edit"])
            self.assertEqual(status["dashboard"]["device_map"]["scope"], "devices")
            self.assertTrue(any(device["id"] == "mac" and device["operation_scope"] == "local" for device in status["dashboard"]["device_map"]["items"]))
            self.assertTrue(any(device["id"] == "oc-vps" and device["operation_scope"] == "remote_read_only" for device in status["dashboard"]["device_map"]["items"]))
            self.assertIn("/api/summary", DASHBOARD_HTML)
            self.assertIn("Skill 同步工作台", DASHBOARD_HTML)
            self.assertIn("本机操作 · 中央仓库 · 设备状态", DASHBOARD_HTML)
            self.assertIn("status-strip", DASHBOARD_HTML)
            self.assertIn("当前处理状态", DASHBOARD_HTML)
            self.assertIn("当前要处理", DASHBOARD_HTML)
            self.assertIn("id=\"strip-health\"", DASHBOARD_HTML)
            self.assertIn("id=\"strip-blocked\"", DASHBOARD_HTML)
            self.assertIn("id=\"strip-focus-note\"", DASHBOARD_HTML)
            self.assertIn("id=\"strip-scan-local\"", DASHBOARD_HTML)
            self.assertIn("id=\"strip-dry-run\"", DASHBOARD_HTML)
            self.assertIn("id=\"strip-action-note\"", DASHBOARD_HTML)
            self.assertIn("blocked > 0 ? \"待审批\"", DASHBOARD_HTML)
            self.assertIn("同步范围摘要", DASHBOARD_HTML)
            self.assertIn("renderStatusStrip", DASHBOARD_HTML)
            self.assertIn("scope-switchboard", DASHBOARD_HTML)
            self.assertIn("Skill 同步分区", DASHBOARD_HTML)
            self.assertIn("scope-readonly-rail", DASHBOARD_HTML)
            self.assertIn("<details class=\"secondary-context\">", DASHBOARD_HTML)
            self.assertIn("权限边界和执行细节", DASHBOARD_HTML)
            self.assertIn("id=\"scope-local-count\"", DASHBOARD_HTML)
            self.assertIn("id=\"scope-central-count\"", DASHBOARD_HTML)
            self.assertIn("id=\"scope-device-count\"", DASHBOARD_HTML)
            self.assertIn("id=\"scope-scan\"", DASHBOARD_HTML)
            self.assertIn("id=\"scope-dry-run\"", DASHBOARD_HTML)
            self.assertIn("id=\"scope-publish\"", DASHBOARD_HTML)
            self.assertIn("renderScopeSwitchboard", DASHBOARD_HTML)
            self.assertIn("授权发现本机目录", DASHBOARD_HTML)
            self.assertIn("这里的操作只影响当前设备", DASHBOARD_HTML)
            self.assertIn("中央仓库和其他设备只读状态", DASHBOARD_HTML)
            self.assertIn("decision-console", DASHBOARD_HTML)
            self.assertLess(
                DASHBOARD_HTML.index("<section class=\"workspace-overview\""),
                DASHBOARD_HTML.index("<section id=\"review-queue-panel\""),
            )
            self.assertLess(
                DASHBOARD_HTML.index("<section class=\"workspace-overview\""),
                DASHBOARD_HTML.index("<section class=\"scope-switchboard\""),
            )
            self.assertLess(
                DASHBOARD_HTML.index("<section id=\"review-queue-panel\""),
                DASHBOARD_HTML.index("<section class=\"decision-console\""),
            )
            self.assertIn("id=\"operator-panel\"", DASHBOARD_HTML)
            self.assertIn("当前要做", DASHBOARD_HTML)
            self.assertIn("下一步", DASHBOARD_HTML)
            self.assertIn("技术摘要", DASHBOARD_HTML)
            self.assertIn("安全边界", DASHBOARD_HTML)
            self.assertIn("boundary-title", DASHBOARD_HTML)
            self.assertIn("执行细节和本机执行器", DASHBOARD_HTML)
            self.assertIn("guide-details", DASHBOARD_HTML)
            self.assertIn("conciseOperatorNext", DASHBOARD_HTML)
            self.assertIn("conciseGuideSummary", DASHBOARD_HTML)
            self.assertIn("renderSkillChips", DASHBOARD_HTML)
            self.assertIn("skill-chip-row", DASHBOARD_HTML)
            self.assertIn("id=\"review-queue-panel\"", DASHBOARD_HTML)
            self.assertIn("待审批清单", DASHBOARD_HTML)
            self.assertIn("待办任务", DASHBOARD_HTML)
            self.assertIn("renderReviewQueue", DASHBOARD_HTML)
            self.assertIn("currentReviewQueueItems", DASHBOARD_HTML)
            self.assertIn("rerenderReviewQueueIfViewportModeChanged", DASHBOARD_HTML)
            self.assertIn("window.addEventListener(\"resize\"", DASHBOARD_HTML)
            self.assertIn("reviewActionText", DASHBOARD_HTML)
            self.assertIn("reviewRiskText", DASHBOARD_HTML)
            self.assertIn("reviewNextStepText", DASHBOARD_HTML)
            self.assertIn("review-meta", DASHBOARD_HTML)
            self.assertIn("review-controls", DASHBOARD_HTML)
            self.assertIn("review-dry-run-button", DASHBOARD_HTML)
            self.assertIn("runExecutorActionForSkill", DASHBOARD_HTML)
            self.assertIn("id=\"review-progress\"", DASHBOARD_HTML)
            self.assertIn("id=\"review-feedback\"", DASHBOARD_HTML)
            self.assertIn("待审批处理进度", DASHBOARD_HTML)
            self.assertIn("reviewTaskResults", DASHBOARD_HTML)
            self.assertIn("renderReviewProgress", DASHBOARD_HTML)
            self.assertIn("setReviewFeedback", DASHBOARD_HTML)
            self.assertIn("updateReviewTaskResult", DASHBOARD_HTML)
            self.assertIn("等待预检", DASHBOARD_HTML)
            self.assertIn("预检通过", DASHBOARD_HTML)
            self.assertIn("safe_to_push=true，可以继续确认发布到中央仓库", DASHBOARD_HTML)
            self.assertIn("预检", DASHBOARD_HTML)
            self.assertIn(".review-meta-item:not(:last-child)", DASHBOARD_HTML)
            self.assertIn("查看 dry-run 命令", DASHBOARD_HTML)
            self.assertIn("review-more", DASHBOARD_HTML)
            self.assertIn(".review-list::after", DASHBOARD_HTML)
            self.assertIn("个待审，先预检", DASHBOARD_HTML)
            self.assertIn("mobileReview ? 0 : 1", DASHBOARD_HTML)
            self.assertIn(".review-item > div:nth-child(2)", DASHBOARD_HTML)
            self.assertIn("完整队列在下方高级诊断", DASHBOARD_HTML)
            self.assertIn("workspace-overview", DASHBOARD_HTML)
            self.assertIn("只操作本机", DASHBOARD_HTML)
            self.assertIn("这里是操作区；中央", DASHBOARD_HTML)
            self.assertLess(
                DASHBOARD_HTML.index("<section class=\"workspace-overview\""),
                DASHBOARD_HTML.index("<section class=\"decision-console\""),
            )
            self.assertLess(
                DASHBOARD_HTML.index("<section class=\"workspace-overview\""),
                DASHBOARD_HTML.index("<section id=\"review-queue-panel\""),
            )
            self.assertIn("id=\"workspace-overview-summary\"", DASHBOARD_HTML)
            self.assertIn("renderWorkspaceOverviewSummary", DASHBOARD_HTML)
            self.assertIn("本地 Skill 工作区", DASHBOARD_HTML)
            self.assertIn("可操作 · 只影响当前设备", DASHBOARD_HTML)
            self.assertIn("id=\"local-workspace-total\"", DASHBOARD_HTML)
            self.assertIn("id=\"local-workspace-blocked\"", DASHBOARD_HTML)
            self.assertIn("id=\"local-workspace-source\"", DASHBOARD_HTML)
            self.assertIn("id=\"local-workspace-action-note\"", DASHBOARD_HTML)
            self.assertIn("只读状态 · 不直接编辑", DASHBOARD_HTML)
            self.assertIn("设备实测 · 只读观察", DASHBOARD_HTML)
            self.assertIn("的本地 skill：扫描、预检、再显式推送", DASHBOARD_HTML)
            self.assertLess(
                DASHBOARD_HTML.index("<div class=\"workspace-actions\">"),
                DASHBOARD_HTML.index("<div class=\"workspace-metrics\">"),
            )
            self.assertIn("WebDAV 快照", DASHBOARD_HTML)
            self.assertIn("共享事实源收录", DASHBOARD_HTML)
            self.assertIn("这里不直接编辑，只接收显式推送", DASHBOARD_HTML)
            self.assertIn("statusLabel", DASHBOARD_HTML)
            self.assertIn("scopeLabel", DASHBOARD_HTML)
            self.assertIn("中央仓库状态", DASHBOARD_HTML)
            self.assertIn("其他设备状态", DASHBOARD_HTML)
            self.assertIn("id=\"local-workspace-tools\"", DASHBOARD_HTML)
            self.assertIn("id=\"central-repository-kv\"", DASHBOARD_HTML)
            self.assertIn("id=\"device-map\"", DASHBOARD_HTML)
            self.assertIn("<details class=\"advanced-diagnostics\">", DASHBOARD_HTML)
            self.assertIn("高级诊断：状态、设备、工具、队列明细", DASHBOARD_HTML)
            self.assertIn("refreshLocalWorkspace", DASHBOARD_HTML)
            self.assertIn("id=\"devices\"", DASHBOARD_HTML)
            self.assertIn("id=\"planned-devices\"", DASHBOARD_HTML)
            self.assertIn("id=\"tools\"", DASHBOARD_HTML)
            self.assertIn("id=\"device-tools\"", DASHBOARD_HTML)
            self.assertIn("id=\"operator-headline\"", DASHBOARD_HTML)
            self.assertIn("id=\"operator-verdict\"", DASHBOARD_HTML)
            self.assertIn("id=\"operator-brief\"", DASHBOARD_HTML)
            self.assertIn("renderOperatorBrief", DASHBOARD_HTML)
            self.assertIn("id=\"action-guide\"", DASHBOARD_HTML)
            self.assertIn("renderActionGuide", DASHBOARD_HTML)
            self.assertIn("copyCommand", DASHBOARD_HTML)
            self.assertIn("executor-panel", DASHBOARD_HTML)
            self.assertIn("runExecutorAction", DASHBOARD_HTML)
            self.assertIn("127.0.0.1:18765", DASHBOARD_HTML)
            self.assertIn("现在怎么做", DASHBOARD_HTML)
            self.assertIn("topIssueText", DASHBOARD_HTML)
            self.assertIn("blockedItemAction", DASHBOARD_HTML)
            self.assertIn("Recommendation / Next step", DASHBOARD_HTML)
            self.assertIn("action-cell", DASHBOARD_HTML)
            self.assertIn("briefLine(\"issue\"", DASHBOARD_HTML)
            self.assertIn("需要审核", DASHBOARD_HTML)
            self.assertIn("正常", DASHBOARD_HTML)
            self.assertIn("更新于", DASHBOARD_HTML)
            self.assertIn("新鲜度", DASHBOARD_HTML)
            self.assertIn("renderDeviceTools", DASHBOARD_HTML)
            self.assertIn("freshnessPill", DASHBOARD_HTML)
            self.assertIn("daemon_writer_policy", DASHBOARD_HTML)
            self.assertIn("待审批队列", DASHBOARD_HTML)
            self.assertIn("Recommendation", DASHBOARD_HTML)
            self.assertIn("shortHash", DASHBOARD_HTML)
            self.assertIn("/api/hub-import-preview", DASHBOARD_HTML)
            self.assertIn("id=\"hub-import-preview-button\"", DASHBOARD_HTML)

    def test_dashboard_beginner_guide_batches_openclaw_push_and_new_items(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_root = root / "skills"
            snapshot_dir = root / "remote"
            base_record = root / "base-record.json"
            peer_status = root / "openclaw-status.json"

            self._write_skill(local_root / "demo", "Demo", "Demo skill")
            index = write_snapshot(scan_roots([f"cc-switch={local_root}"]), snapshot_dir, "snap-1")
            self._write_base_record(base_record, index)
            peer_status.write_text(
                json.dumps(
                    {
                        "published_at": datetime.now(timezone.utc).isoformat(),
                        "health": "yellow",
                        "writer_policy": "pull-only",
                        "remote_snapshot": {"total": 96},
                        "sync_plan": {
                            "writer_policy": "pull-only",
                            "blocked": 2,
                            "blocked_items": [
                                {
                                    "skill_id": "finance-auto-bookkeeping",
                                    "status_action": "push",
                                    "plan_action": "blocked",
                                    "allowed": False,
                                    "category": "writer_policy",
                                    "reason": "writer policy pull-only blocks push",
                                },
                                {
                                    "skill_id": "wechat-editorial-automation",
                                    "status_action": "local_new",
                                    "plan_action": "blocked",
                                    "allowed": False,
                                    "category": "writer_policy",
                                    "reason": "writer policy pull-only blocks push_new",
                                },
                            ],
                        },
                        "blocked_report": {"total": 0, "summary": {}, "items": []},
                    }
                ),
                encoding="utf-8",
            )

            status = build_dashboard_status(
                DashboardConfig(
                    local_root=local_root,
                    remote_snapshot=snapshot_dir,
                    base_record=base_record,
                    allow_new=True,
                    peer_status_files={"oc-vps": peer_status},
                )
            )

            guide = status["dashboard"]["operator"]["action_guide"]
            self.assertEqual(guide["state"], "yellow")
            self.assertEqual(guide["skills"], ["finance-auto-bookkeeping", "wechat-editorial-automation"])
            self.assertEqual(
                guide["steps"][0]["command"],
                "scripts/openclaw-approved-push-batch.sh finance-auto-bookkeeping wechat-editorial-automation",
            )
            self.assertEqual(
                guide["steps"][1]["command"],
                "scripts/openclaw-approved-push-batch.sh --yes finance-auto-bookkeeping wechat-editorial-automation",
            )
            self.assertEqual(
                status["dashboard"]["blocked_items"][1]["operator_command"],
                "scripts/openclaw-approved-push-batch.sh wechat-editorial-automation",
            )

    def test_dashboard_uses_live_blocked_items_when_report_is_stale(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_root = root / "skills"
            snapshot_dir = root / "remote"
            base_record = root / "base-record.json"
            peer_status = root / "openclaw-status.json"

            self._write_skill(local_root / "demo", "Demo", "Demo skill")
            index = write_snapshot(scan_roots([f"cc-switch={local_root}"]), snapshot_dir, "snap-1")
            self._write_base_record(base_record, index)
            peer_status.write_text(
                json.dumps(
                    {
                        "published_at": datetime.now(timezone.utc).isoformat(),
                        "health": "yellow",
                        "writer_policy": "pull-only",
                        "remote_snapshot": {"total": 96},
                        "sync_plan": {
                            "writer_policy": "pull-only",
                            "blocked": 1,
                            "blocked_items": [
                                {
                                    "skill_id": "hebei-recruitment",
                                    "status_action": "push",
                                    "plan_action": "blocked",
                                    "allowed": False,
                                    "category": "writer_policy",
                                    "reason": "writer policy pull-only blocks push",
                                    "recommendation": "Review before approved-push.",
                                }
                            ],
                        },
                        "blocked_report": {"total": 0, "summary": {}, "items": []},
                    }
                ),
                encoding="utf-8",
            )

            status = build_dashboard_status(
                DashboardConfig(
                    local_root=local_root,
                    remote_snapshot=snapshot_dir,
                    base_record=base_record,
                    allow_new=True,
                    peer_status_files={"oc-vps": peer_status},
                )
            )

            self.assertEqual(status["dashboard"]["blocked"], 1)
            self.assertEqual(status["dashboard"]["blocked_items"][0]["skill_id"], "hebei-recruitment")
            self.assertEqual(status["dashboard"]["blocked_items"][0]["source"], "live_sync_plan")

    def test_dashboard_hub_import_preview_response_is_non_writing_dry_run(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            hub = home / ".skillshub"
            cc_switch = home / ".cc-switch" / "skills"
            work = root / "work"

            self._write_skill(hub / "stale", "stale", "old")
            self._write_skill(cc_switch / "stale", "stale", "new")
            self._write_skill(cc_switch / "fresh", "fresh", "fresh")

            response = build_hub_import_preview_response(
                work,
                hub_root=hub,
                source_roots=[("cc-switch", cc_switch)],
            )

            self.assertTrue(response["ok"])
            self.assertEqual(response["mode"], "dry_run")
            self.assertFalse(response["writes_files"])
            self.assertTrue(Path(response["preview"]["preview_json"]).exists())
            self.assertEqual(response["apply_plan"]["allowed"], 1)
            self.assertEqual(response["apply_plan"]["blocked"], 1)
            self.assertFalse((hub / "fresh").exists())

    def test_dashboard_parser_accepts_ops_status_arguments(self):
        parser = build_parser()

        args = parser.parse_args(
            [
                "dashboard",
                "--local-root",
                "/tmp/skills",
                "--remote-snapshot",
                "/tmp/snapshot",
                "--writer-policy",
                "pull-only",
                "--allow-new",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--peer-status",
                "oc-vps=/tmp/openclaw.json",
            ]
        )

        self.assertEqual(args.command, "dashboard")
        self.assertEqual(args.writer_policy, "pull-only")
        self.assertTrue(args.allow_new)
        self.assertEqual(args.port, 0)
        self.assertEqual(parse_peer_status_files(args.peer_status)["oc-vps"], Path("/tmp/openclaw.json"))

        with self.assertRaises(ValueError):
            parse_peer_status_files(["broken"])

    def test_gateway_status_reads_remote_snapshot_without_static_export(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_skills = root / "source-skills"
            remote_dir = root / "remote"
            cache_dir = root / "gateway-cache"
            peer_status = root / "openclaw-status.json"

            self._write_skill(source_skills / "demo", "Demo", "Demo skill")
            index = write_snapshot(scan_roots([f"cc-switch={source_skills}"]), remote_dir, "snap-gateway")
            peer_status.write_text(
                json.dumps(
                    {
                        "published_at": datetime.now(timezone.utc).isoformat(),
                        "health": "green",
                        "writer_policy": "pull-only",
                        "remote_snapshot": {"total": 1},
                        "sync_plan": {"writer_policy": "pull-only", "blocked": 0},
                    }
                ),
                encoding="utf-8",
            )

            cache = RemoteSnapshotCache(FileRemote(remote_dir), "", cache_dir, refresh_interval_seconds=3600)
            status = build_gateway_status(
                cache,
                {"oc-vps": peer_status},
                {
                    "mac": {
                        "published_at": datetime.now(timezone.utc).isoformat(),
                        "health": "green",
                        "writer_policy": "push-pull",
                        "remote_snapshot": {"total": 1},
                        "sync_plan": {"writer_policy": "push-pull", "blocked": 0},
                    }
                },
            )

            self.assertEqual(status["mode"], "gateway")
            self.assertEqual(status["remote_snapshot"]["snapshot_id"], "snap-gateway")
            self.assertEqual(status["remote_snapshot"]["total"], index["total"])
            self.assertEqual(status["writer_policy"], "read-only")
            self.assertEqual(status["sync_plan"]["summary"], {"observed": 1})
            self.assertTrue((cache_dir / "index.json").exists())
            self.assertTrue((cache_dir / index["skills"][0]["archive"]).exists())
            devices = {device["id"]: device for device in status["dashboard"]["devices"]}
            self.assertEqual(devices["gateway"]["policy"], "read-only")
            self.assertEqual(devices["mac"]["health"], "green")
            self.assertEqual(devices["oc-vps"]["health"], "green")
            self.assertEqual(devices["gateway"]["snapshot_id"], "snap-gateway")
            self.assertEqual(devices["mac"]["freshness"]["state"], "fresh")
            self.assertEqual(devices["oc-vps"]["freshness"]["state"], "fresh")
            self.assertIsNotNone(devices["gateway"]["last_seen_at"])
            tools = {tool["id"]: tool for tool in status["dashboard"]["tools"]}
            self.assertEqual(tools["cc-switch"]["state"], "observer")
            self.assertIsNone(tools["cc-switch"]["installed"])
            self.assertEqual(tools["cc-switch"]["role"], "远端投影")
            self.assertNotEqual(tools["cc-switch"]["state"], "not_found")
            self.assertIn("不扫描 NAS", tools["cc-switch"]["note"])
            device_tools = {group["device_id"]: group for group in status["dashboard"]["device_tools"]}
            self.assertIn("mac", device_tools)
            self.assertIn("oc-vps", device_tools)
            self.assertNotIn("win", devices)
            self.assertEqual(status["dashboard"]["planned_devices"][0]["id"], "win")
            self.assertFalse(device_tools["mac"]["reported"])
            self.assertEqual(device_tools["mac"]["tools"][0]["state"], "unknown")
            self.assertEqual(device_tools["oc-vps"]["tools"][0]["state"], "unknown")

    def test_gateway_status_groups_reported_device_tools(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_skills = root / "source-skills"
            remote_dir = root / "remote"
            cache_dir = root / "gateway-cache"

            self._write_skill(source_skills / "demo", "Demo", "Demo skill")
            write_snapshot(scan_roots([f"cc-switch={source_skills}"]), remote_dir, "snap-gateway-tools")

            cache = RemoteSnapshotCache(FileRemote(remote_dir), "", cache_dir, refresh_interval_seconds=3600)
            status = build_gateway_status(
                cache,
                remote_peer_status={
                    "mac": {
                        "peer_status_version": 1,
                        "published_at": datetime.now(timezone.utc).isoformat(),
                        "health": "green",
                        "remote_snapshot": {"total": 1},
                        "sync_plan": {"writer_policy": "push-pull", "blocked": 0},
                        "tools": [
                            {
                                "id": "codex",
                                "name": "Codex",
                                "roots": ["/tmp/codex"],
                                "path": "/tmp/codex",
                                "role": "Codex 可发现目录",
                                "installed": True,
                                "state": "detected",
                                "skills": 3,
                                "risk": {"ok": 3, "warning": 0, "error": 0},
                                "measured_at": "2026-06-28T00:00:00Z",
                                "note": "已检测到目录",
                            }
                        ],
                    }
                },
            )

            device_tools = {group["device_id"]: group for group in status["dashboard"]["device_tools"]}

            self.assertTrue(device_tools["mac"]["reported"])
            self.assertEqual(device_tools["mac"]["peer_status_version"], 1)
            self.assertEqual(device_tools["mac"]["tools"][0]["id"], "codex")
            self.assertEqual(device_tools["mac"]["tools"][0]["state"], "detected")
            self.assertFalse(device_tools["oc-vps"]["reported"])

    def test_dashboard_summary_keeps_ui_data_without_heavy_projection(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_skills = root / "source-skills"
            remote_dir = root / "remote"
            cache_dir = root / "gateway-cache"

            self._write_skill(source_skills / "demo", "Demo", "Demo skill")
            write_snapshot(scan_roots([f"cc-switch={source_skills}"]), remote_dir, "snap-summary")

            cache = RemoteSnapshotCache(FileRemote(remote_dir), "", cache_dir, refresh_interval_seconds=3600)
            status = build_gateway_status(
                cache,
                remote_peer_status={
                    "mac": {
                        "peer_status_version": 1,
                        "published_at": datetime.now(timezone.utc).isoformat(),
                        "health": "green",
                        "remote_snapshot": {"total": 1},
                        "sync_plan": {"writer_policy": "push-pull", "blocked": 0},
                        "tools": [{"id": "cc-switch", "name": "cc-switch", "state": "detected", "skills": 1}],
                    }
                },
            )
            status["dashboard"]["hub_import"] = {
                "ok": True,
                "hub_total": 30,
                "source_total": 60,
                "summary": {"already_in_hub": 80},
                "action_plan": {
                    "mode": "dry_run",
                    "safe_to_apply_automatically": False,
                    "summary": {"skip_existing": 80},
                    "review_required": 0,
                    "actions": [{"skill_id": f"skill-{i}", "action": "skip_existing"} for i in range(80)],
                },
                "items": [{"skill_id": f"hub-{i}", "status": "already_in_hub"} for i in range(80)],
            }

            summary = build_dashboard_summary(status)

            self.assertEqual(summary["remote_snapshot"]["snapshot_id"], "snap-summary")
            self.assertEqual(summary["dashboard"]["operator"]["snapshot_id"], "snap-summary")
            self.assertIn("tools", summary["dashboard"])
            self.assertIn("device_tools", summary["dashboard"])
            self.assertIn("planned_devices", summary["dashboard"])
            self.assertEqual(summary["dashboard"]["planned_devices"][0]["id"], "win")
            self.assertNotIn("tool_projection", summary["dashboard"])
            self.assertNotIn("items", summary["sync_plan"])
            self.assertNotIn("actions", summary["dashboard"]["hub_import"]["action_plan"])
            self.assertLess(len(summary["dashboard"]["hub_import"]["items"]), 80)
            self.assertEqual(summary["dashboard"]["hub_import"]["items"][0]["skill_id"], "hub-0")

    def test_dashboard_summary_cache_returns_503_without_seed_when_provider_times_out(self):
        def slow_provider():
            time.sleep(0.2)
            return {"ok": True, "health": "green", "dashboard": {"health": "green"}}

        cache = DashboardSummaryCache(slow_provider, timeout_seconds=0.01, stale_after_seconds=0)

        status_code, payload = cache.get_summary()

        self.assertEqual(status_code, 503)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["health"], "red")
        self.assertEqual(payload["summary_cache"]["state"], "miss")
        self.assertIn("timed out", payload["summary_cache"]["last_error"])

    def test_dashboard_summary_cache_serves_stale_payload_when_refresh_times_out(self):
        mode = {"slow": False}

        def provider():
            if mode["slow"]:
                time.sleep(0.2)
            return {
                "ok": True,
                "health": "green",
                "remote_snapshot": {"snapshot_id": "snap-cache", "total": 1},
                "daemon_state": {},
                "sync_plan": {},
                "dashboard": {"health": "green", "blocked": 0, "operator": {"snapshot_id": "snap-cache"}},
            }

        cache = DashboardSummaryCache(provider, timeout_seconds=0.01, stale_after_seconds=0)
        first_status, first_payload = cache.get_summary()
        mode["slow"] = True

        second_status, second_payload = cache.get_summary()

        self.assertEqual(first_status, 200)
        self.assertEqual(first_payload["summary_cache"]["state"], "fresh")
        self.assertEqual(second_status, 200)
        self.assertEqual(second_payload["summary_cache"]["state"], "stale")
        self.assertEqual(second_payload["remote_snapshot"]["snapshot_id"], "snap-cache")
        self.assertTrue(second_payload["summary_cache"]["refresh_in_flight"])

    def test_gateway_parser_accepts_remote_arguments(self):
        parser = build_parser()

        args = parser.parse_args(
            [
                "gateway",
                "--remote",
                "file:///tmp/snapshot",
                "--prefix",
                "current",
                "--cache-dir",
                "/tmp/cache",
                "--refresh-interval-seconds",
                "5",
                "--host",
                "0.0.0.0",
                "--port",
                "8766",
                "--peer-status",
                "oc-vps=/tmp/openclaw.json",
                "--remote-peer-status",
                "mac=skill-sync-sidecar-peer-status/mac.json",
            ]
        )

        self.assertEqual(args.command, "gateway")
        self.assertEqual(args.remote, "file:///tmp/snapshot")
        self.assertEqual(args.prefix, "current")
        self.assertEqual(args.cache_dir, "/tmp/cache")
        self.assertEqual(args.refresh_interval_seconds, 5)
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 8766)
        self.assertEqual(parse_peer_status_files(args.peer_status)["oc-vps"], Path("/tmp/openclaw.json"))
        self.assertEqual(parse_remote_peer_status_paths(args.remote_peer_status)["mac"], "skill-sync-sidecar-peer-status/mac.json")

        with self.assertRaises(ValueError):
            parse_remote_peer_status_paths(["broken"])

    def test_operator_executor_parser_accepts_local_executor_arguments(self):
        parser = build_parser()

        args = parser.parse_args(
            [
                "operator-executor",
                "--repo-root",
                "/tmp/skill-sync-sidecar",
                "--host",
                "127.0.0.1",
                "--port",
                "18765",
                "--allow-publish",
            ]
        )

        self.assertEqual(args.command, "operator-executor")
        self.assertEqual(args.repo_root, "/tmp/skill-sync-sidecar")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 18765)
        self.assertTrue(args.allow_publish)

    def test_operator_executor_runs_dry_run_and_blocks_publish_by_default(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            scripts = repo / "scripts"
            scripts.mkdir()
            helper = scripts / "openclaw-approved-push-batch.sh"
            helper.write_text(
                "#!/usr/bin/env bash\n"
                "echo openclaw_approved_push_batch_mode=${1#--}\n"
                "printf '{\"safe_to_push\":true,\"approved\":2,\"approved_skill_ids\":[\"%s\",\"%s\"]}\\n' \"$2\" \"$3\"\n",
                encoding="utf-8",
            )
            os.chmod(helper, 0o755)

            result = run_openclaw_approved_push_batch(repo, ["finance-auto-bookkeeping", "wechat-publisher"])

            self.assertTrue(result["ok"])
            self.assertEqual(result["mode"], "dry_run")
            self.assertTrue(result["safe_to_push"])
            self.assertEqual(result["approved"], 2)
            self.assertEqual(result["approved_skill_ids"], ["finance-auto-bookkeeping", "wechat-publisher"])
            self.assertIn("--dry-run", result["command"])

            with self.assertRaises(OperatorExecutorError):
                run_openclaw_approved_push_batch(repo, ["finance-auto-bookkeeping"], yes=True)

    def test_publish_peer_status_writes_remote_json(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_root = root / "skills"
            snapshot_dir = root / "snapshot"
            remote_dir = root / "remote"
            base_record = root / "base-record.json"
            state_file = root / "state.json"

            self._write_skill(local_root / "demo", "Demo", "Demo skill")
            index = write_snapshot(scan_roots([f"cc-switch={local_root}"]), snapshot_dir, "snap-publish")
            self._write_base_record(base_record, index)
            state_file.write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "daemon_status": "running",
                        "updated_at": "2026-06-28T00:00:00Z",
                        "cycles_run": 1,
                        "target": "mixed-scope-root",
                        "writer_policy": "push-pull",
                    }
                ),
                encoding="utf-8",
            )

            parser = build_parser()
            args = parser.parse_args(
                [
                    "publish-peer-status",
                    "--remote",
                    f"file://{remote_dir}",
                    "--peer-id",
                    "mac",
                    "--status-path",
                    "skill-sync-sidecar-peer-status/mac.json",
                    "--local-root",
                    str(local_root),
                    "--remote-snapshot",
                    str(snapshot_dir),
                    "--base-record",
                    str(base_record),
                    "--state-file",
                    str(state_file),
                    "--allow-new",
                ]
            )

            self.assertEqual(args.func(args), 0)
            payload = json.loads((remote_dir / "skill-sync-sidecar-peer-status" / "mac.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["record_type"], "skill-sync-peer-status")
            self.assertEqual(payload["peer_status_version"], 1)
            self.assertEqual(payload["peer_id"], "mac")
            self.assertEqual(payload["device"]["id"], "mac")
            self.assertTrue(payload["capabilities"]["tool_status"])
            self.assertIsInstance(payload["tools"], list)
            self.assertTrue(any(tool["id"] == "cc-switch" for tool in payload["tools"]))
            self.assertEqual(payload["remote_snapshot"]["snapshot_id"], "snap-publish")

    def test_publish_peer_status_can_publish_existing_peer_file(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote_dir = root / "remote"
            status_file = root / "openclaw-status.json"
            status_file.write_text(
                json.dumps(
                    {
                        "health": "green",
                        "remote_snapshot": {"snapshot_id": "snap-openclaw", "total": 94},
                        "sync_plan": {"writer_policy": "pull-only", "blocked": 0},
                    }
                ),
                encoding="utf-8",
            )

            parser = build_parser()
            args = parser.parse_args(
                [
                    "publish-peer-status",
                    "--remote",
                    f"file://{remote_dir}",
                    "--peer-id",
                    "oc-vps",
                    "--status-path",
                    "skill-sync-sidecar-peer-status/oc-vps.json",
                    "--status-file",
                    str(status_file),
                ]
            )

            self.assertEqual(args.func(args), 0)
            payload = json.loads((remote_dir / "skill-sync-sidecar-peer-status" / "oc-vps.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["record_type"], "skill-sync-peer-status")
            self.assertEqual(payload["peer_id"], "oc-vps")
            self.assertEqual(payload["remote_snapshot"]["snapshot_id"], "snap-openclaw")
            self.assertNotIn("tools", payload)

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
