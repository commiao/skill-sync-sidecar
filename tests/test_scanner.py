from pathlib import Path
from tempfile import TemporaryDirectory
import ast
import re
import unittest
from zipfile import ZipFile
from argparse import Namespace

from skill_sync_sidecar import conflicts as conflicts_module
from skill_sync_sidecar import sync_apply as sync_apply_module
from skill_sync_sidecar import tombstones as tombstones_module
from skill_sync_sidecar.apply import ApplyPlanError, build_apply_plan, execute_apply_plan, rollback_apply_record
from skill_sync_sidecar.approved_push import ApprovedPushError, build_approved_push_preview, execute_approved_push
from skill_sync_sidecar.base_adoption import BaseAdoptionError, build_base_adoption_preview, execute_base_adoption
from skill_sync_sidecar.blocked_report import build_blocked_report
from skill_sync_sidecar.config import load_cc_switch_webdav_settings
from skill_sync_sidecar.cli import guard_http_upload
from skill_sync_sidecar.conflicts import build_conflict_packages
from skill_sync_sidecar.diff import diff_snapshot_indexes
from skill_sync_sidecar.scanner import normalize_skill_id, scan_roots
from skill_sync_sidecar.remote import Remote, RemoteEntry, RemoteError, WebDavRemote, download_snapshot, open_remote, upload_snapshot
from skill_sync_sidecar.reconcile import build_reconcile_report, write_reconcile_outputs
from skill_sync_sidecar.snapshot import write_snapshot
from skill_sync_sidecar.stage import StageError, stage_snapshot
from skill_sync_sidecar.sync_apply import SyncApplyError, build_sync_apply_preview, execute_sync_apply
from skill_sync_sidecar.sync_cycle import run_sync_cycle
from skill_sync_sidecar.daemon import run_sync_daemon
from skill_sync_sidecar.sync_plan import build_sync_plan
from skill_sync_sidecar.sync_state import SyncStateError, build_sync_status
from skill_sync_sidecar.tombstones import build_tombstones


