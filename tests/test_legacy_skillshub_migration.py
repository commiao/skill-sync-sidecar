import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from skill_sync_sidecar.legacy_skillshub_migration import (
    LegacySkillshubMigrationError,
    build_legacy_skillshub_peer_report,
    build_legacy_skillshub_migration_preview,
    execute_legacy_skillshub_migration,
    rollback_legacy_skillshub_migration,
)
from skill_sync_sidecar.snapshot import write_snapshot
from skill_sync_sidecar.scanner import scan_roots


class LegacySkillshubMigrationTest(unittest.TestCase):
    def test_preview_distinguishes_matching_missing_and_changed_skills(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / ".skillshub"
            codex = root / ".codex" / "skills"
            claude = root / ".claude" / "skills"
            central = root / "central"
            source = root / "source"
            self._write_skill(legacy / "same", "same", "same body")
            self._write_skill(legacy / "missing", "missing", "missing body")
            self._write_skill(legacy / "changed", "changed", "local body")
            self._link(codex / "same", legacy / "same")
            self._link(codex / "missing", legacy / "missing")
            self._link(claude / "changed", legacy / "changed")
            self._write_skill(source / "same", "same", "same body", targets=["codex"])
            self._write_skill(source / "changed", "changed", "central body", targets=["claude-code"])
            write_snapshot(scan_roots([f"central={source}"]), central, "central-snapshot")

            preview = build_legacy_skillshub_migration_preview(
                legacy,
                central,
                consumer_roots={"codex": codex, "claude-code": claude},
            )

            self.assertTrue(preview["ok"])
            self.assertEqual(preview["summary"]["legacy_skills"], 3)
            self.assertEqual(preview["summary"]["central_missing"], 1)
            self.assertEqual(preview["summary"]["central_match"], 1)
            self.assertEqual(preview["summary"]["central_changed"], 1)
            self.assertEqual(preview["summary"]["detachable_links"], 1)
            by_id = {item["skill_id"]: item for item in preview["items"]}
            self.assertEqual(by_id["same"]["action"], "detach_legacy_links")
            self.assertEqual(by_id["missing"]["action"], "publish_to_central")
            self.assertEqual(by_id["changed"]["action"], "review_central_difference")
            self.assertEqual(by_id["same"]["links"][0]["tool_id"], "codex")

    def test_peer_report_keeps_the_migration_counts_without_local_paths(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / ".skillshub"
            codex = root / ".codex" / "skills"
            central = root / "central"
            source = root / "source"
            self._write_skill(legacy / "missing", "missing", "local body")
            self._link(codex / "missing", legacy / "missing")
            write_snapshot(scan_roots([f"central={source}"]), central, "central-snapshot")

            report = build_legacy_skillshub_peer_report(
                legacy,
                central,
                consumer_roots={"codex": codex},
                measured_at="2026-08-23T00:00:00+00:00",
            )

            self.assertTrue(report["available"])
            self.assertEqual(report["legacy_skillshub_report_version"], 1)
            self.assertEqual(report["snapshot_id"], "central-snapshot")
            self.assertEqual(report["summary"]["linked_skills"], 1)
            self.assertEqual(report["summary"]["linked_entries"], 1)
            self.assertEqual(report["summary"]["central_missing"], 1)
            self.assertEqual(report["items"][0]["tools"], ["codex"])
            self.assertNotIn("legacy_path", report["items"][0])
            self.assertNotIn(str(root), json.dumps(report, ensure_ascii=False))

            unavailable = build_legacy_skillshub_peer_report(root / "missing", central)
            self.assertFalse(unavailable["available"])
            self.assertNotIn(str(root), json.dumps(unavailable, ensure_ascii=False))

    def test_execute_replaces_only_matching_symlink_and_rollback_restores_link(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / ".skillshub"
            codex = root / ".codex" / "skills"
            central = root / "central"
            source = root / "source"
            record = root / "records" / "migration.json"
            self._write_skill(legacy / "same", "same", "same body")
            self._link(codex / "same", legacy / "same")
            self._write_skill(source / "same", "same", "same body", targets=["codex"])
            write_snapshot(scan_roots([f"central={source}"]), central, "central-snapshot")

            preview = execute_legacy_skillshub_migration(
                legacy,
                central,
                ["same"],
                consumer_roots={"codex": codex},
            )
            self.assertTrue(preview["dry_run"])
            self.assertTrue((codex / "same").is_symlink())

            result = execute_legacy_skillshub_migration(
                legacy,
                central,
                ["same"],
                consumer_roots={"codex": codex},
                record_out=record,
                yes=True,
                allow_local_writes=True,
            )

            target = codex / "same"
            self.assertTrue(result["ok"])
            self.assertEqual(result["migrated_links"], 1)
            self.assertFalse(target.is_symlink())
            self.assertTrue((target / "SKILL.md").exists())
            self.assertTrue((legacy / "same" / "SKILL.md").exists())
            backup = Path(result["applied"][0]["backup_link_path"])
            self.assertTrue(backup.is_symlink())
            self.assertEqual(backup.resolve(), (legacy / "same").resolve())
            self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["status"], "complete")

            rollback = rollback_legacy_skillshub_migration(record)

            self.assertTrue(rollback["ok"])
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), (legacy / "same").resolve())

    def test_execute_rejects_a_skill_that_is_not_verified_against_central(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / ".skillshub"
            codex = root / ".codex" / "skills"
            central = root / "central"
            self._write_skill(legacy / "missing", "missing", "local body")
            self._link(codex / "missing", legacy / "missing")
            write_snapshot(scan_roots([]), central, "central-snapshot")

            with self.assertRaisesRegex(LegacySkillshubMigrationError, "not safe to detach"):
                execute_legacy_skillshub_migration(
                    legacy,
                    central,
                    ["missing"],
                    consumer_roots={"codex": codex},
                    yes=True,
                    allow_local_writes=True,
                )

    def _write_skill(self, path: Path, skill_id: str, body: str, *, targets=None) -> None:
        path.mkdir(parents=True, exist_ok=True)
        (path / "SKILL.md").write_text(
            f"---\nname: {skill_id}\ndescription: {skill_id} skill\n---\n{body}\n",
            encoding="utf-8",
        )
        if targets is not None:
            (path / "manifest.json").write_text(
                json.dumps({"protocol_version": 0, "skill_id": skill_id, "scope": "global", "targets": targets}) + "\n",
                encoding="utf-8",
            )

    def _link(self, link: Path, target: Path) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, link, target_is_directory=True)


if __name__ == "__main__":
    unittest.main()
