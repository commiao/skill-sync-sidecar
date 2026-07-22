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
from skill_sync_sidecar.operator_executor import OperatorExecutorError, run_openclaw_approved_push_batch, run_openclaw_conflict_package
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
            self.assertIn("检查确认", status["dashboard"]["operator"]["next_action"])
            self.assertEqual(status["dashboard"]["operator"]["blocked_count"], 1)
            self.assertEqual(status["dashboard"]["operator"]["top_issue"]["peer_id"], "oc-vps")
            self.assertEqual(status["dashboard"]["operator"]["top_issue"]["skill_id"], "beijing-recruitment")
            self.assertEqual(status["dashboard"]["operator"]["top_issue"]["category"], "writer_policy")
            self.assertIn("检查", status["dashboard"]["operator"]["top_issue"]["action"])
            guide = status["dashboard"]["operator"]["action_guide"]
            self.assertEqual(guide["title"], "OpenClaw 更新需要确认")
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
            self.assertIn("发布到共享仓库", status["dashboard"]["operator"]["next_action"])
            self.assertIn("OpenClaw", status["dashboard"]["operator"]["sync_path"])
            self.assertEqual(status["dashboard"]["blocked_items"][0]["peer_id"], "oc-vps")
            self.assertEqual(status["dashboard"]["blocked_items"][0]["skill_id"], "beijing-recruitment")
            self.assertIn("检查", status["dashboard"]["blocked_items"][0]["operator_action"])
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
            self.assertIn("Skill 管理", DASHBOARD_HTML)
            self.assertIn("看第一块建议；需要操作再用“常用操作”", DASHBOARD_HTML)
            self.assertIn("quick-status-details", DASHBOARD_HTML)
            self.assertIn("可选：查看状态数字", DASHBOARD_HTML)
            self.assertIn("status-strip", DASHBOARD_HTML)
            self.assertIn("状态摘要", DASHBOARD_HTML)
            self.assertIn("当前状态", DASHBOARD_HTML)
            self.assertIn("id=\"strip-health\"", DASHBOARD_HTML)
            self.assertIn("id=\"strip-blocked\"", DASHBOARD_HTML)
            self.assertIn("id=\"strip-focus-note\"", DASHBOARD_HTML)
            self.assertIn("id=\"strip-scan-local\"", DASHBOARD_HTML)
            self.assertIn("id=\"strip-dry-run\"", DASHBOARD_HTML)
            self.assertIn("id=\"strip-action-note\"", DASHBOARD_HTML)
            self.assertIn("个需要确认", DASHBOARD_HTML)
            self.assertIn("先看报告，不会自动覆盖", DASHBOARD_HTML)
            self.assertIn("先看报告，再决定保留哪一版", DASHBOARD_HTML)
            self.assertIn("runFirstConflictPackage", DASHBOARD_HTML)
            self.assertIn("还有 ${blocked} 件事要你确认", DASHBOARD_HTML)
            self.assertIn("不会自动写入共享仓库；确认后才会发布", DASHBOARD_HTML)
            self.assertIn("只剩版本差异", DASHBOARD_HTML)
            self.assertIn("先看只读差异报告，报告会给出推荐动作", DASHBOARD_HTML)
            self.assertIn("同步范围摘要", DASHBOARD_HTML)
            self.assertIn("renderStatusStrip", DASHBOARD_HTML)
            self.assertLess(
                DASHBOARD_HTML.index("id=\"simple-action-panel\""),
                DASHBOARD_HTML.index("class=\"easy-workspace panel\""),
            )
            self.assertLess(
                DASHBOARD_HTML.index("class=\"easy-workspace panel\""),
                DASHBOARD_HTML.index("class=\"quick-status-details\""),
            )
            self.assertLess(
                DASHBOARD_HTML.index("class=\"quick-status-details\""),
                DASHBOARD_HTML.index("class=\"status-strip\""),
            )
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
            self.assertIn("共享仓库和其他设备只读状态", DASHBOARD_HTML)
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
            self.assertIn("高级：本机助手和执行日志", DASHBOARD_HTML)
            self.assertIn("guide-details", DASHBOARD_HTML)
            self.assertIn("conciseOperatorNext", DASHBOARD_HTML)
            self.assertIn("conciseGuideSummary", DASHBOARD_HTML)
            self.assertIn("renderSkillChips", DASHBOARD_HTML)
            self.assertIn("skill-chip-row", DASHBOARD_HTML)
            self.assertIn("id=\"review-queue-panel\"", DASHBOARD_HTML)
            self.assertIn("待审批清单", DASHBOARD_HTML)
            self.assertIn("待办任务", DASHBOARD_HTML)
            self.assertIn("renderReviewQueue", DASHBOARD_HTML)
            self.assertIn("id=\"review-queue-label\"", DASHBOARD_HTML)
            self.assertIn("id=\"review-queue-title\"", DASHBOARD_HTML)
            self.assertIn("版本确认", DASHBOARD_HTML)
            self.assertIn("这里不是批量发布队列", DASHBOARD_HTML)
            self.assertIn("当前版本差异", DASHBOARD_HTML)
            self.assertIn("点“生成只读报告”，报告会把推荐动作放在最上方", DASHBOARD_HTML)
            self.assertIn("版本差异处理进度", DASHBOARD_HTML)
            self.assertIn("id=\"simple-action-panel\"", DASHBOARD_HTML)
            self.assertIn("renderSimpleActionPanel", DASHBOARD_HTML)
            self.assertIn("推荐下一步", DASHBOARD_HTML)
            self.assertIn("simple-choice-grid", DASHBOARD_HTML)
            self.assertIn("aria-label=\"处理版本差异\"", DASHBOARD_HTML)
            self.assertIn("需要选择保留哪一版", DASHBOARD_HTML)
            self.assertIn("两边都有改动", DASHBOARD_HTML)
            self.assertIn("生成只读报告", DASHBOARD_HTML)
            self.assertIn("不会发布、不会恢复、不会删除", DASHBOARD_HTML)
            self.assertIn("报告出来后", DASHBOARD_HTML)
            self.assertIn("为什么停下来", DASHBOARD_HTML)
            self.assertNotIn("选择 ${skill} 保留哪一版", DASHBOARD_HTML)
            self.assertNotIn("我确定 OpenClaw 上的是最新版", DASHBOARD_HTML)
            self.assertNotIn("我确定共享仓库是正确版", DASHBOARD_HTML)
            self.assertIn("id=\"simple-action-feedback\"", DASHBOARD_HTML)
            self.assertIn("id=\"simple-action-feedback-title\"", DASHBOARD_HTML)
            self.assertIn("选择一个按钮后，这里会显示进度", DASHBOARD_HTML)
            self.assertIn("这里优先展示推荐动作", DASHBOARD_HTML)
            self.assertIn("没有可发布更新", DASHBOARD_HTML)
            self.assertIn("不会自动删除", DASHBOARD_HTML)
            self.assertIn("renderSimpleDecisionList", DASHBOARD_HTML)
            self.assertIn("renderSimpleDecisionCard", DASHBOARD_HTML)
            self.assertIn("showDecisionExplanation", DASHBOARD_HTML)
            self.assertIn("本机缺失", DASHBOARD_HTML)
            self.assertIn("版本需要确认", DASHBOARD_HTML)
            self.assertIn("从共享仓库恢复到", DASHBOARD_HTML)
            self.assertIn("restoreCentralSkill", DASHBOARD_HTML)
            self.assertIn("centralRestoreEndpointBase", DASHBOARD_HTML)
            self.assertIn("/api/mac-central-restore", DASHBOARD_HTML)
            self.assertIn("/api/openclaw-central-restore", DASHBOARD_HTML)
            self.assertIn("生成差异报告", DASHBOARD_HTML)
            self.assertIn("generateConflictPackage", DASHBOARD_HTML)
            self.assertIn("id=\"conflict-resolution-panel\"", DASHBOARD_HTML)
            self.assertIn("renderConflictResolutionPanel", DASHBOARD_HTML)
            self.assertIn("只读差异报告已生成", DASHBOARD_HTML)
            self.assertIn("版本差异摘要", DASHBOARD_HTML)
            self.assertIn("renderConflictVersionCard", DASHBOARD_HTML)
            self.assertIn("renderConflictChoiceGrid", DASHBOARD_HTML)
            self.assertIn("conflictFilesText", DASHBOARD_HTML)
            self.assertIn("OpenClaw 版", DASHBOARD_HTML)
            self.assertIn("共享仓库版", DASHBOARD_HTML)
            self.assertIn("共同基线", DASHBOARD_HTML)
            self.assertIn("文件：${count} 个", DASHBOARD_HTML)
            self.assertIn("版本指纹", DASHBOARD_HTML)
            self.assertIn("OpenClaw 当前缺失这个 skill，共享仓库仍有完整版本", DASHBOARD_HTML)
            self.assertIn("renderConflictRecommendedAction", DASHBOARD_HTML)
            self.assertIn("推荐下一步", DASHBOARD_HTML)
            self.assertIn("报告判断：OpenClaw 当前缺失，共享仓库仍有完整版本", DASHBOARD_HTML)
            self.assertIn("恢复共享仓库版到 OpenClaw", DASHBOARD_HTML)
            self.assertIn("其他选择和风险说明", DASHBOARD_HTML)
            self.assertIn("当前面板不会一键删除共享仓库", DASHBOARD_HTML)
            self.assertIn("报告判断：共享仓库缺失，OpenClaw 仍有版本", DASHBOARD_HTML)
            self.assertIn("发布 OpenClaw 版到共享仓库", DASHBOARD_HTML)
            self.assertIn("需要选择保留哪一版", DASHBOARD_HTML)
            self.assertIn("两边都有改动", DASHBOARD_HTML)
            self.assertIn("生成只读报告", DASHBOARD_HTML)
            self.assertIn("不会发布、不会恢复、不会删除", DASHBOARD_HTML)
            self.assertIn("为什么停下来", DASHBOARD_HTML)
            self.assertIn("single-choice", DASHBOARD_HTML)
            self.assertIn("保留 OpenClaw 版", DASHBOARD_HTML)
            self.assertIn("发布 OpenClaw 版到共享仓库", DASHBOARD_HTML)
            self.assertIn("publishOpenclawVersionForConflict", DASHBOARD_HTML)
            self.assertIn("allow_conflict_local_wins", DASHBOARD_HTML)
            self.assertIn("confirmProtectedWrite", DASHBOARD_HTML)
            self.assertIn("确认发布 OpenClaw 版", DASHBOARD_HTML)
            self.assertIn("确认恢复共享仓库版", DASHBOARD_HTML)
            self.assertIn("将会：", DASHBOARD_HTML)
            self.assertIn("不会：", DASHBOARD_HTML)
            self.assertIn("直接取消或输入其他内容，不会写入", DASHBOARD_HTML)
            self.assertIn("只处理这一个 skill", DASHBOARD_HTML)
            self.assertIn("执行前保留 OpenClaw 当前目录备份", DASHBOARD_HTML)
            self.assertNotIn("这会把 OpenClaw 上的", DASHBOARD_HTML)
            self.assertIn("保留共享仓库版", DASHBOARD_HTML)
            self.assertIn("恢复共享仓库版到 OpenClaw", DASHBOARD_HTML)
            self.assertIn("restoreCentralVersionForConflict", DASHBOARD_HTML)
            self.assertIn("/api/openclaw-central-restore-dry-run", DASHBOARD_HTML)
            self.assertNotIn("这会用共享仓库版本覆盖 OpenClaw", DASHBOARD_HTML)
            self.assertIn("waitForSkillResolution", DASHBOARD_HTML)
            self.assertIn("waitForSkillsResolution", DASHBOARD_HTML)
            self.assertIn("正在确认是否完成", DASHBOARD_HTML)
            self.assertIn("正在确认待办是否下降", DASHBOARD_HTML)
            self.assertIn("这一步只读，不会再写入", DASHBOARD_HTML)
            self.assertIn("版本差异已清空", DASHBOARD_HTML)
            self.assertIn("已写入，但待办还没清空", DASHBOARD_HTML)
            self.assertIn("reviewItemsForSkill", DASHBOARD_HTML)
            self.assertIn("reviewItemsForSkills", DASHBOARD_HTML)
            self.assertIn("我手动合并", DASHBOARD_HTML)
            self.assertIn("查看诊断路径和版本指纹", DASHBOARD_HTML)
            self.assertIn("explainConflictChoice", DASHBOARD_HTML)
            self.assertIn("conflictPackageEndpoint", DASHBOARD_HTML)
            self.assertIn("/api/openclaw-conflict-package", DASHBOARD_HTML)
            self.assertIn("差异报告已生成", DASHBOARD_HTML)
            self.assertIn("先查看只读差异报告，再按推荐恢复、发布或手动处理", DASHBOARD_HTML)
            self.assertIn("approved=0", DASHBOARD_HTML)
            self.assertIn("没有写入共享仓库", DASHBOARD_HTML)
            self.assertIn("当前没有可发布更新。版本差异和删除确认不会通过这个按钮自动处理。", DASHBOARD_HTML)
            self.assertIn("本机助手未连接，无法执行检查或发布。", DASHBOARD_HTML)
            self.assertIn("RESTORE", DASHBOARD_HTML)
            self.assertIn("自动确认结果", DASHBOARD_HTML)
            self.assertIn("写入后回查", DASHBOARD_HTML)
            self.assertNotIn("simple-task-dry-run", DASHBOARD_HTML)
            self.assertNotIn("const simpleTaskDryRun", DASHBOARD_HTML)
            self.assertIn("id=\"simple-dry-run\"", DASHBOARD_HTML)
            self.assertIn("id=\"simple-publish\"", DASHBOARD_HTML)
            self.assertIn("openAdvancedDetails", DASHBOARD_HTML)
            self.assertIn("<details class=\"advanced-workspace\">", DASHBOARD_HTML)
            self.assertIn("常用操作", DASHBOARD_HTML)
            self.assertIn("普通使用只看这里", DASHBOARD_HTML)
            self.assertIn("导入 / 安装本地 skill", DASHBOARD_HTML)
            self.assertIn("不需要你补配置文件", DASHBOARD_HTML)
            self.assertIn("检查后发布", DASHBOARD_HTML)
            self.assertIn("看到“无待处理”才算完成", DASHBOARD_HTML)
            self.assertIn("id=\"easy-dry-run\"", DASHBOARD_HTML)
            self.assertIn("id=\"easy-publish\"", DASHBOARD_HTML)
            self.assertIn("没有已检查通过的待发布更新", DASHBOARD_HTML)
            self.assertIn("可选：查看 Mac / OpenClaw / 共享仓库状态", DASHBOARD_HTML)
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
            self.assertIn("renderReviewRecommendation", DASHBOARD_HTML)
            self.assertIn("review-recommendation", DASHBOARD_HTML)
            self.assertIn("推荐操作", DASHBOARD_HTML)
            self.assertIn("确认缺失项是恢复还是删除", DASHBOARD_HTML)
            self.assertIn("先检查", DASHBOARD_HTML)
            self.assertIn("先处理缺失/删除确认", DASHBOARD_HTML)
            self.assertIn("再处理可发布更新", DASHBOARD_HTML)
            self.assertIn("id=\"review-dry-run-all\"", DASHBOARD_HTML)
            self.assertIn("id=\"review-publish-all\"", DASHBOARD_HTML)
            self.assertIn("确认发布 ${publishItems.length} 个 OpenClaw 更新", DASHBOARD_HTML)
            self.assertIn("下一步就是点“确认发布”", DASHBOARD_HTML)
            self.assertIn("allReviewPublishCandidatesReady", DASHBOARD_HTML)
            self.assertIn("publishCandidateSkillIds", DASHBOARD_HTML)
            self.assertIn("refreshOpenclawPeerStatus", DASHBOARD_HTML)
            self.assertIn("/api/openclaw-peer-status-refresh", DASHBOARD_HTML)
            self.assertIn("executorErrorDetail", DASHBOARD_HTML)
            self.assertIn("等待上方“确认发布”写入共享仓库", DASHBOARD_HTML)
            self.assertIn("重新检查", DASHBOARD_HTML)
            self.assertIn("renderReviewGroup", DASHBOARD_HTML)
            self.assertIn("renderReviewItem", DASHBOARD_HTML)
            self.assertIn("setReviewFeedback", DASHBOARD_HTML)
            self.assertIn("updateReviewTaskResult", DASHBOARD_HTML)
            self.assertIn("等待检查", DASHBOARD_HTML)
            self.assertIn("检查通过", DASHBOARD_HTML)
            self.assertIn("可以继续确认发布到共享仓库", DASHBOARD_HTML)
            self.assertIn("状态缓存偏旧，正在重新读取实时状态", DASHBOARD_HTML)
            self.assertIn("refresh(true);", DASHBOARD_HTML)
            self.assertIn("staleRefreshTimer", DASHBOARD_HTML)
            self.assertIn("检查", DASHBOARD_HTML)
            self.assertIn(".review-meta-item:not(:last-child)", DASHBOARD_HTML)
            self.assertIn("查看检查命令", DASHBOARD_HTML)
            self.assertIn("确认缺失项是恢复还是删除", DASHBOARD_HTML)
            self.assertIn("reviewItemKey", DASHBOARD_HTML)
            self.assertIn("simpleFeedback.className", DASHBOARD_HTML)
            self.assertIn("simple-action-feedback-detail", DASHBOARD_HTML)
            self.assertIn("只剩版本差异", DASHBOARD_HTML)
            self.assertIn("先看报告", DASHBOARD_HTML)
            self.assertNotIn("冲突表示 OpenClaw 和共享仓库都改过", DASHBOARD_HTML)
            self.assertNotIn("两边都改过，系统不会自动覆盖", DASHBOARD_HTML)
            self.assertNotIn("先生成冲突包", DASHBOARD_HTML)
            self.assertNotIn("先人工合并", DASHBOARD_HTML)
            self.assertIn("reviewIsPublishCandidate", DASHBOARD_HTML)
            self.assertIn(".review-item > div:nth-child(2)", DASHBOARD_HTML)
            self.assertNotIn("完整队列在下方高级诊断", DASHBOARD_HTML)
            self.assertNotIn("检查待审批", DASHBOARD_HTML)
            self.assertNotIn("display: none;\\n      }\\n      .review-list::after", DASHBOARD_HTML)
            self.assertIn("id=\"plain-detail-grid\"", DASHBOARD_HTML)
            self.assertIn("renderPlainDetails", DASHBOARD_HTML)
            self.assertIn("Mac 本机", DASHBOARD_HTML)
            self.assertIn("共享仓库收录", DASHBOARD_HTML)
            self.assertIn("回到上方任务卡处理", DASHBOARD_HTML)
            self.assertIn("<details class=\"technical-workspace\">", DASHBOARD_HTML)
            self.assertIn("高级：工具目录、版本号、原始队列", DASHBOARD_HTML)
            self.assertIn("workspace-overview", DASHBOARD_HTML)
            self.assertIn("只操作本机", DASHBOARD_HTML)
            self.assertIn("这里只是高级明细；常用操作请回到页面顶部", DASHBOARD_HTML)
            self.assertLess(
                DASHBOARD_HTML.index("id=\"simple-action-panel\""),
                DASHBOARD_HTML.index("class=\"easy-workspace panel\""),
            )
            self.assertLess(
                DASHBOARD_HTML.index("class=\"easy-workspace panel\""),
                DASHBOARD_HTML.index("id=\"plain-detail-grid\""),
            )
            self.assertLess(
                DASHBOARD_HTML.index("class=\"easy-workspace panel\""),
                DASHBOARD_HTML.index("<section class=\"workspace-overview\""),
            )
            self.assertLess(
                DASHBOARD_HTML.index("id=\"plain-detail-grid\""),
                DASHBOARD_HTML.index("<section class=\"workspace-overview\""),
            )
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
            self.assertIn("高级明细", DASHBOARD_HTML)
            self.assertIn("可操作 · 只影响当前设备", DASHBOARD_HTML)
            self.assertIn("workspace-flow", DASHBOARD_HTML)
            self.assertIn("本机操作流程", DASHBOARD_HTML)
            self.assertIn("1. 扫描", DASHBOARD_HTML)
            self.assertIn("只看会改什么，不写共享仓库", DASHBOARD_HTML)
            self.assertIn("确认无误后再写入共享仓库", DASHBOARD_HTML)
            self.assertIn("1 扫描本机", DASHBOARD_HTML)
            self.assertIn("2 检查", DASHBOARD_HTML)
            self.assertIn("3 发布共享仓库", DASHBOARD_HTML)
            self.assertIn("先点“分析”。通过后，按钮会自动解锁安装或发布", DASHBOARD_HTML)
            self.assertIn("id=\"local-workspace-total\"", DASHBOARD_HTML)
            self.assertIn("id=\"local-workspace-blocked\"", DASHBOARD_HTML)
            self.assertIn("id=\"local-workspace-source\"", DASHBOARD_HTML)
            self.assertIn("id=\"local-workspace-action-note\"", DASHBOARD_HTML)
            self.assertIn("workspace-secondary", DASHBOARD_HTML)
            self.assertIn("查看数量和工具目录", DASHBOARD_HTML)
            self.assertIn("id=\"local-workspace-tool-summary\"", DASHBOARD_HTML)
            self.assertIn("workspace-tool-details", DASHBOARD_HTML)
            self.assertIn("工具目录明细", DASHBOARD_HTML)
            self.assertIn("renderLocalToolSummary", DASHBOARD_HTML)
            self.assertIn("toolSummaryItem", DASHBOARD_HTML)
            self.assertIn("已检测工具", DASHBOARD_HTML)
            self.assertIn("需整理提示", DASHBOARD_HTML)
            self.assertIn("只读状态 · 不直接编辑", DASHBOARD_HTML)
            self.assertIn("其他设备 · 只读观察", DASHBOARD_HTML)
            self.assertIn("是当前页面唯一能直接操作的设备", DASHBOARD_HTML)
            self.assertIn("otherDeviceItems", DASHBOARD_HTML)
            self.assertLess(
                DASHBOARD_HTML.index("<div class=\"workspace-actions\">"),
                DASHBOARD_HTML.index("<details class=\"workspace-secondary\">"),
            )
            self.assertIn("共享仓库", DASHBOARD_HTML)
            self.assertIn("这里是只读明细，不能直接编辑", DASHBOARD_HTML)
            self.assertIn("statusLabel", DASHBOARD_HTML)
            self.assertIn("scopeLabel", DASHBOARD_HTML)
            self.assertIn("共享仓库状态", DASHBOARD_HTML)
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
            self.assertIn("建议 / 下一步", DASHBOARD_HTML)
            self.assertIn("action-cell", DASHBOARD_HTML)
            self.assertIn("briefLine(\"issue\"", DASHBOARD_HTML)
            self.assertIn("需处理", DASHBOARD_HTML)
            self.assertIn("正常", DASHBOARD_HTML)
            self.assertIn("更新于", DASHBOARD_HTML)
            self.assertIn("新鲜度", DASHBOARD_HTML)
            self.assertIn("renderDeviceTools", DASHBOARD_HTML)
            self.assertIn("freshnessPill", DASHBOARD_HTML)
            self.assertIn("daemon_writer_policy", DASHBOARD_HTML)
            self.assertIn("待审批队列", DASHBOARD_HTML)
            self.assertIn("同步摘要", DASHBOARD_HTML)
            self.assertIn("同步进程", DASHBOARD_HTML)
            self.assertIn("设备本地策略", DASHBOARD_HTML)
            self.assertIn("产物路径", DASHBOARD_HTML)
            self.assertIn("暂无待审批项", DASHBOARD_HTML)
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

    def test_remote_snapshot_cache_force_refresh_bypasses_interval(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_skills = root / "source-skills"
            remote_dir = root / "remote"
            cache_dir = root / "gateway-cache"

            self._write_skill(source_skills / "demo", "Demo", "Demo skill")
            write_snapshot(scan_roots([f"cc-switch={source_skills}"]), remote_dir, "snap-1")
            cache = RemoteSnapshotCache(FileRemote(remote_dir), "", cache_dir, refresh_interval_seconds=3600)

            first_dir = cache.snapshot_dir()
            self.assertEqual(json.loads((first_dir / "index.json").read_text(encoding="utf-8"))["snapshot_id"], "snap-1")

            self._write_skill(source_skills / "new-demo", "New Demo", "New demo skill")
            write_snapshot(scan_roots([f"cc-switch={source_skills}"]), remote_dir, "snap-2")

            cached_dir = cache.snapshot_dir()
            self.assertEqual(json.loads((cached_dir / "index.json").read_text(encoding="utf-8"))["snapshot_id"], "snap-1")

            refreshed_dir = cache.force_refresh()
            self.assertEqual(json.loads((refreshed_dir / "index.json").read_text(encoding="utf-8"))["snapshot_id"], "snap-2")

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

    def test_dashboard_summary_cache_force_refreshes_payload(self):
        calls = {"count": 0}

        def provider():
            calls["count"] += 1
            return {
                "ok": True,
                "health": "green",
                "remote_snapshot": {"snapshot_id": f"snap-{calls['count']}", "total": 1},
                "daemon_state": {},
                "sync_plan": {},
                "dashboard": {"health": "green", "blocked": 0, "operator": {"snapshot_id": f"snap-{calls['count']}"}},
            }

        cache = DashboardSummaryCache(provider, timeout_seconds=0.01, stale_after_seconds=120)
        first_status, first_payload = cache.get_summary()
        second_status, second_payload = cache.get_summary()
        forced_status, forced_payload = cache.get_summary(force=True)

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(forced_status, 200)
        self.assertEqual(first_payload["remote_snapshot"]["snapshot_id"], "snap-1")
        self.assertEqual(second_payload["remote_snapshot"]["snapshot_id"], "snap-1")
        self.assertEqual(forced_payload["remote_snapshot"]["snapshot_id"], "snap-2")
        self.assertEqual(forced_payload["summary_cache"]["state"], "fresh")

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
                "--allow-local-writes",
            ]
        )

        self.assertEqual(args.command, "operator-executor")
        self.assertEqual(args.repo_root, "/tmp/skill-sync-sidecar")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 18765)
        self.assertTrue(args.allow_publish)
        self.assertTrue(args.allow_local_writes)

    def test_local_skill_publish_parser_accepts_selective_publish_arguments(self):
        parser = build_parser()

        args = parser.parse_args(
            [
                "local-skill-publish",
                "--path",
                "/tmp/read-wechat-article",
                "--local-root",
                "/tmp/local",
                "--remote-snapshot",
                "/tmp/cache",
                "--last-applied-record",
                "/tmp/base.json",
                "--base-record-out",
                "/tmp/base-next.json",
                "--remote",
                "file:///tmp/remote",
                "--prefix",
                "skill-sync-sidecar-dev/current-mac",
                "--dry-run",
            ]
        )

        self.assertEqual(args.command, "local-skill-publish")
        self.assertEqual(args.path, "/tmp/read-wechat-article")
        self.assertEqual(args.local_root, "/tmp/local")
        self.assertEqual(args.remote_snapshot, "/tmp/cache")
        self.assertEqual(args.last_applied_record, "/tmp/base.json")
        self.assertEqual(args.base_record_out, "/tmp/base-next.json")
        self.assertEqual(args.remote, "file:///tmp/remote")
        self.assertEqual(args.prefix, "skill-sync-sidecar-dev/current-mac")
        self.assertTrue(args.dry_run)

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

    def test_operator_executor_allows_explicit_conflict_local_wins_preview(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            scripts = repo / "scripts"
            scripts.mkdir()
            helper = scripts / "openclaw-approved-push-batch.sh"
            helper.write_text(
                "#!/usr/bin/env bash\n"
                "printf '{\"safe_to_push\":true,\"approved\":1,\"approved_skill_ids\":[\"%s\"],\"allow_conflict_local_wins\":true}\\n' \"${@: -1}\"\n",
                encoding="utf-8",
            )
            os.chmod(helper, 0o755)

            result = run_openclaw_approved_push_batch(
                repo,
                ["finance-auto-bookkeeping"],
                allow_conflict_local_wins=True,
            )

            self.assertTrue(result["ok"])
            self.assertTrue(result["allow_conflict_local_wins"])
            self.assertIn("--allow-conflict-local-wins", result["command"])
            self.assertEqual(result["approved_skill_ids"], ["finance-auto-bookkeeping"])

    def test_operator_executor_runs_openclaw_conflict_package(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            scripts = repo / "scripts"
            scripts.mkdir()
            package_dir = repo / "conflict"
            (package_dir / "local" / "scripts").mkdir(parents=True)
            (package_dir / "remote").mkdir(parents=True)
            (package_dir / "local" / "SKILL.md").write_text(
                "---\nname: Finance Local\ndescription: OpenClaw edited version\n---\n# Finance Local\n",
                encoding="utf-8",
            )
            (package_dir / "local" / "scripts" / "run.py").write_text("print('local')\n", encoding="utf-8")
            (package_dir / "remote" / "SKILL.md").write_text(
                "---\nname: Finance Central\ndescription: Central repository version\n---\n# Finance Central\n",
                encoding="utf-8",
            )
            helper = scripts / "openclaw-conflict-package.sh"
            helper.write_text(
                "#!/usr/bin/env bash\n"
                f"printf '{{\"ok\":true,\"total_conflicts\":1,\"packages\":[{{\"skill_id\":\"%s\",\"path\":\"{package_dir}\"}}]}}\\n' \"$1\"\n",
                encoding="utf-8",
            )
            os.chmod(helper, 0o755)

            result = run_openclaw_conflict_package(repo, ["finance-auto-bookkeeping"])

            self.assertTrue(result["ok"])
            self.assertEqual(result["mode"], "conflict_package")
            self.assertEqual(result["total_conflicts"], 1)
            self.assertEqual(result["packages"][0]["skill_id"], "finance-auto-bookkeeping")
            review = result["packages"][0]["review"]
            self.assertEqual(review["local"]["title"], "Finance Local")
            self.assertEqual(review["local"]["description"], "OpenClaw edited version")
            self.assertEqual(review["local"]["file_count"], 2)
            self.assertEqual(review["remote"]["title"], "Finance Central")
            self.assertEqual(review["remote"]["description"], "Central repository version")
            self.assertEqual(review["base"]["state"], "absent")
            self.assertIn("先比较 OpenClaw 版和共享仓库版", review["decision_hint"])
            self.assertIn("openclaw-conflict-package.sh finance-auto-bookkeeping", result["command"])

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