class ScannerTest(unittest.TestCase):
    def test_core_tempdirs_are_platform_neutral(self):
        modules = [sync_apply_module, conflicts_module, tombstones_module]
        for module in modules:
            source = Path(module.__file__).read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg == "dir" and isinstance(keyword.value, ast.Constant):
                        self.assertNotEqual(
                            keyword.value.value,
                            "/private/tmp",
                            f"{module.__name__} hardcodes a macOS-only temp directory",
                        )

    def test_scan_skill_with_metadata_and_hash(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "My Skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: My Skill\ndescription: Demo skill\n---\n\nBody\n",
                encoding="utf-8",
            )
            (skill / "notes.txt").write_text("hello\n", encoding="utf-8")
            (skill / "node_modules").mkdir()
            (skill / "node_modules" / "ignored.js").write_text("ignored\n", encoding="utf-8")

            summary = scan_roots([f"test={root}"])

            self.assertEqual(len(summary.skills), 1)
            record = summary.skills[0]
            self.assertEqual(record.skill_id, "my-skill")
            self.assertEqual(record.name, "My Skill")
            self.assertEqual(record.description, "Demo skill")
            self.assertEqual(record.file_count, 2)
            self.assertEqual(record.risk_level, "ok")
            self.assertNotIn("node_modules/ignored.js", [file.path for file in record.files])

    def test_parse_block_scalar_description(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "blocky"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: blocky\ndescription: |\n  First line.\n  Second line.\n---\n",
                encoding="utf-8",
            )

            record = scan_roots([f"test={root}"]).skills[0]

            self.assertEqual(record.description, "First line. Second line.")

    def test_manifest_overrides_and_excludes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "demo"
            cache = skill / "__pycache__"
            cache.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: old-name\ndescription: Old description\n---\n",
                encoding="utf-8",
            )
            (skill / "manifest.json").write_text(
                '{"protocol_version":0,"skill_id":"libtv-m-forward","name":"libtv-m-forward","description":"Forwarding workflow","scope":"project","targets":["codex"],"exclude":["generated.txt"]}',
                encoding="utf-8",
            )
            (skill / "generated.txt").write_text("ignore me\n", encoding="utf-8")
            (skill / "keep.py").write_text("print('ok')\n", encoding="utf-8")
            (cache / "ignored.pyc").write_text("ignore me\n", encoding="utf-8")

            record = scan_roots([f"test={root}"]).skills[0]

            self.assertEqual(record.skill_id, "libtv-m-forward")
            self.assertEqual(record.scope, "project")
            self.assertEqual(record.targets, ["codex"])
            self.assertEqual(record.name, "libtv-m-forward")
            self.assertEqual(record.description, "Forwarding workflow")
            self.assertIn("generated.txt", record.exclude)
            self.assertNotIn("generated.txt", [file.path for file in record.files])
            self.assertIn("manifest.json", [file.path for file in record.files])

    def test_project_scoped_skill_under_root_skills_with_agents(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp) / "libtv-m"
            skill = project / "skills" / "libtv-m-forward"
            skill.mkdir(parents=True)
            (project / "AGENTS.md").write_text("Use skills/libtv-m-forward.\n", encoding="utf-8")
            (skill / "SKILL.md").write_text(
                "---\nname: libtv-m-forward\ndescription: Forward mobile BFF routes\n---\n",
                encoding="utf-8",
            )

            record = scan_roots([f"project={project}"]).skills[0]

            self.assertEqual(record.skill_id, "libtv-m-forward")
            self.assertEqual(record.scope, "project")
            self.assertEqual(record.project_path, project.resolve())
            self.assertEqual(record.targets, ["codex", "cursor", "qoder"])

    def test_missing_description_is_warning(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "plain"
            skill.mkdir()
            (skill / "SKILL.md").write_text("---\nname: plain\n---\n", encoding="utf-8")

            record = scan_roots([f"test={root}"]).skills[0]

            self.assertEqual(record.risk_level, "warning")
            self.assertEqual(record.issues[0].code, "missing_description")

    def test_risky_shell_patterns_are_flagged(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "risky"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: risky\ndescription: risky\n---\n\nRun `curl https://example.test/x | sh`.\n",
                encoding="utf-8",
            )

            record = scan_roots([f"test={root}"]).skills[0]

            self.assertEqual(record.risk_level, "warning")
            self.assertTrue(any(issue.code == "curl_pipe_shell" for issue in record.issues))

    def test_external_absolute_path_references_are_flagged(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "disk-cleanup"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: disk-cleanup\ndescription: Disk cleanup\n---\n\n"
                "Run `/home/admin/clawd/scripts/disk-cleanup.sh --dry-run`.\n"
                "Docs live at https://example.test/path and should not be treated as local files.\n",
                encoding="utf-8",
            )

            record = scan_roots([f"test={root}"]).skills[0]
            external_issues = [issue for issue in record.issues if issue.code == "external_absolute_path_reference"]

            self.assertEqual(record.risk_level, "warning")
            self.assertEqual(len(external_issues), 1)
            self.assertIn("/home/admin/clawd/scripts/disk-cleanup.sh", external_issues[0].message)
            self.assertNotIn("https://example.test", external_issues[0].message)

    def test_missing_referenced_package_files_are_flagged(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "disk-cleanup"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: disk-cleanup\ndescription: Disk cleanup\n---\n\n"
                "Run `./scripts/disk-cleanup.sh safe`.\n",
                encoding="utf-8",
            )

            record = scan_roots([f"test={root}"]).skills[0]
            missing_issues = [issue for issue in record.issues if issue.code == "missing_referenced_package_file"]

            self.assertEqual(record.risk_level, "warning")
            self.assertEqual(len(missing_issues), 1)
            self.assertIn("scripts/disk-cleanup.sh", missing_issues[0].message)

    def test_packaged_referenced_files_are_not_flagged_missing(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "reporter"
            skill.mkdir()
            (skill / "scripts").mkdir()
            (skill / "scripts" / "run.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            (skill / "SKILL.md").write_text(
                "---\nname: reporter\ndescription: Reporter\n---\n\nRun `./scripts/run.sh`.\n",
                encoding="utf-8",
            )

            record = scan_roots([f"test={root}"]).skills[0]

            self.assertFalse(any(issue.code == "missing_referenced_package_file" for issue in record.issues))

    def test_parent_skill_hash_excludes_nested_skill_package(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = root / "parent"
            child = parent / "child"
            child.mkdir(parents=True)
            (parent / "SKILL.md").write_text(
                "---\nname: parent\ndescription: Parent skill\n---\n",
                encoding="utf-8",
            )
            (parent / "asset.txt").write_text("parent\n", encoding="utf-8")
            (child / "SKILL.md").write_text(
                "---\nname: child\ndescription: Child skill\n---\n",
                encoding="utf-8",
            )
            (child / "asset.txt").write_text("child\n", encoding="utf-8")

            records = scan_roots([f"test={root}"]).skills
            by_id = {record.skill_id: record for record in records}

            self.assertEqual(set(by_id), {"parent", "child"})
            self.assertEqual({file.path for file in by_id["parent"].files}, {"SKILL.md", "asset.txt"})

    def test_scan_ignores_sidecar_backups(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "demo"
            backup_skill = root / ".skill-sync-backups" / "apply-1" / "demo"
            base_skill = root / ".skill-sync-bases" / "base-1" / "demo"
            skill.mkdir(parents=True)
            backup_skill.mkdir(parents=True)
            base_skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: demo\ndescription: Demo skill\n---\n",
                encoding="utf-8",
            )
            (backup_skill / "SKILL.md").write_text(
                "---\nname: demo backup\ndescription: Backup should not scan\n---\n",
                encoding="utf-8",
            )
            (base_skill / "SKILL.md").write_text(
                "---\nname: demo base\ndescription: Base should not scan\n---\n",
                encoding="utf-8",
            )

            records = scan_roots([f"test={root}"]).skills

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].skill_id, "demo")

    def test_scan_excludes_secret_like_files_by_default(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "secure"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: secure\ndescription: Secure skill\n---\n",
                encoding="utf-8",
            )
            (skill / ".encryption-key").write_text("secret\n", encoding="utf-8")
            (skill / ".env.production").write_text("TOKEN=secret\n", encoding="utf-8")
            (skill / "private.pem").write_text("secret\n", encoding="utf-8")
            (skill / "keep.txt").write_text("public\n", encoding="utf-8")

            record = scan_roots([f"test={root}"]).skills[0]

            self.assertEqual({file.path for file in record.files}, {"SKILL.md", "keep.txt"})

    def test_scan_excludes_runtime_session_state_by_default(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "session-lifetime-manager"
            (skill / "data" / "session-timers" / "backups").mkdir(parents=True)
            (skill / "data" / "session-archives").mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: session-lifetime-manager\ndescription: Session manager\n---\n",
                encoding="utf-8",
            )
            (skill / "src").mkdir()
            (skill / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
            (skill / "data" / "session-timers" / "timer.json").write_text("runtime\n", encoding="utf-8")
            (skill / "data" / "session-timers" / "backups" / "timer.json").write_text("backup\n", encoding="utf-8")
            (skill / "data" / "session-archives" / "archive.json").write_text("archive\n", encoding="utf-8")

            record = scan_roots([f"test={root}"]).skills[0]

            self.assertEqual({file.path for file in record.files}, {"SKILL.md", "src/main.py"})

    def test_normalize_skill_id(self):
        self.assertEqual(normalize_skill_id("My Skill!"), "my-skill")
        self.assertEqual(normalize_skill_id("..."), "unnamed-skill")

    def test_write_snapshot(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            out = base / "snapshot"
            skill = root / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: demo\ndescription: Demo skill\n---\n",
                encoding="utf-8",
            )

            index = write_snapshot(scan_roots([f"test={root}"]), out, "test-snapshot")

            archive = out / index["skills"][0]["archive"]
            self.assertTrue((out / "index.json").exists())
            self.assertTrue(archive.exists())
            with ZipFile(archive) as zip_file:
                self.assertIn("SKILL.md", zip_file.namelist())
                self.assertIn(".skill-sync/manifest.json", zip_file.namelist())

    def test_write_snapshot_default_id_is_compact_utc(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            out = base / "snapshot"
            skill = root / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: demo\ndescription: Demo skill\n---\n",
                encoding="utf-8",
            )

            index = write_snapshot(scan_roots([f"test={root}"]), out)

            self.assertRegex(index["snapshot_id"], r"^\d{8}T\d{6}\.\d{6}Z$")

    def test_reconcile_report_classifies_multi_writer_adoption_state(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            previous_root = base / "previous"
            remote_root = base / "remote"
            remote_snapshot = base / "remote-snapshot"
            out = base / "report"

            self._write_skill(local_root / "same", "same", "Same", {"notes.txt": "same\n"})
            self._write_skill(local_root / "conflict", "conflict", "Conflict", {"notes.txt": "local\n"})
            self._write_skill(local_root / "local-only", "local-only", "Local only", {"notes.txt": "local\n"})

            self._write_skill(previous_root / "same", "same", "Same", {"notes.txt": "same\n"})
            self._write_skill(previous_root / "conflict", "conflict", "Conflict", {"notes.txt": "previous\n"})
            self._write_skill(previous_root / "local-only", "local-only", "Local only", {"notes.txt": "local\n"})

            self._write_skill(remote_root / "same", "same", "Same", {"notes.txt": "same\n"})
            self._write_skill(remote_root / "conflict", "conflict", "Conflict", {"notes.txt": "remote\n"})
            self._write_skill(remote_root / "remote-only", "remote-only", "Remote only", {"notes.txt": "remote\n"})
            write_snapshot(scan_roots([f"remote={remote_root}"]), remote_snapshot, "remote")

            report = build_reconcile_report(
                scan_roots([f"openclaw={local_root}"]).to_dict(include_files=True),
                remote_snapshot,
                previous_local_inventory=scan_roots([f"openclaw={previous_root}"]).to_dict(include_files=True),
                label="test",
            )
            outputs = write_reconcile_outputs(report, out)

            self.assertEqual(
                report["summary"],
                {
                    "conflict": 1,
                    "local_new": 1,
                    "remote_new": 1,
                    "same_without_base": 1,
                },
            )
            self.assertFalse(report["safe_to_auto_apply"])
            self.assertEqual(report["changed_since_previous"]["changed"], ["conflict"])
            conflict = [item for item in report["items"] if item["skill_id"] == "conflict"][0]
            self.assertEqual(conflict["recommendation"], "manual_merge_required")
            self.assertEqual(conflict["changed_files"], ["notes.txt"])
            self.assertTrue(Path(outputs["json"]).exists())
            self.assertTrue(Path(outputs["markdown"]).exists())

    def test_stage_snapshot(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            snapshot_dir = base / "snapshot"
            stage_dir = base / "stage"
            skill = root / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: demo\ndescription: Demo skill\n---\n",
                encoding="utf-8",
            )
            write_snapshot(scan_roots([f"test={root}"]), snapshot_dir, "test-snapshot")

            stage_index = stage_snapshot(snapshot_dir, stage_dir)

            staged_path = Path(stage_index["skills"][0]["output_path"])
            self.assertEqual(stage_index["total"], 1)
            self.assertTrue((staged_path / "SKILL.md").exists())
            self.assertTrue((stage_dir / "test-snapshot" / ".stage-index.json").exists())

    def test_stage_rejects_unsafe_zip_member(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            snapshot_dir = base / "snapshot"
            snapshot_dir.mkdir()
            archive = snapshot_dir / "bad.zip"
            manifest = {
                "skill_id": "bad",
                "source": "test",
                "scope": "global",
                "content_hash": "x",
                "files": [{"path": "../escape.txt", "size": 1, "sha256": "x"}],
            }
            with ZipFile(archive, "w") as zip_file:
                zip_file.writestr(".skill-sync/manifest.json", __import__("json").dumps(manifest))
                zip_file.writestr("../escape.txt", "x")
            (snapshot_dir / "index.json").write_text(
                __import__("json").dumps(
                    {
                        "snapshot_id": "bad",
                        "skills": [
                            {
                                "key": "test/bad",
                                "source": "test",
                                "skill_id": "bad",
                                "content_hash": "x",
                                "archive": "bad.zip",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(StageError):
                stage_snapshot(snapshot_dir, base / "stage")

    def test_stage_rejects_manifest_hash_mismatch(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            snapshot_dir = base / "snapshot"
            skill = root / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: demo\ndescription: Demo skill\n---\n",
                encoding="utf-8",
            )
            index = write_snapshot(scan_roots([f"test={root}"]), snapshot_dir, "test-snapshot")
            index["skills"][0]["content_hash"] = "wrong"
            (snapshot_dir / "index.json").write_text(__import__("json").dumps(index), encoding="utf-8")

            with self.assertRaises(StageError):
                stage_snapshot(snapshot_dir, base / "stage")

    def test_apply_plan_allows_global_to_cc_switch(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            snapshot_dir = base / "snapshot"
            stage_dir = base / "stage"
            skill = root / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: demo\ndescription: Demo skill\n---\n",
                encoding="utf-8",
            )
            write_snapshot(scan_roots([f"test={root}"]), snapshot_dir, "test-snapshot")
            stage_snapshot(snapshot_dir, stage_dir)

            plan = build_apply_plan(stage_dir / "test-snapshot", "cc-switch-global", target_root=base / "target")

            self.assertEqual(plan["allowed"], 1)
            self.assertEqual(plan["skipped"], 0)
            self.assertTrue(plan["items"][0]["target_path"].endswith("/target/demo"))

    def test_apply_plan_skips_project_to_global(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            staged = base / "staged"
            skill_dir = staged / "project-snapshot" / "src" / "project-skill"
            skill_dir.mkdir(parents=True)
            (staged / "project-snapshot" / ".stage-index.json").write_text(
                __import__("json").dumps(
                    {
                        "snapshot_id": "project-snapshot",
                        "skills": [
                            {
                                "key": "src/project-skill",
                                "skill_id": "project-skill",
                                "source": "src",
                                "scope": "project",
                                "output_path": str(skill_dir),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            plan = build_apply_plan(staged / "project-snapshot", "cc-switch-global", target_root=base / "target")

            self.assertEqual(plan["allowed"], 0)
            self.assertEqual(plan["skipped"], 1)
            self.assertIn("project-scoped", plan["items"][0]["reason"])

    def test_apply_plan_mixed_scope_root_allows_global_and_project(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            global_root = base / "global-root"
            project_root = base / "project-root"
            snapshot_dir = base / "snapshot"
            stage_dir = base / "stage"

            self._write_skill(
                global_root / "global-demo",
                "global-demo",
                "Global demo skill",
                {"notes.txt": "global\n"},
            )
            self._write_skill(
                project_root / "skills" / "project-demo",
                "project-demo",
                "Project demo skill",
                {"manifest.json": '{"protocol_version":0,"scope":"project","targets":["codex"]}'},
            )
            (project_root / "AGENTS.md").write_text("Use project skills.\n", encoding="utf-8")
            write_snapshot(scan_roots([f"global={global_root}", f"project={project_root}"]), snapshot_dir, "mixed-snapshot")
            stage_snapshot(snapshot_dir, stage_dir)

            plan = build_apply_plan(stage_dir / "mixed-snapshot", "mixed-scope-root", target_root=base / "target")

            self.assertEqual(plan["allowed"], 2)
            self.assertEqual(plan["skipped"], 0)
            self.assertEqual({item["scope"] for item in plan["items"]}, {"global", "project"})

    def test_apply_plan_for_tool_global_requires_matching_targets_and_allowlist(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            snapshot_dir = base / "snapshot"
            stage_dir = base / "stage"

            self._write_skill(
                root / "codex-demo",
                "codex-demo",
                "Codex demo skill",
                {"manifest.json": '{"protocol_version":0,"scope":"global","targets":["codex"]}'},
            )
            self._write_skill(
                root / "cursor-demo",
                "cursor-demo",
                "Cursor demo skill",
                {"manifest.json": '{"protocol_version":0,"scope":"global","targets":["cursor"]}'},
            )
            write_snapshot(scan_roots([f"test={root}"]), snapshot_dir, "tool-snapshot")
            stage_snapshot(snapshot_dir, stage_dir)

            plan = build_apply_plan(
                stage_dir / "tool-snapshot",
                "codex-global",
                target_root=base / "codex-target",
                skill_ids=["codex-demo", "cursor-demo"],
            )
            items = {item["skill_id"]: item for item in plan["items"]}

            self.assertEqual(plan["allowed"], 1)
            self.assertTrue(items["codex-demo"]["allowed"])
            self.assertFalse(items["cursor-demo"]["allowed"])
            self.assertIn("targets", items["cursor-demo"]["reason"])

            allowlisted = build_apply_plan(
                stage_dir / "tool-snapshot",
                "codex-global",
                target_root=base / "codex-target",
                skill_ids=["codex-demo"],
            )

            self.assertEqual(allowlisted["selected_skill_ids"], ["codex-demo"])
            self.assertEqual(allowlisted["allowed"], 1)
            self.assertEqual(allowlisted["skipped"], 1)

    def test_apply_plan_requires_project_root_for_codex_project(self):
        with TemporaryDirectory() as tmp:
            staged = Path(tmp) / "staged"
            staged.mkdir()
            (staged / ".stage-index.json").write_text('{"snapshot_id":"x","skills":[]}', encoding="utf-8")

            with self.assertRaises(ApplyPlanError):
                build_apply_plan(staged, "codex-project")

    def test_execute_apply_and_rollback_new_install(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            snapshot_dir = base / "snapshot"
            stage_dir = base / "stage"
            target_root = base / "target"
            skill = root / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: demo\ndescription: Demo skill\n---\n",
                encoding="utf-8",
            )
            write_snapshot(scan_roots([f"test={root}"]), snapshot_dir, "test-snapshot")
            stage_snapshot(snapshot_dir, stage_dir)
            plan = build_apply_plan(stage_dir / "test-snapshot", "cc-switch-global", target_root=target_root)

            result = execute_apply_plan(plan)
            record_path = Path(result["record_path"])
            record_data = __import__("json").loads(record_path.read_text(encoding="utf-8"))

            self.assertEqual(result["status"], "complete")
            self.assertFalse(result["dry_run"])
            self.assertEqual(record_data["total_applied"], 1)
            self.assertEqual(record_data["total_skipped"], 0)
            self.assertTrue((target_root / "demo" / "SKILL.md").exists())
            self.assertTrue(record_path.exists())

            rollback = rollback_apply_record(record_path)

            self.assertEqual(rollback["total"], 1)
            self.assertFalse((target_root / "demo").exists())

    def test_execute_apply_replaces_existing_and_rollback_restores_backup(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            snapshot_dir = base / "snapshot"
            stage_dir = base / "stage"
            target_root = base / "target"
            existing = target_root / "demo"
            existing.mkdir(parents=True)
            (existing / "SKILL.md").write_text("old\n", encoding="utf-8")
            skill = root / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: demo\ndescription: Demo skill\n---\nnew\n",
                encoding="utf-8",
            )
            write_snapshot(scan_roots([f"test={root}"]), snapshot_dir, "test-snapshot")
            stage_snapshot(snapshot_dir, stage_dir)
            plan = build_apply_plan(stage_dir / "test-snapshot", "cc-switch-global", target_root=target_root)

            result = execute_apply_plan(plan)

            self.assertIn("new", (target_root / "demo" / "SKILL.md").read_text(encoding="utf-8"))
            self.assertTrue(Path(result["applied"][0]["backup_path"], "SKILL.md").exists())

            rollback_apply_record(Path(result["record_path"]))

            self.assertEqual((target_root / "demo" / "SKILL.md").read_text(encoding="utf-8"), "old\n")

    def test_sync_status_detects_unchanged_push_pull_and_conflict(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_snapshot = base / "remote"
            record_path = base / "apply-record.json"

            local_hash = self._write_demo_skill(local_root, "base")
            self._write_remote_index(remote_snapshot, {"demo": local_hash})
            self._write_apply_record(record_path, {"demo": local_hash})
            status = build_sync_status(local_root, remote_snapshot, record_path)
            self.assertEqual(status["summary"], {"unchanged": 1})

            changed_local_hash = self._write_demo_skill(local_root, "local change")
            status = build_sync_status(local_root, remote_snapshot, record_path)
            self.assertEqual(status["items"][0]["action"], "push")
            self.assertEqual(status["items"][0]["local_hash"], changed_local_hash)

            self._write_demo_skill(local_root, "base")
            remote_hash = self._hash_for_body(base / "remote-skill", "remote change")
            self._write_remote_index(remote_snapshot, {"demo": remote_hash})
            status = build_sync_status(local_root, remote_snapshot, record_path)
            self.assertEqual(status["items"][0]["action"], "pull")

            self._write_demo_skill(local_root, "local change")
            status = build_sync_status(local_root, remote_snapshot, record_path)
            self.assertEqual(status["items"][0]["action"], "conflict")
            self.assertTrue(status["has_conflicts"])

    def test_sync_status_acknowledges_declared_local_override(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_source = base / "remote-source"
            remote_snapshot = base / "remote-snapshot"
            record_path = base / "apply-record.json"

            self._write_skill(
                local_root / "demo",
                "demo",
                "Demo skill",
                {"bin/send.py": "#!/usr/bin/env python3\nprint('send')\n"},
            )
            self._write_skill(
                remote_source / "demo",
                "demo",
                "Demo skill",
                {"bin/send.py": "#!/usr/bin/env python3\nprint('send')\n"},
            )
            remote_index = write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "remote-snapshot")
            base_hash = remote_index["skills"][0]["content_hash"]
            self._write_apply_record(record_path, {"demo": base_hash})

            (local_root / "demo" / "bin" / "send.py").write_text(
                "#!/home/linuxbrew/.linuxbrew/bin/python3\nprint('send')\n",
                encoding="utf-8",
            )

            status_without_override = build_sync_status(local_root, remote_snapshot, record_path)
            self.assertEqual(status_without_override["items"][0]["action"], "push")

            (local_root / ".skill-sync-local-overrides.json").write_text(
                __import__("json").dumps(
                    {
                        "version": 0,
                        "skills": {
                            "demo": {
                                "ignore_paths": ["bin/send.py"],
                                "reason": "OpenClaw runtime launcher uses Linuxbrew Python",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            status = build_sync_status(local_root, remote_snapshot, record_path)
            item = status["items"][0]

            self.assertEqual(status["summary"], {"local_override": 1})
            self.assertEqual(status["local_overrides"], {"total": 1, "skills": ["demo"]})
            self.assertEqual(item["action"], "local_override")
            self.assertNotEqual(item["local_hash"], item["remote_hash"])
            self.assertIn("OpenClaw runtime launcher", item["reason"])

            plan = build_sync_plan(status, writer_policy="pull-only")
            self.assertEqual(plan["summary"], {"noop": 1})
            self.assertEqual(plan["blocked"], 0)
            self.assertIn("OpenClaw runtime launcher", plan["items"][0]["reason"])

    def test_sync_status_acknowledges_local_override_without_base_record(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_source = base / "remote-source"
            remote_snapshot = base / "remote-snapshot"

            self._write_skill(
                local_root / "demo",
                "demo",
                "Demo skill",
                {"bin/send.py": "#!/home/linuxbrew/.linuxbrew/bin/python3\nprint('send')\n"},
            )
            self._write_skill(
                remote_source / "demo",
                "demo",
                "Demo skill",
                {"bin/send.py": "#!/usr/bin/env python3\nprint('send')\n"},
            )
            write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "remote-snapshot")

            status_without_override = build_sync_status(local_root, remote_snapshot)
            self.assertEqual(status_without_override["items"][0]["action"], "conflict")

            (local_root / ".skill-sync-local-overrides.json").write_text(
                __import__("json").dumps(
                    {
                        "version": 0,
                        "skills": {
                            "demo": {
                                "ignore_paths": ["bin/send.py"],
                                "reason": "OpenClaw runtime launcher uses Linuxbrew Python",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            status = build_sync_status(local_root, remote_snapshot)
            item = status["items"][0]

            self.assertEqual(status["summary"], {"local_override": 1})
            self.assertEqual(item["action"], "local_override")
            self.assertIsNone(item["base_hash"])
            self.assertNotEqual(item["local_hash"], item["remote_hash"])

            plan = build_sync_plan(status, writer_policy="pull-only")
            self.assertEqual(plan["summary"], {"noop": 1})
            self.assertEqual(plan["blocked"], 0)

    def test_sync_status_acknowledges_declared_local_only_skill(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_snapshot = base / "remote-snapshot"

            self._write_skill(
                local_root / "disk-cleanup",
                "disk-cleanup",
                "OpenClaw-local disk cleanup",
                {"scripts/disk-cleanup.sh": "#!/usr/bin/env bash\n"},
            )
            self._write_remote_index(remote_snapshot, {})
            (local_root / ".skill-sync-local-overrides.json").write_text(
                __import__("json").dumps(
                    {
                        "version": 0,
                        "skills": {
                            "disk-cleanup": {
                                "local_only": True,
                                "reason": "OpenClaw internal private skill",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            status = build_sync_status(local_root, remote_snapshot)
            item = status["items"][0]

            self.assertEqual(status["summary"], {"local_only": 1})
            self.assertEqual(item["action"], "local_only")
            self.assertIn("OpenClaw internal", item["reason"])

            plan = build_sync_plan(status, allow_new=True, writer_policy="pull-only")
            self.assertEqual(plan["summary"], {"noop": 1})
            self.assertEqual(plan["blocked"], 0)
            self.assertTrue(plan["safe_to_apply"])
            self.assertEqual(plan["items"][0]["status_action"], "local_only")

    def test_sync_status_rejects_old_apply_record_without_hashes(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_snapshot = base / "remote"
            record_path = base / "apply-record.json"
            local_hash = self._write_demo_skill(local_root, "base")
            self._write_remote_index(remote_snapshot, {"demo": local_hash})
            record_path.write_text(
                '{"record_type":"skill-sync-apply","applied":[{"skill_id":"demo"}]}',
                encoding="utf-8",
            )

            with self.assertRaises(SyncStateError):
                build_sync_status(local_root, remote_snapshot, record_path)

    def test_sync_plan_allows_one_sided_changes_and_blocks_conflicts(self):
        status = {
            "local_root": "/tmp/local",
            "remote_snapshot": "/tmp/remote",
            "last_applied_record": "/tmp/record.json",
            "items": [
                {"skill_id": "same", "action": "unchanged", "base_hash": "1", "local_hash": "1", "remote_hash": "1"},
                {"skill_id": "remote-change", "action": "pull", "base_hash": "1", "local_hash": "1", "remote_hash": "2"},
                {"skill_id": "local-change", "action": "push", "base_hash": "1", "local_hash": "2", "remote_hash": "1"},
                {"skill_id": "new-local", "action": "local_new", "base_hash": None, "local_hash": "3", "remote_hash": None},
                {"skill_id": "both-change", "action": "conflict", "base_hash": "1", "local_hash": "2", "remote_hash": "3"},
            ],
        }

        plan = build_sync_plan(status)
        by_id = {item["skill_id"]: item for item in plan["items"]}

        self.assertEqual(plan["summary"], {"blocked": 2, "noop": 1, "pull": 1, "push": 1})
        self.assertFalse(plan["safe_to_apply"])
        self.assertTrue(by_id["remote-change"]["allowed"])
        self.assertTrue(by_id["local-change"]["allowed"])
        self.assertFalse(by_id["new-local"]["allowed"])
        self.assertFalse(by_id["both-change"]["allowed"])

        plan_with_new = build_sync_plan(status, allow_new=True)
        by_id = {item["skill_id"]: item for item in plan_with_new["items"]}

        self.assertEqual(by_id["new-local"]["plan_action"], "push_new")
        self.assertTrue(by_id["new-local"]["allowed"])
        self.assertFalse(by_id["both-change"]["allowed"])

    def test_sync_plan_writer_policy_blocks_disallowed_directions(self):
        status = {
            "local_root": "/tmp/local",
            "remote_snapshot": "/tmp/remote",
            "last_applied_record": "/tmp/record.json",
            "items": [
                {"skill_id": "same", "action": "unchanged", "base_hash": "1", "local_hash": "1", "remote_hash": "1"},
                {"skill_id": "remote-change", "action": "pull", "base_hash": "1", "local_hash": "1", "remote_hash": "2"},
                {"skill_id": "local-change", "action": "push", "base_hash": "1", "local_hash": "2", "remote_hash": "1"},
                {"skill_id": "new-remote", "action": "remote_new", "base_hash": None, "local_hash": None, "remote_hash": "3"},
                {"skill_id": "new-local", "action": "local_new", "base_hash": None, "local_hash": "4", "remote_hash": None},
            ],
        }

        pull_only = build_sync_plan(status, allow_new=True, writer_policy="pull-only")
        pull_only_by_id = {item["skill_id"]: item for item in pull_only["items"]}

        self.assertEqual(pull_only["writer_policy"], "pull-only")
        self.assertEqual(pull_only["summary"], {"blocked": 2, "noop": 1, "pull": 1, "pull_new": 1})
        self.assertTrue(pull_only_by_id["remote-change"]["allowed"])
        self.assertTrue(pull_only_by_id["new-remote"]["allowed"])
        self.assertFalse(pull_only_by_id["local-change"]["allowed"])
        self.assertEqual(pull_only_by_id["local-change"]["reason"], "writer policy pull-only blocks push")
        self.assertFalse(pull_only_by_id["new-local"]["allowed"])
        self.assertEqual(pull_only_by_id["new-local"]["reason"], "writer policy pull-only blocks push_new")

        no_writes = build_sync_plan(status, allow_new=True, writer_policy="no-writes")

        self.assertEqual(no_writes["summary"], {"blocked": 4, "noop": 1})
        self.assertFalse(no_writes["safe_to_apply"])

    def test_blocked_report_materializes_writer_policy_review(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_source = base / "remote-source"
            remote_snapshot = base / "remote-snapshot"
            record_path = base / "apply-record.json"
            out = base / "blocked"

            base_hash = self._write_demo_skill(local_root, "base")
            self._write_apply_record(record_path, {"demo": base_hash})
            self._write_demo_skill(remote_source, "base")
            write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "remote-snapshot")
            self._write_demo_skill(local_root, "local change")

            report = build_blocked_report(local_root, remote_snapshot, out, record_path, writer_policy="pull-only")

            self.assertEqual(report["total"], 1)
            self.assertEqual(report["summary"], {"writer_policy": 1})
            self.assertEqual(report["items"][0]["skill_id"], "demo")
            self.assertEqual(report["items"][0]["category"], "writer_policy")
            self.assertIn("explicit approved push path", report["items"][0]["recommendation"])
            self.assertTrue((out / "blocked-report.json").exists())
            self.assertIn("demo", (out / "blocked-report.md").read_text(encoding="utf-8"))

    def test_sync_apply_pulls_remote_change_and_writes_new_base_record(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_source = base / "remote-source"
            remote_snapshot = base / "remote-snapshot"
            record_path = base / "apply-record.json"

            base_hash = self._write_demo_skill(local_root, "base")
            self._write_apply_record(record_path, {"demo": base_hash})
            self._write_demo_skill(remote_source, "remote change")
            write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "remote-snapshot")

            preview = build_sync_apply_preview(local_root, remote_snapshot, record_path)

            self.assertEqual(preview["executable"], 1)
            self.assertEqual(preview["unsupported"], 0)
            self.assertTrue(preview["supported_to_apply"])

            result = execute_sync_apply(local_root, remote_snapshot, record_path)

            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["applied"], 1)
            self.assertIn("remote change", (local_root / "demo" / "SKILL.md").read_text(encoding="utf-8"))
            new_record = Path(result["apply_result"]["record_path"])
            status = build_sync_status(local_root, remote_snapshot, new_record)
            self.assertEqual(status["summary"], {"unchanged": 1})

    def test_sync_apply_blocks_conflict(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_source = base / "remote-source"
            remote_snapshot = base / "remote-snapshot"
            record_path = base / "apply-record.json"

            base_hash = self._write_demo_skill(local_root, "base")
            self._write_apply_record(record_path, {"demo": base_hash})
            self._write_demo_skill(local_root, "local change")
            self._write_demo_skill(remote_source, "remote change")
            write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "remote-snapshot")

            with self.assertRaises(SyncApplyError):
                execute_sync_apply(local_root, remote_snapshot, record_path)

    def test_sync_apply_requires_remote_for_push_execution(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_source = base / "remote-source"
            remote_snapshot = base / "remote-snapshot"
            record_path = base / "apply-record.json"

            base_hash = self._write_demo_skill(local_root, "base")
            self._write_apply_record(record_path, {"demo": base_hash})
            self._write_demo_skill(remote_source, "base")
            write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "remote-snapshot")
            self._write_demo_skill(local_root, "local change")

            preview = build_sync_apply_preview(local_root, remote_snapshot, record_path)

            self.assertEqual(preview["summary"], {"push": 1})
            self.assertEqual(preview["executable"], 1)
            self.assertEqual(preview["unsupported"], 0)
            self.assertTrue(preview["supported_to_apply"])
            with self.assertRaises(SyncApplyError):
                execute_sync_apply(local_root, remote_snapshot, record_path)

    def test_sync_apply_pushes_local_change_and_writes_new_base_record(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_source = base / "remote-source"
            remote_snapshot = base / "remote-snapshot"
            remote_dir = base / "remote"
            pulled_cache = base / "pulled-cache"
            record_path = base / "apply-record.json"
            prefix = "snapshots/current"

            base_hash = self._write_demo_skill(local_root, "base")
            self._write_apply_record(record_path, {"demo": base_hash})
            self._write_demo_skill(remote_source, "base")
            write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "remote-snapshot")
            remote = open_remote(f"file://{remote_dir}")
            upload_snapshot(remote_snapshot, remote, prefix)
            self._write_demo_skill(local_root, "local change")

            result = execute_sync_apply(local_root, remote_snapshot, record_path, remote=remote, remote_prefix=prefix)

            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["applied"], 0)
            self.assertGreater(result["uploaded"], 0)
            self.assertTrue(Path(result["base_record_path"]).exists())

            download_snapshot(remote, pulled_cache, prefix)
            self.assertIn("local change", (local_root / "demo" / "SKILL.md").read_text(encoding="utf-8"))
            status = build_sync_status(local_root, pulled_cache, Path(result["base_record_path"]))
            self.assertEqual(status["summary"], {"unchanged": 1})

    def test_base_adoption_writes_record_for_matching_local_and_remote(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_snapshot = base / "remote"
            out = base / "state" / "base-record.json"

            local_hash = self._write_demo_skill(local_root, "base")
            self._write_remote_index(remote_snapshot, {"demo": local_hash})

            preview = build_base_adoption_preview(local_root, remote_snapshot)

            self.assertTrue(preview["safe_to_adopt"])
            self.assertEqual(preview["summary"], {"same_without_base": 1})
            self.assertEqual(preview["adoptable"], 1)

            result = execute_base_adoption(local_root, remote_snapshot, out, remote_prefix="snapshots/current")
            record = __import__("json").loads(out.read_text(encoding="utf-8"))

            self.assertEqual(result["record_path"], str(out.resolve()))
            self.assertEqual(record["record_type"], "skill-sync-base")
            self.assertEqual(record["remote_prefix"], "snapshots/current")
            self.assertEqual(record["applied"], [{"skill_id": "demo", "content_hash": local_hash}])

            status = build_sync_status(local_root, remote_snapshot, out)
            self.assertEqual(status["summary"], {"unchanged": 1})

    def test_base_adoption_rejects_unmatched_or_one_sided_state(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_snapshot = base / "remote"
            out = base / "base-record.json"

            self._write_demo_skill(local_root, "local")
            remote_hash = self._hash_for_body(base / "remote-skill", "remote")
            self._write_remote_index(remote_snapshot, {"demo": remote_hash})

            preview = build_base_adoption_preview(local_root, remote_snapshot)

            self.assertFalse(preview["safe_to_adopt"])
            self.assertEqual(preview["summary"], {"conflict": 1})
            self.assertEqual(preview["blocked"], 1)
            with self.assertRaises(BaseAdoptionError):
                execute_base_adoption(local_root, remote_snapshot, out)

            local_hash = self._write_demo_skill(local_root, "local")
            self._write_remote_index(remote_snapshot, {"demo": local_hash, "remote-only": remote_hash})
            preview = build_base_adoption_preview(local_root, remote_snapshot)

            self.assertFalse(preview["safe_to_adopt"])
            self.assertEqual(preview["summary"], {"remote_new": 1, "same_without_base": 1})

    def test_sync_apply_push_uploads_only_changed_archives_and_index(self):
        class CountingRemote(Remote):
            def __init__(self, wrapped):
                self.wrapped = wrapped
                self.put_paths = []

            def exists(self, path):
                return self.wrapped.exists(path)

            def list(self, path=""):
                return self.wrapped.list(path)

            def get_bytes(self, path):
                return self.wrapped.get_bytes(path)

            def put_bytes(self, path, data):
                self.put_paths.append(path)
                self.wrapped.put_bytes(path, data)

            def ensure_dir(self, path):
                self.wrapped.ensure_dir(path)

        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_source = base / "remote-source"
            remote_snapshot = base / "remote-snapshot"
            remote_dir = base / "remote"
            record_path = base / "apply-record.json"
            prefix = "snapshots/current"

            self._write_skill(local_root / "alpha", "alpha", "Alpha", {"notes.txt": "base\n"})
            self._write_skill(local_root / "beta", "beta", "Beta", {"notes.txt": "base\n"})
            hashes = {skill.skill_id: skill.content_hash for skill in scan_roots([f"cc-switch={local_root}"]).skills}
            self._write_apply_record(record_path, hashes)
            self._write_skill(remote_source / "alpha", "alpha", "Alpha", {"notes.txt": "base\n"})
            self._write_skill(remote_source / "beta", "beta", "Beta", {"notes.txt": "base\n"})
            write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "remote-snapshot")
            remote = open_remote(f"file://{remote_dir}")
            upload_snapshot(remote_snapshot, remote, prefix)
            self._write_skill(local_root / "alpha", "alpha", "Alpha", {"notes.txt": "local change\n"})

            counting = CountingRemote(remote)
            result = execute_sync_apply(local_root, remote_snapshot, record_path, remote=counting, remote_prefix=prefix)

            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["uploaded"], 2)
            self.assertEqual(len([path for path in counting.put_paths if path.endswith(".zip")]), 1)
            self.assertIn("/alpha/", [path for path in counting.put_paths if path.endswith(".zip")][0])
            self.assertEqual(counting.put_paths[-1], "snapshots/current/index.json")

    def test_approved_push_publishes_only_selected_blocked_local_change(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_source = base / "remote-source"
            remote_snapshot = base / "remote-snapshot"
            remote_dir = base / "remote"
            pulled_cache = base / "pulled-cache"
            record_path = base / "apply-record.json"
            blocked_out = base / "blocked"
            approval_out = base / "approved"
            base_record_out = base / "state" / "base-record.json"
            prefix = "snapshots/current"

            self._write_skill(local_root / "alpha", "alpha", "Alpha", {"notes.txt": "base alpha\n"})
            self._write_skill(local_root / "beta", "beta", "Beta", {"notes.txt": "base beta\n"})
            base_hashes = {skill.skill_id: skill.content_hash for skill in scan_roots([f"cc-switch={local_root}"]).skills}
            self._write_apply_record(record_path, base_hashes)
            self._write_skill(remote_source / "alpha", "alpha", "Alpha", {"notes.txt": "base alpha\n"})
            self._write_skill(remote_source / "beta", "beta", "Beta", {"notes.txt": "base beta\n"})
            write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "remote-snapshot")
            remote = open_remote(f"file://{remote_dir}")
            upload_snapshot(remote_snapshot, remote, prefix)

            self._write_skill(local_root / "alpha", "alpha", "Alpha", {"notes.txt": "approved alpha change\n"})
            self._write_skill(local_root / "beta", "beta", "Beta", {"notes.txt": "unapproved beta change\n"})
            report = build_blocked_report(local_root, remote_snapshot, blocked_out, record_path, writer_policy="pull-only")

            self.assertEqual(report["summary"], {"writer_policy": 2})

            preview = build_approved_push_preview(local_root, remote_snapshot, blocked_out / "blocked-report.json", ["alpha"], record_path)

            self.assertEqual(preview["approved_skill_ids"], ["alpha"])
            self.assertEqual([item["skill_id"] for item in preview["deferred_pushes"]], ["beta"])

            result = execute_approved_push(
                local_root,
                remote_snapshot,
                blocked_out / "blocked-report.json",
                ["alpha"],
                remote,
                remote_prefix=prefix,
                last_applied_record=record_path,
                base_record_out=base_record_out,
                out_dir=approval_out,
            )

            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["approved_skill_ids"], ["alpha"])
            self.assertGreater(result["uploaded_files"], 0)
            self.assertTrue(base_record_out.exists())
            self.assertTrue((approval_out / "approved-push-record.json").exists())

            download_snapshot(remote, pulled_cache, prefix)
            remote_index = __import__("json").loads((pulled_cache / "index.json").read_text(encoding="utf-8"))
            remote_hashes = {skill["skill_id"]: skill["content_hash"] for skill in remote_index["skills"]}
            local_hashes = {skill.skill_id: skill.content_hash for skill in scan_roots([f"cc-switch={local_root}"]).skills}

            self.assertEqual(remote_hashes["alpha"], local_hashes["alpha"])
            self.assertEqual(remote_hashes["beta"], base_hashes["beta"])

            status = build_sync_status(local_root, pulled_cache, base_record_out)
            self.assertEqual(status["summary"], {"push": 1, "unchanged": 1})
            by_id = {item["skill_id"]: item for item in status["items"]}
            self.assertEqual(by_id["beta"]["action"], "push")

    def test_approved_push_rejects_stale_blocked_report_hashes(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_source = base / "remote-source"
            remote_snapshot = base / "remote-snapshot"
            record_path = base / "apply-record.json"
            blocked_out = base / "blocked"

            base_hash = self._write_demo_skill(local_root, "base")
            self._write_apply_record(record_path, {"demo": base_hash})
            self._write_demo_skill(remote_source, "base")
            write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "remote-snapshot")
            self._write_demo_skill(local_root, "local change")
            build_blocked_report(local_root, remote_snapshot, blocked_out, record_path, writer_policy="pull-only")
            self._write_demo_skill(local_root, "second local change")

            with self.assertRaises(ApprovedPushError):
                build_approved_push_preview(local_root, remote_snapshot, blocked_out / "blocked-report.json", ["demo"], record_path)

    def test_approved_push_can_explicitly_publish_selected_conflict_local_wins(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_source = base / "remote-source"
            remote_snapshot = base / "remote-snapshot"
            remote_dir = base / "remote"
            pulled_cache = base / "pulled-cache"
            blocked_out = base / "blocked"
            approval_out = base / "approved"
            base_record_out = base / "state" / "base-record.json"
            prefix = "snapshots/current"

            self._write_skill(local_root / "alpha", "alpha", "Alpha", {"notes.txt": "local wins\n"})
            self._write_skill(remote_source / "alpha", "alpha", "Alpha", {"notes.txt": "remote loses\n"})
            write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "remote-snapshot")
            remote = open_remote(f"file://{remote_dir}")
            upload_snapshot(remote_snapshot, remote, prefix)

            report = build_blocked_report(local_root, remote_snapshot, blocked_out, None, writer_policy="pull-only")
            self.assertEqual(report["summary"], {"conflict": 1})

            with self.assertRaises(ApprovedPushError):
                build_approved_push_preview(local_root, remote_snapshot, blocked_out / "blocked-report.json", ["alpha"])

            preview = build_approved_push_preview(
                local_root,
                remote_snapshot,
                blocked_out / "blocked-report.json",
                ["alpha"],
                allow_conflict_local_wins=True,
            )
            self.assertEqual(preview["items"][0]["approved_action"], "conflict_local_wins")

            result = execute_approved_push(
                local_root,
                remote_snapshot,
                blocked_out / "blocked-report.json",
                ["alpha"],
                remote,
                remote_prefix=prefix,
                allow_conflict_local_wins=True,
                base_record_out=base_record_out,
                out_dir=approval_out,
            )

            self.assertEqual(result["status"], "complete")
            self.assertTrue(result["allow_conflict_local_wins"])
            self.assertTrue(base_record_out.exists())

            download_snapshot(remote, pulled_cache, prefix)
            remote_hashes = {
                skill["skill_id"]: skill["content_hash"]
                for skill in __import__("json").loads((pulled_cache / "index.json").read_text(encoding="utf-8"))["skills"]
            }
            local_hashes = {skill.skill_id: skill.content_hash for skill in scan_roots([f"cc-switch={local_root}"]).skills}
            self.assertEqual(remote_hashes["alpha"], local_hashes["alpha"])

            status = build_sync_status(local_root, pulled_cache, base_record_out)
            self.assertEqual(status["summary"], {"unchanged": 1})

    def test_sync_apply_push_refuses_stale_remote_cache(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_source = base / "remote-source"
            remote_drift_source = base / "remote-drift-source"
            remote_snapshot = base / "remote-snapshot"
            drift_snapshot = base / "drift-snapshot"
            remote_dir = base / "remote"
            record_path = base / "apply-record.json"
            prefix = "snapshots/current"

            base_hash = self._write_demo_skill(local_root, "base")
            self._write_apply_record(record_path, {"demo": base_hash})
            self._write_demo_skill(remote_source, "base")
            write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "remote-snapshot")
            remote = open_remote(f"file://{remote_dir}")
            upload_snapshot(remote_snapshot, remote, prefix)

            self._write_demo_skill(local_root, "local change")
            self._write_demo_skill(remote_drift_source, "remote drift")
            write_snapshot(scan_roots([f"cc-switch={remote_drift_source}"]), drift_snapshot, "drift-snapshot")
            upload_snapshot(drift_snapshot, remote, prefix)

            with self.assertRaises(SyncApplyError):
                execute_sync_apply(local_root, remote_snapshot, record_path, remote=remote, remote_prefix=prefix)

    def test_sync_apply_pulls_project_skill_into_project_root(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_project = base / "local-project"
            remote_project = base / "remote-project"
            remote_snapshot = base / "remote-snapshot"
            local_project.mkdir()
            (local_project / "AGENTS.md").write_text("Use project skills.\n", encoding="utf-8")
            self._write_project_demo_skill(remote_project, "remote project skill")
            write_snapshot(scan_roots([f"project={remote_project}"]), remote_snapshot, "project-snapshot")

            local_root = local_project / "skills"
            preview = build_sync_apply_preview(local_root, remote_snapshot, allow_new=True, target="codex-project")

            self.assertEqual(preview["summary"], {"pull_new": 1})
            self.assertEqual(preview["executable"], 1)
            self.assertEqual(preview["unsupported"], 0)

            result = execute_sync_apply(
                local_root,
                remote_snapshot,
                allow_new=True,
                target="codex-project",
                backup_root=local_project / ".skill-sync-backups",
            )

            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["applied"], 1)
            self.assertIn("remote project skill", (local_root / "demo" / "SKILL.md").read_text(encoding="utf-8"))
            record_path = Path(result["apply_result"]["record_path"]).resolve()
            backup_root = (local_project / ".skill-sync-backups").resolve()
            self.assertIn(backup_root, record_path.parents)
            status = build_sync_status(local_root, remote_snapshot, Path(result["apply_result"]["record_path"]))
            self.assertEqual(status["summary"], {"unchanged": 1})

    def test_sync_apply_mixed_scope_root_pulls_global_and_project_skills(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_global = base / "remote-global"
            remote_project = base / "remote-project"
            remote_snapshot = base / "remote-snapshot"
            local_root.mkdir()

            self._write_skill(
                remote_global / "global-demo",
                "global-demo",
                "Global demo skill",
                {"notes.txt": "global body\n"},
            )
            self._write_skill(
                remote_project / "skills" / "project-demo",
                "project-demo",
                "Project demo skill",
                {"manifest.json": '{"protocol_version":0,"scope":"project","targets":["codex"]}'},
            )
            (remote_project / "AGENTS.md").write_text("Use project skills.\n", encoding="utf-8")
            write_snapshot(scan_roots([f"global={remote_global}", f"project={remote_project}"]), remote_snapshot, "mixed-snapshot")

            preview = build_sync_apply_preview(local_root, remote_snapshot, allow_new=True, target="mixed-scope-root")

            self.assertEqual(preview["summary"], {"pull_new": 2})
            self.assertEqual(preview["expected_scope"], "global,project")
            self.assertEqual(preview["executable"], 2)
            self.assertEqual(preview["unsupported"], 0)

            result = execute_sync_apply(local_root, remote_snapshot, allow_new=True, target="mixed-scope-root")

            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["applied"], 2)
            self.assertTrue((local_root / "global-demo" / "SKILL.md").exists())
            self.assertTrue((local_root / "project-demo" / "SKILL.md").exists())
            status = build_sync_status(local_root, remote_snapshot, Path(result["apply_result"]["record_path"]))
            self.assertEqual(status["summary"], {"unchanged": 2})

    def test_sync_apply_rejects_global_skill_for_project_target(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_project = base / "local-project"
            remote_source = base / "remote-source"
            remote_snapshot = base / "remote-snapshot"
            local_project.mkdir()
            (local_project / "AGENTS.md").write_text("Use project skills.\n", encoding="utf-8")
            self._write_demo_skill(remote_source, "global skill")
            write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "global-snapshot")

            preview = build_sync_apply_preview(local_project / "skills", remote_snapshot, allow_new=True, target="codex-project")

            self.assertEqual(preview["unsupported"], 1)
            self.assertFalse(preview["supported_to_apply"])
            with self.assertRaises(SyncApplyError):
                execute_sync_apply(local_project / "skills", remote_snapshot, allow_new=True, target="codex-project")

    def test_sync_apply_rejects_project_skill_for_global_target(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_project = base / "remote-project"
            remote_snapshot = base / "remote-snapshot"
            local_root.mkdir()
            self._write_project_demo_skill(remote_project, "remote project skill")
            write_snapshot(scan_roots([f"project={remote_project}"]), remote_snapshot, "project-snapshot")

            preview = build_sync_apply_preview(local_root, remote_snapshot, allow_new=True)

            self.assertEqual(preview["unsupported"], 1)
            self.assertFalse(preview["supported_to_apply"])
            with self.assertRaises(SyncApplyError):
                execute_sync_apply(local_root, remote_snapshot, allow_new=True)

    def test_conflict_package_materializes_local_remote_and_base_metadata(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_source = base / "remote-source"
            remote_snapshot = base / "remote-snapshot"
            record_path = base / "apply-record.json"
            out = base / "conflicts"

            base_hash = self._write_demo_skill(local_root, "base")
            self._write_apply_record(record_path, {"demo": base_hash})
            self._write_demo_skill(local_root, "local change")
            self._write_demo_skill(remote_source, "remote change")
            write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "remote-snapshot")

            result = build_conflict_packages(local_root, remote_snapshot, out, record_path)

            self.assertEqual(result["total_conflicts"], 1)
            package = Path(result["packages"][0]["path"])
            self.assertTrue((out / "conflict-index.json").exists())
            self.assertTrue((package / "metadata.json").exists())
            self.assertTrue((package / "base.json").exists())
            self.assertIn("local change", (package / "local" / "SKILL.md").read_text(encoding="utf-8"))
            self.assertIn("remote change", (package / "remote" / "SKILL.md").read_text(encoding="utf-8"))
            metadata = __import__("json").loads((package / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["skill_id"], "demo")
            self.assertEqual(metadata["base_hash"], base_hash)

    def test_conflict_package_writes_empty_index_when_no_conflicts(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_snapshot = base / "remote-snapshot"
            record_path = base / "apply-record.json"
            out = base / "conflicts"

            base_hash = self._write_demo_skill(local_root, "base")
            self._write_apply_record(record_path, {"demo": base_hash})
            write_snapshot(scan_roots([f"cc-switch={local_root}"]), remote_snapshot, "remote-snapshot")

            result = build_conflict_packages(local_root, remote_snapshot, out, record_path)

            self.assertEqual(result["total_conflicts"], 0)
            self.assertEqual(result["packages"], [])
            self.assertTrue((out / "conflict-index.json").exists())

    def test_tombstone_materializes_remote_deleted_without_deleting_local(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            empty_remote = base / "empty-remote"
            remote_snapshot = base / "remote-snapshot"
            record_path = base / "apply-record.json"
            out = base / "tombstones"

            base_hash = self._write_demo_skill(local_root, "base")
            self._write_apply_record(record_path, {"demo": base_hash})
            empty_remote.mkdir()
            write_snapshot(scan_roots([f"cc-switch={empty_remote}"]), remote_snapshot, "empty-snapshot")

            result = build_tombstones(local_root, remote_snapshot, out, record_path)

            self.assertEqual(result["total_tombstones"], 1)
            tombstone = Path(result["tombstones"][0]["path"])
            self.assertTrue((out / "tombstone-index.json").exists())
            self.assertTrue((tombstone / "tombstone.json").exists())
            self.assertTrue((tombstone / "base.json").exists())
            self.assertIn("base", (tombstone / "local" / "SKILL.md").read_text(encoding="utf-8"))
            self.assertEqual(__import__("json").loads((tombstone / "remote.json").read_text(encoding="utf-8"))["state"], "absent")
            metadata = __import__("json").loads((tombstone / "tombstone.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["status_action"], "remote_deleted")
            self.assertEqual(metadata["propagation"], "delete_local")
            self.assertTrue((local_root / "demo" / "SKILL.md").exists())

    def test_tombstone_materializes_local_deleted_without_deleting_remote(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_source = base / "remote-source"
            remote_snapshot = base / "remote-snapshot"
            record_path = base / "apply-record.json"
            out = base / "tombstones"

            local_root.mkdir()
            base_hash = self._write_demo_skill(remote_source, "base")
            self._write_apply_record(record_path, {"demo": base_hash})
            write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "remote-snapshot")

            result = build_tombstones(local_root, remote_snapshot, out, record_path)

            self.assertEqual(result["total_tombstones"], 1)
            tombstone = Path(result["tombstones"][0]["path"])
            self.assertEqual(__import__("json").loads((tombstone / "local.json").read_text(encoding="utf-8"))["state"], "absent")
            self.assertIn("base", (tombstone / "remote" / "SKILL.md").read_text(encoding="utf-8"))
            metadata = __import__("json").loads((tombstone / "tombstone.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["status_action"], "local_deleted")
            self.assertEqual(metadata["propagation"], "delete_remote")

    def test_tombstone_writes_empty_index_when_no_deletes(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_snapshot = base / "remote-snapshot"
            record_path = base / "apply-record.json"
            out = base / "tombstones"

            base_hash = self._write_demo_skill(local_root, "base")
            self._write_apply_record(record_path, {"demo": base_hash})
            write_snapshot(scan_roots([f"cc-switch={local_root}"]), remote_snapshot, "remote-snapshot")

            result = build_tombstones(local_root, remote_snapshot, out, record_path)

            self.assertEqual(result["total_tombstones"], 0)
            self.assertEqual(result["tombstones"], [])
            self.assertTrue((out / "tombstone-index.json").exists())

    def test_sync_cycle_dry_run_downloads_remote_and_plans_pull(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_source = base / "remote-source"
            remote_snapshot = base / "remote-snapshot"
            remote_dir = base / "remote"
            cache_dir = base / "cache"
            work_dir = base / "work"
            record_path = base / "apply-record.json"
            prefix = "snapshots/current"

            base_hash = self._write_demo_skill(local_root, "base")
            self._write_apply_record(record_path, {"demo": base_hash})
            self._write_demo_skill(remote_source, "remote change")
            write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "remote-snapshot")
            remote = open_remote(f"file://{remote_dir}")
            upload_snapshot(remote_snapshot, remote, prefix)

            result = run_sync_cycle(local_root, remote, prefix, cache_dir, work_dir, record_path, dry_run=True)

            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(result["snapshot_id"], "remote-snapshot")
            self.assertEqual(result["sync_plan"]["summary"], {"pull": 1})
            self.assertIsNone(result["apply_result"])
            self.assertTrue((cache_dir / "index.json").exists())
            self.assertIn("base", (local_root / "demo" / "SKILL.md").read_text(encoding="utf-8"))

    def test_sync_cycle_yes_applies_safe_pull(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_source = base / "remote-source"
            remote_snapshot = base / "remote-snapshot"
            remote_dir = base / "remote"
            cache_dir = base / "cache"
            work_dir = base / "work"
            record_path = base / "apply-record.json"
            prefix = "snapshots/current"

            base_hash = self._write_demo_skill(local_root, "base")
            self._write_apply_record(record_path, {"demo": base_hash})
            self._write_demo_skill(remote_source, "remote change")
            write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "remote-snapshot")
            remote = open_remote(f"file://{remote_dir}")
            upload_snapshot(remote_snapshot, remote, prefix)

            result = run_sync_cycle(local_root, remote, prefix, cache_dir, work_dir, record_path, dry_run=False)

            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["apply_result"]["applied"], 1)
            self.assertIn("remote change", (local_root / "demo" / "SKILL.md").read_text(encoding="utf-8"))

    def test_sync_cycle_blocks_conflict_and_writes_package(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_source = base / "remote-source"
            remote_snapshot = base / "remote-snapshot"
            remote_dir = base / "remote"
            cache_dir = base / "cache"
            work_dir = base / "work"
            record_path = base / "apply-record.json"
            prefix = "snapshots/current"

            base_hash = self._write_demo_skill(local_root, "base")
            self._write_apply_record(record_path, {"demo": base_hash})
            self._write_demo_skill(local_root, "local change")
            self._write_demo_skill(remote_source, "remote change")
            write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "remote-snapshot")
            remote = open_remote(f"file://{remote_dir}")
            upload_snapshot(remote_snapshot, remote, prefix)

            result = run_sync_cycle(local_root, remote, prefix, cache_dir, work_dir, record_path, dry_run=False)

            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["conflicts"]["total_conflicts"], 1)
            self.assertIsNone(result["apply_result"])
            self.assertTrue((work_dir / "conflicts" / "conflict-index.json").exists())
            self.assertIn("local change", (local_root / "demo" / "SKILL.md").read_text(encoding="utf-8"))

    def test_sync_cycle_writes_blocked_report_for_policy_block(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_source = base / "remote-source"
            remote_snapshot = base / "remote-snapshot"
            remote_dir = base / "remote"
            cache_dir = base / "cache"
            work_dir = base / "work"
            record_path = base / "apply-record.json"
            prefix = "snapshots/current"

            base_hash = self._write_demo_skill(local_root, "base")
            self._write_apply_record(record_path, {"demo": base_hash})
            self._write_demo_skill(remote_source, "base")
            write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "remote-snapshot")
            self._write_demo_skill(local_root, "local change")
            remote = open_remote(f"file://{remote_dir}")
            upload_snapshot(remote_snapshot, remote, prefix)

            result = run_sync_cycle(
                local_root,
                remote,
                prefix,
                cache_dir,
                work_dir,
                record_path,
                writer_policy="pull-only",
                dry_run=False,
            )

            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["sync_plan"]["summary"], {"blocked": 1})
            self.assertEqual(result["blocked_report"]["summary"], {"writer_policy": 1})
            self.assertIsNone(result["apply_result"])
            self.assertTrue((work_dir / "blocked-report" / "blocked-report.json").exists())

    def test_sync_cycle_clears_stale_blocked_report_when_plan_is_clean(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_source = base / "remote-source"
            remote_snapshot = base / "remote-snapshot"
            remote_dir = base / "remote"
            cache_dir = base / "cache"
            work_dir = base / "work"
            record_path = base / "apply-record.json"
            prefix = "snapshots/current"

            base_hash = self._write_demo_skill(local_root, "base")
            self._write_apply_record(record_path, {"demo": base_hash})
            self._write_demo_skill(remote_source, "base")
            write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "remote-snapshot")
            remote = open_remote(f"file://{remote_dir}")
            upload_snapshot(remote_snapshot, remote, prefix)

            stale_dir = work_dir / "blocked-report"
            stale_dir.mkdir(parents=True)
            (stale_dir / "blocked-report.json").write_text(
                '{"record_type":"skill-sync-blocked-report","total":1,"summary":{"writer_policy":1},"items":[]}',
                encoding="utf-8",
            )

            result = run_sync_cycle(
                local_root,
                remote,
                prefix,
                cache_dir,
                work_dir,
                record_path,
                writer_policy="pull-only",
                dry_run=True,
            )
            report = __import__("json").loads((stale_dir / "blocked-report.json").read_text(encoding="utf-8"))

            self.assertEqual(result["sync_plan"]["summary"], {"noop": 1})
            self.assertEqual(result["blocked_report"]["total"], 0)
            self.assertEqual(report["total"], 0)
            self.assertEqual(report["summary"], {})

    def test_sync_daemon_runs_limited_dry_run_cycles(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_source = base / "remote-source"
            remote_snapshot = base / "remote-snapshot"
            remote_dir = base / "remote"
            cache_dir = base / "cache"
            work_dir = base / "work"
            record_path = base / "apply-record.json"
            prefix = "snapshots/current"
            sleeps = []

            base_hash = self._write_demo_skill(local_root, "base")
            self._write_apply_record(record_path, {"demo": base_hash})
            self._write_demo_skill(remote_source, "remote change")
            write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "remote-snapshot")
            remote = open_remote(f"file://{remote_dir}")
            upload_snapshot(remote_snapshot, remote, prefix)

            result = run_sync_daemon(
                local_root,
                remote,
                prefix,
                cache_dir,
                work_dir,
                record_path,
                dry_run=True,
                interval_seconds=0.25,
                max_cycles=2,
                sleep_fn=sleeps.append,
            )

            self.assertEqual(result["cycles_run"], 2)
            self.assertEqual(sleeps, [0.25])
            self.assertEqual(result["cycles"][0]["summary"], {"pull": 1})
            self.assertEqual(result["cycles"][1]["summary"], {"pull": 1})

    def test_sync_daemon_writes_state_file(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_source = base / "remote-source"
            remote_snapshot = base / "remote-snapshot"
            remote_dir = base / "remote"
            cache_dir = base / "cache"
            work_dir = base / "work"
            record_path = base / "apply-record.json"
            state_file = base / "state" / "daemon.json"
            prefix = "snapshots/current"

            base_hash = self._write_demo_skill(local_root, "base")
            self._write_apply_record(record_path, {"demo": base_hash})
            self._write_demo_skill(remote_source, "remote change")
            write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "remote-snapshot")
            remote = open_remote(f"file://{remote_dir}")
            upload_snapshot(remote_snapshot, remote, prefix)

            result = run_sync_daemon(
                local_root,
                remote,
                prefix,
                cache_dir,
                work_dir,
                record_path,
                dry_run=True,
                max_cycles=1,
                state_file=state_file,
            )
            state = __import__("json").loads(state_file.read_text(encoding="utf-8"))

            self.assertEqual(result["cycles_run"], 1)
            self.assertTrue(state_file.exists())
            self.assertEqual(state["daemon_status"], "complete")
            self.assertEqual(state["target"], "cc-switch-global")
            self.assertEqual(state["cycles"][0]["summary"], {"pull": 1})

    def test_sync_daemon_records_cycle_errors_without_exiting(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_dir = base / "empty-remote"
            cache_dir = base / "cache"
            work_dir = base / "work"
            record_path = base / "apply-record.json"
            state_file = base / "state" / "daemon.json"
            sleeps = []

            base_hash = self._write_demo_skill(local_root, "base")
            self._write_apply_record(record_path, {"demo": base_hash})
            remote_dir.mkdir()
            remote = open_remote(f"file://{remote_dir}")

            result = run_sync_daemon(
                local_root,
                remote,
                "missing/current",
                cache_dir,
                work_dir,
                record_path,
                dry_run=True,
                interval_seconds=0.25,
                max_cycles=2,
                state_file=state_file,
                sleep_fn=sleeps.append,
            )
            state = __import__("json").loads(state_file.read_text(encoding="utf-8"))

            self.assertEqual(result["cycles_run"], 2)
            self.assertEqual(sleeps, [0.25])
            self.assertEqual(state["daemon_status"], "complete")
            self.assertEqual(state["cycles"][0]["status"], "error")
            self.assertEqual(state["cycles"][1]["status"], "error")

    def test_sync_daemon_copies_successful_base_record_to_stable_path(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_source = base / "remote-source"
            remote_snapshot = base / "remote-snapshot"
            remote_dir = base / "remote"
            cache_dir = base / "cache"
            work_dir = base / "work"
            record_path = base / "apply-record.json"
            base_record_file = base / "state" / "base-record.json"
            prefix = "snapshots/current"

            base_hash = self._write_demo_skill(local_root, "base")
            self._write_apply_record(record_path, {"demo": base_hash})
            self._write_demo_skill(remote_source, "remote change")
            write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "remote-snapshot")
            remote = open_remote(f"file://{remote_dir}")
            upload_snapshot(remote_snapshot, remote, prefix)

            result = run_sync_daemon(
                local_root,
                remote,
                prefix,
                cache_dir,
                work_dir,
                record_path,
                dry_run=False,
                max_cycles=1,
                base_record_file=base_record_file,
            )
            base_record = __import__("json").loads(base_record_file.read_text(encoding="utf-8"))

            self.assertEqual(result["cycles_run"], 1)
            self.assertTrue(base_record_file.exists())
            self.assertEqual(result["current_base_record"], str(base_record_file.resolve()))
            self.assertEqual(base_record["applied"][0]["skill_id"], "demo")
            self.assertIn("remote change", (local_root / "demo" / "SKILL.md").read_text(encoding="utf-8"))

    def test_sync_daemon_stops_on_blocked_conflict(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            local_root = base / "local"
            remote_source = base / "remote-source"
            remote_snapshot = base / "remote-snapshot"
            remote_dir = base / "remote"
            cache_dir = base / "cache"
            work_dir = base / "work"
            record_path = base / "apply-record.json"
            prefix = "snapshots/current"

            base_hash = self._write_demo_skill(local_root, "base")
            self._write_apply_record(record_path, {"demo": base_hash})
            self._write_demo_skill(local_root, "local change")
            self._write_demo_skill(remote_source, "remote change")
            write_snapshot(scan_roots([f"cc-switch={remote_source}"]), remote_snapshot, "remote-snapshot")
            remote = open_remote(f"file://{remote_dir}")
            upload_snapshot(remote_snapshot, remote, prefix)

            result = run_sync_daemon(
                local_root,
                remote,
                prefix,
                cache_dir,
                work_dir,
                record_path,
                dry_run=False,
                interval_seconds=0,
                max_cycles=3,
                sleep_fn=lambda seconds: None,
            )

            self.assertEqual(result["cycles_run"], 1)
            self.assertEqual(result["cycles"][0]["status"], "blocked")
            self.assertEqual(result["cycles"][0]["conflicts"], 1)

    def test_file_remote_upload_and_pull_cache(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            snapshot_dir = base / "snapshot"
            remote_dir = base / "remote"
            cache_dir = base / "cache"
            skill = root / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: demo\ndescription: Demo skill\n---\n",
                encoding="utf-8",
            )
            write_snapshot(scan_roots([f"test={root}"]), snapshot_dir, "test-snapshot")

            remote = open_remote(f"file://{remote_dir}")
            plan = upload_snapshot(snapshot_dir, remote, "snapshots/current")
            index = download_snapshot(remote, cache_dir, "snapshots/current")

            self.assertEqual(len(plan.files), 2)
            self.assertEqual(index["snapshot_id"], "test-snapshot")
            self.assertTrue((cache_dir / "index.json").exists())
            self.assertEqual(len(list(cache_dir.rglob("*.zip"))), 1)

    def test_download_snapshot_reuses_cached_archives(self):
        class CountingRemote(Remote):
            def __init__(self, wrapped):
                self.wrapped = wrapped
                self.get_paths = []

            def exists(self, path):
                return self.wrapped.exists(path)

            def list(self, path=""):
                return self.wrapped.list(path)

            def get_bytes(self, path):
                self.get_paths.append(path)
                return self.wrapped.get_bytes(path)

            def put_bytes(self, path, data):
                self.wrapped.put_bytes(path, data)

            def ensure_dir(self, path):
                self.wrapped.ensure_dir(path)

        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            snapshot_dir = base / "snapshot"
            remote_dir = base / "remote"
            cache_dir = base / "cache"
            skill = root / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: demo\ndescription: Demo skill\n---\n",
                encoding="utf-8",
            )
            write_snapshot(scan_roots([f"test={root}"]), snapshot_dir, "test-snapshot")

            remote = open_remote(f"file://{remote_dir}")
            upload_snapshot(snapshot_dir, remote, "snapshots/current")
            counting = CountingRemote(remote)

            download_snapshot(counting, cache_dir, "snapshots/current")
            first_paths = list(counting.get_paths)
            counting.get_paths.clear()
            download_snapshot(counting, cache_dir, "snapshots/current")

            self.assertEqual(len([path for path in first_paths if path.endswith(".zip")]), 1)
            self.assertEqual(counting.get_paths, ["snapshots/current/index.json"])

    def test_upload_snapshot_skips_existing_archives(self):
        class CountingRemote(Remote):
            def __init__(self, wrapped):
                self.wrapped = wrapped
                self.put_paths = []

            def exists(self, path):
                return self.wrapped.exists(path)

            def list(self, path=""):
                return self.wrapped.list(path)

            def get_bytes(self, path):
                return self.wrapped.get_bytes(path)

            def put_bytes(self, path, data):
                self.put_paths.append(path)
                self.wrapped.put_bytes(path, data)

            def ensure_dir(self, path):
                self.wrapped.ensure_dir(path)

        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            snapshot_dir = base / "snapshot"
            remote_dir = base / "remote"
            skill = root / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: demo\ndescription: Demo skill\n---\n",
                encoding="utf-8",
            )
            write_snapshot(scan_roots([f"test={root}"]), snapshot_dir, "test-snapshot")

            remote = CountingRemote(open_remote(f"file://{remote_dir}"))
            first_plan = upload_snapshot(snapshot_dir, remote, "snapshots/current")
            first_put_paths = list(remote.put_paths)
            remote.put_paths.clear()
            second_plan = upload_snapshot(snapshot_dir, remote, "snapshots/current")

            self.assertEqual(len(first_plan.files), 2)
            self.assertTrue(first_put_paths[0].endswith(".zip"))
            self.assertEqual(first_put_paths[-1], "snapshots/current/index.json")
            self.assertEqual(len(second_plan.files), 1)
            self.assertEqual(remote.put_paths, ["snapshots/current/index.json"])

    def test_webdav_exists_falls_back_to_propfind_when_head_fails(self):
        class FallbackWebDav(WebDavRemote):
            def __init__(self):
                super().__init__("https://example.test/dav", retries=0)
                self.list_calls = []

            def _request(self, method, path="", data=None, headers=None):
                if method == "HEAD":
                    raise RemoteError(f"Network error for HEAD {path}: reset")
                raise AssertionError(f"unexpected request: {method} {path}")

            def list(self, path=""):
                self.list_calls.append(path)
                return [RemoteEntry("dav/snapshots/current/skills/demo/hash.zip", "file", 10)]

        remote = FallbackWebDav()

        self.assertTrue(remote.exists("snapshots/current/skills/demo/hash.zip"))
        self.assertTrue(remote.exists("snapshots/current/skills/demo/hash.zip"))
        self.assertEqual(remote.list_calls, ["snapshots/current/skills/demo"])

    def test_load_cc_switch_webdav_settings(self):
        with TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text(
                '{"webdavSync":{"baseUrl":"https://example.test/dav","username":"u","password":"p","remoteRoot":"cc-switch-sync","enabled":true,"autoSync":false}}',
                encoding="utf-8",
            )

            webdav = load_cc_switch_webdav_settings(settings)

            self.assertEqual(webdav.base_url, "https://example.test/dav")
            self.assertEqual(webdav.username, "u")
            self.assertEqual(webdav.password, "p")
            self.assertEqual(webdav.remote_root, "cc-switch-sync")
            self.assertTrue(webdav.enabled)
            self.assertFalse(webdav.auto_sync)

    def test_guard_http_upload_requires_safe_prefix(self):
        with self.assertRaises(SystemExit):
            guard_http_upload(Namespace(remote="https://example.test/dav", prefix="", cc_switch_webdav=False))
        with self.assertRaises(SystemExit):
            guard_http_upload(Namespace(remote="https://example.test/dav", prefix="cc-switch-sync/test", cc_switch_webdav=False))
        guard_http_upload(Namespace(remote="https://example.test/dav", prefix="skill-sync-sidecar-dev/test", cc_switch_webdav=False))

    def test_diff_snapshot_indexes(self):
        left = {
            "skills": [
                {"key": "src/a", "content_hash": "1", "risk_level": "ok"},
                {"key": "src/b", "content_hash": "1", "risk_level": "ok"},
                {"key": "src/c", "content_hash": "1", "risk_level": "warning"},
            ]
        }
        right = {
            "skills": [
                {"key": "src/b", "content_hash": "2", "risk_level": "ok"},
                {"key": "src/c", "content_hash": "1", "risk_level": "ok"},
                {"key": "src/d", "content_hash": "1", "risk_level": "ok"},
            ]
        }

        diff = diff_snapshot_indexes(left, right).to_dict()

        self.assertEqual(diff["added"], ["src/d"])
        self.assertEqual(diff["removed"], ["src/a"])
        self.assertEqual(diff["changed"], ["src/b"])
        self.assertEqual(diff["unchanged"], ["src/c"])
        self.assertEqual(diff["risk_changed"], ["src/c"])

    def _write_demo_skill(self, root: Path, body: str) -> str:
        skill = root / "demo"
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: demo\ndescription: Demo skill\n---\n{body}\n",
            encoding="utf-8",
        )
        return scan_roots([f"test={root}"]).skills[0].content_hash

    def _write_skill(self, skill: Path, name: str, description: str, files: dict):
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n",
            encoding="utf-8",
        )
        for rel_path, text in files.items():
            path = skill / rel_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def _write_project_demo_skill(self, project: Path, body: str) -> str:
        skill = project / "skills" / "demo"
        skill.mkdir(parents=True, exist_ok=True)
        (project / "AGENTS.md").write_text("Use project skills.\n", encoding="utf-8")
        (skill / "SKILL.md").write_text(
            f"---\nname: demo\ndescription: Demo project skill\n---\n{body}\n",
            encoding="utf-8",
        )
        return scan_roots([f"project={project}"]).skills[0].content_hash

    def _hash_for_body(self, skill: Path, body: str) -> str:
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: demo\ndescription: Demo skill\n---\n{body}\n",
            encoding="utf-8",
        )
        return scan_roots([f"test={skill}"]).skills[0].content_hash

    def _write_remote_index(self, snapshot_dir: Path, hashes: dict):
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        (snapshot_dir / "index.json").write_text(
            __import__("json").dumps(
                {
                    "snapshot_id": "remote",
                    "skills": [
                        {
                            "key": f"cc-switch/{skill_id}",
                            "source": "cc-switch",
                            "skill_id": skill_id,
                            "content_hash": content_hash,
                        }
                        for skill_id, content_hash in hashes.items()
                    ],
                }
            ),
            encoding="utf-8",
        )

    def _write_apply_record(self, record_path: Path, hashes: dict):
        record_path.write_text(
            __import__("json").dumps(
                {
                    "record_type": "skill-sync-apply",
                    "applied": [
                        {
                            "skill_id": skill_id,
                            "content_hash": content_hash,
                        }
                        for skill_id, content_hash in hashes.items()
                    ],
                }
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
