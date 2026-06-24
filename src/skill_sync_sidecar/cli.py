from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional, Sequence, Tuple

from . import __version__
from .apply import ApplyError, ApplyPlanError, build_apply_plan, execute_apply_plan, rollback_apply_record
from .approved_push import ApprovedPushError, build_approved_push_preview, execute_approved_push, write_approved_push_preview
from .base_adoption import BaseAdoptionError, build_base_adoption_preview, execute_base_adoption
from .blocked_report import build_blocked_report
from .config import ConfigError, load_cc_switch_webdav_settings
from .conflicts import ConflictPackageError, build_conflict_packages
from .dashboard import DashboardConfig, serve_dashboard
from .daemon import run_sync_daemon
from .diff import diff_snapshot_dirs
from .hub_import import (
    HubImportDiagnosisError,
    build_hub_import_diagnosis,
    build_hub_import_preview_package,
    parse_hub_source_spec,
    render_hub_import_diagnosis_text,
    render_hub_import_preview_text,
)
from .openclaw_gate import build_openclaw_gate, render_openclaw_gate_text
from .ops_status import build_ops_status, render_ops_status_text
from .projection import ProjectionError, build_tool_projection, parse_tool_adapter_spec
from .remote import RemoteError, build_upload_plan, download_snapshot, open_remote, upload_snapshot
from .reconcile import ReconcileError, build_reconcile_report, load_inventory, write_reconcile_outputs
from .scanner import scan_roots
from .snapshot import write_snapshot
from .stage import StageError, stage_snapshot
from .sync_apply import SyncApplyError, build_sync_apply_preview, execute_sync_apply
from .sync_cycle import run_sync_cycle
from .sync_plan import WRITER_POLICIES, build_sync_plan
from .sync_state import SyncStateError, build_sync_status
from .tombstones import TombstoneError, build_tombstones


APPLY_TARGETS = [
    "cc-switch-global",
    "skillshub-global",
    "codex-global",
    "cursor-global",
    "claude-code-global",
    "codex-project",
    "mixed-scope-root",
]
TOOL_GLOBAL_TARGETS = {"skillshub-global", "codex-global", "cursor-global", "claude-code-global"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skill-sync",
        description="WebDAV-backed sidecar for scanning and validating agent skills.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subcommands = parser.add_subparsers(dest="command", required=True)

    scan = subcommands.add_parser("scan", help="Scan local skill roots and emit a normalized inventory.")
    add_common_scan_args(scan)
    scan.add_argument("--include-files", action="store_true", help="Include per-file hashes in JSON output.")
    scan.set_defaults(func=cmd_scan)

    status = subcommands.add_parser("status", help="Summarize local skill roots.")
    add_common_scan_args(status)
    status.set_defaults(func=cmd_status)

    ops_status = subcommands.add_parser("ops-status", help="Summarize daemon, base record, remote snapshot, and optional OpenClaw reconcile state.")
    ops_status.add_argument("--local-root", default="~/.cc-switch/skills", help="Local installed skill root to scan.")
    ops_status.add_argument("--remote-snapshot", default="~/public-sync/skill-sync-sidecar-dev/current-mac", help="Local remote snapshot/cache directory with index.json.")
    ops_status.add_argument("--base-record", default="~/Library/Application Support/skill-sync-sidecar/base-record.json", help="Stable base record used by sync-daemon.")
    ops_status.add_argument("--state-file", default="~/Library/Application Support/skill-sync-sidecar/state.json", help="Daemon state file written by sync-daemon.")
    ops_status.add_argument("--blocked-report", help="Optional blocked-report.json to show the current approval queue.")
    ops_status.add_argument("--openclaw-reconcile-report", help="Existing reconcile-report.json to include; this command does not SSH.")
    ops_status.add_argument("--openclaw-reconcile-root", default="/private/tmp/openclaw-skill-sync-validate", help="Directory to search for the latest OpenClaw reconcile-report.json when no explicit report is provided.")
    ops_status.add_argument("--allow-new", action="store_true", help="Evaluate the sync plan with new skills allowed.")
    ops_status.add_argument("--allow-delete", action="store_true", help="Evaluate the sync plan with delete propagation allowed.")
    add_writer_policy_arg(ops_status)
    ops_status.add_argument("--fail-on-error", action="store_true", help="Exit non-zero when required local artifacts are unreadable.")
    ops_status.add_argument("--fail-on-blocked", action="store_true", help="Exit non-zero when the sync plan has blocked items.")
    ops_status.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    ops_status.set_defaults(func=cmd_ops_status)

    dashboard = subcommands.add_parser("dashboard", help="Serve a read-only local status dashboard.")
    dashboard.add_argument("--local-root", default="~/.cc-switch/skills", help="Local installed skill root to scan.")
    dashboard.add_argument("--remote-snapshot", default="~/public-sync/skill-sync-sidecar-dev/current-mac", help="Local remote snapshot/cache directory with index.json.")
    dashboard.add_argument("--base-record", default="~/Library/Application Support/skill-sync-sidecar/base-record.json", help="Stable base record used by sync-daemon.")
    dashboard.add_argument("--state-file", default="~/Library/Application Support/skill-sync-sidecar/state.json", help="Daemon state file written by sync-daemon.")
    dashboard.add_argument("--blocked-report", help="Optional blocked-report.json to show the current approval queue.")
    dashboard.add_argument("--openclaw-reconcile-report", help="Existing reconcile-report.json to include; this command does not SSH.")
    dashboard.add_argument("--openclaw-reconcile-root", default="/private/tmp/openclaw-skill-sync-validate", help="Directory to search for the latest OpenClaw reconcile-report.json when no explicit report is provided.")
    dashboard.add_argument("--allow-new", action="store_true", help="Evaluate the sync plan with new skills allowed.")
    dashboard.add_argument("--allow-delete", action="store_true", help="Evaluate the sync plan with delete propagation allowed.")
    add_writer_policy_arg(dashboard)
    dashboard.add_argument("--host", default="127.0.0.1", help="Dashboard listen host.")
    dashboard.add_argument("--port", type=int, default=8765, help="Dashboard listen port. Use 0 to allocate a free port.")
    dashboard.add_argument("--peer-status", action="append", default=[], help="Peer status JSON as id=/path/status.json. Repeat for multiple peers.")
    dashboard.set_defaults(func=cmd_dashboard)

    tool_projection = subcommands.add_parser("tool-projection", help="Preview canonical snapshot projection into local tool skill roots.")
    tool_projection.add_argument("--snapshot-dir", default="~/public-sync/skill-sync-sidecar-dev/current-mac", help="Canonical snapshot/cache directory with index.json.")
    tool_projection.add_argument("--tool", action="append", default=[], help="Tool root as id=/path or id=/path1,/path2. Repeat for multiple tools.")
    tool_projection.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    tool_projection.set_defaults(func=cmd_tool_projection)

    hub_import_diagnosis = subcommands.add_parser("hub-import-diagnosis", help="Explain why external skills can or cannot be imported into skillshub.")
    hub_import_diagnosis.add_argument("--hub-root", default="~/.skillshub", help="Skillshub Hub root.")
    hub_import_diagnosis.add_argument("--source-root", action="append", default=[], help="External skill root as id=/path. Repeat for multiple roots.")
    hub_import_diagnosis.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    hub_import_diagnosis.set_defaults(func=cmd_hub_import_diagnosis)

    hub_import_preview = subcommands.add_parser("hub-import-preview", help="Write a dry-run skillshub import preview package.")
    hub_import_preview.add_argument("--hub-root", default="~/.skillshub", help="Skillshub Hub root.")
    hub_import_preview.add_argument("--source-root", action="append", default=[], help="External skill root as id=/path. Repeat for multiple roots.")
    hub_import_preview.add_argument("--out", required=True, help="Output directory for preview.json and preview.md.")
    hub_import_preview.add_argument("--max-diff-lines", type=int, default=160, help="Maximum SKILL.md diff lines per update action.")
    hub_import_preview.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    hub_import_preview.set_defaults(func=cmd_hub_import_preview)

    openclaw_gate = subcommands.add_parser("openclaw-gate", help="Evaluate a read-only OpenClaw reconcile report as a sync safety gate.")
    openclaw_source = openclaw_gate.add_mutually_exclusive_group()
    openclaw_source.add_argument("--report", help="Explicit reconcile-report.json.")
    openclaw_source.add_argument("--report-root", default="/private/tmp/openclaw-skill-sync-validate", help="Directory to search for the latest reconcile-report.json.")
    openclaw_gate.add_argument("--require-complete", action="store_true", help="Block when remote_new remains; use before enabling automatic OpenClaw writes.")
    openclaw_gate.add_argument("--fail-on-blocked", action="store_true", help="Exit non-zero when the gate has blockers.")
    openclaw_gate.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    openclaw_gate.set_defaults(func=cmd_openclaw_gate)

    doctor = subcommands.add_parser("doctor", help="Validate local skills and report sync blockers.")
    add_common_scan_args(doctor)
    doctor.add_argument("--fail-on-warning", action="store_true", help="Exit non-zero when warnings are present.")
    doctor.set_defaults(func=cmd_doctor)

    snapshot = subcommands.add_parser("snapshot", help="Write a local WebDAV-ready snapshot directory.")
    add_common_scan_args(snapshot)
    snapshot.add_argument("--out", required=True, help="Output directory for index.json and skill archives.")
    snapshot.add_argument("--label", help="Optional snapshot id label.")
    snapshot.set_defaults(func=cmd_snapshot)

    remote_status = subcommands.add_parser("remote-status", help="Read remote snapshot metadata without applying it.")
    add_common_remote_args(remote_status)
    remote_status.set_defaults(func=cmd_remote_status)

    push = subcommands.add_parser("push", help="Upload a local snapshot directory to a remote. Dry-run unless --yes is set.")
    add_common_remote_args(push)
    push.add_argument("--snapshot-dir", required=True, help="Local snapshot directory produced by snapshot.")
    push.add_argument("--yes", action="store_true", help="Actually upload files. Without this flag, only prints a plan.")
    push.set_defaults(func=cmd_push)

    pull_cache = subcommands.add_parser("pull-cache", help="Download a remote snapshot into a local cache directory without applying it.")
    add_common_remote_args(pull_cache)
    pull_cache.add_argument("--out", required=True, help="Local cache directory.")
    pull_cache.set_defaults(func=cmd_pull_cache)

    diff = subcommands.add_parser("diff", help="Compare two local snapshot directories.")
    diff.add_argument("--left", required=True, help="Baseline snapshot directory.")
    diff.add_argument("--right", required=True, help="Candidate snapshot directory.")
    diff.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    diff.set_defaults(func=cmd_diff)

    stage = subcommands.add_parser("stage", help="Safely extract a snapshot cache into a staging directory.")
    stage.add_argument("--snapshot-dir", required=True, help="Local snapshot/cache directory with index.json and archives.")
    stage.add_argument("--out", required=True, help="Output staging directory.")
    stage.add_argument("--clean", action="store_true", help="Remove the snapshot staging subdirectory before extracting.")
    stage.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    stage.set_defaults(func=cmd_stage)

    apply = subcommands.add_parser("apply", help="Install from a staged snapshot with dry-run and explicit --yes modes.")
    apply.add_argument("--staged-dir", required=True, help="Staged snapshot directory containing .stage-index.json.")
    apply.add_argument("--target", required=True, choices=APPLY_TARGETS, help="Apply target adapter.")
    apply.add_argument("--target-root", help="Override target root for global tool targets or set the mixed-scope-root root.")
    apply.add_argument("--project-root", help="Project root for codex-project.")
    apply.add_argument("--skill-id", action="append", default=[], help="Only apply this skill id. Repeat for a small allowlist.")
    apply_mode = apply.add_mutually_exclusive_group(required=True)
    apply_mode.add_argument("--dry-run", action="store_true", help="Print an apply plan without writing files.")
    apply_mode.add_argument("--yes", action="store_true", help="Actually install allowed skills and write rollback metadata.")
    apply.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    apply.set_defaults(func=cmd_apply)

    rollback = subcommands.add_parser("rollback", help="Rollback a previous apply using its .apply-record.json.")
    rollback.add_argument("--record", required=True, help="Path to .apply-record.json.")
    rollback.add_argument("--yes", action="store_true", help="Actually rollback the recorded install.")
    rollback.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    rollback.set_defaults(func=cmd_rollback)

    sync_status = subcommands.add_parser("sync-status", help="Compare local, remote, and last-applied state for safe sync decisions.")
    sync_status.add_argument("--local-root", required=True, help="Local installed skill root to scan.")
    sync_status.add_argument("--remote-snapshot", required=True, help="Local remote snapshot/cache directory with index.json.")
    sync_status.add_argument("--last-applied-record", help="Optional .apply-record.json used as the base version.")
    sync_status.add_argument("--fail-on-conflict", action="store_true", help="Exit non-zero when conflicts are present.")
    sync_status.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    sync_status.set_defaults(func=cmd_sync_status)

    sync_plan = subcommands.add_parser("sync-plan", help="Build a safe dry-run sync plan from local, remote, and last-applied state.")
    sync_plan.add_argument("--local-root", required=True, help="Local installed skill root to scan.")
    sync_plan.add_argument("--remote-snapshot", required=True, help="Local remote snapshot/cache directory with index.json.")
    sync_plan.add_argument("--last-applied-record", help="Optional .apply-record.json used as the base version.")
    sync_plan.add_argument("--allow-new", action="store_true", help="Allow new local/remote skills to be planned for push/pull.")
    sync_plan.add_argument("--allow-delete", action="store_true", help="Allow one-sided deletes to be planned.")
    add_writer_policy_arg(sync_plan)
    sync_plan.add_argument("--fail-on-blocked", action="store_true", help="Exit non-zero when any item is blocked.")
    sync_plan.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    sync_plan.set_defaults(func=cmd_sync_plan)

    sync_apply = subcommands.add_parser("sync-apply", help="Execute safe pull/push sync actions from a sync plan.")
    sync_apply.add_argument("--local-root", help="Local installed skill root to scan and update.")
    sync_apply.add_argument("--target", default="cc-switch-global", choices=APPLY_TARGETS, help="Apply target adapter.")
    sync_apply.add_argument("--project-root", help="Project root for codex-project; defaults local root to <project-root>/skills.")
    sync_apply.add_argument("--remote-snapshot", required=True, help="Local remote snapshot/cache directory with index.json.")
    sync_apply.add_argument("--last-applied-record", help="Optional .apply-record.json used as the base version.")
    sync_apply.add_argument("--allow-new", action="store_true", help="Allow new local/remote skills to be pushed or pulled.")
    sync_apply.add_argument("--allow-delete", action="store_true", help="Plan deletions, but real deletion execution is not enabled yet.")
    add_writer_policy_arg(sync_apply)
    sync_apply_remote = sync_apply.add_mutually_exclusive_group()
    sync_apply_remote.add_argument("--remote", help="Remote URL for push actions. Supports file:// for tests and http(s) WebDAV.")
    sync_apply_remote.add_argument("--cc-switch-webdav", action="store_true", help="Use WebDAV settings from ~/.cc-switch/settings.json for push actions.")
    sync_apply.add_argument("--prefix", default="", help="Remote path prefix under the remote URL for push actions.")
    sync_apply.add_argument("--username-env", default="SKILL_SYNC_WEBDAV_USER", help="Environment variable containing WebDAV username.")
    sync_apply.add_argument("--password-env", default="SKILL_SYNC_WEBDAV_PASSWORD", help="Environment variable containing WebDAV password.")
    sync_apply_mode = sync_apply.add_mutually_exclusive_group(required=True)
    sync_apply_mode.add_argument("--dry-run", action="store_true", help="Print executable sync actions without writing files.")
    sync_apply_mode.add_argument("--yes", action="store_true", help="Actually apply supported pull actions and/or push local snapshot changes.")
    sync_apply.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    sync_apply.set_defaults(func=cmd_sync_apply)

    adopt_base = subcommands.add_parser("adopt-base", help="Write a stable base record when local and remote hashes already match.")
    adopt_base.add_argument("--local-root", required=True, help="Local installed skill root to scan.")
    adopt_base.add_argument("--remote-snapshot", required=True, help="Local remote snapshot/cache directory with index.json.")
    adopt_base.add_argument("--out", required=True, help="Stable base record path to write when --yes is set.")
    adopt_base.add_argument("--last-applied-record", help="Optional existing base/apply record.")
    adopt_base.add_argument("--prefix", default="", help="Remote path prefix recorded in the base record.")
    adopt_base_mode = adopt_base.add_mutually_exclusive_group(required=True)
    adopt_base_mode.add_argument("--dry-run", action="store_true", help="Validate adoption without writing a base record.")
    adopt_base_mode.add_argument("--yes", action="store_true", help="Write the base record when adoption is safe.")
    adopt_base.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    adopt_base.set_defaults(func=cmd_adopt_base)

    conflict_package = subcommands.add_parser("conflict-package", help="Materialize local/remote/base conflict packages for review.")
    conflict_package.add_argument("--local-root", required=True, help="Local installed skill root to scan.")
    conflict_package.add_argument("--remote-snapshot", required=True, help="Local remote snapshot/cache directory with index.json.")
    conflict_package.add_argument("--last-applied-record", help="Optional .apply-record.json or base record used as the common ancestor.")
    conflict_package.add_argument("--out", required=True, help="Output directory for conflict packages.")
    conflict_package.add_argument("--fail-on-empty", action="store_true", help="Exit non-zero when no conflicts are found.")
    conflict_package.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    conflict_package.set_defaults(func=cmd_conflict_package)

    tombstone = subcommands.add_parser("tombstone", help="Materialize non-destructive delete tombstones for one-sided deletes.")
    tombstone.add_argument("--local-root", required=True, help="Local installed skill root to scan.")
    tombstone.add_argument("--remote-snapshot", required=True, help="Local remote snapshot/cache directory with index.json.")
    tombstone.add_argument("--last-applied-record", required=True, help=".apply-record.json or base record used as the common ancestor.")
    tombstone.add_argument("--out", required=True, help="Output directory for tombstone records.")
    tombstone.add_argument("--fail-on-empty", action="store_true", help="Exit non-zero when no one-sided deletes are found.")
    tombstone.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    tombstone.set_defaults(func=cmd_tombstone)

    blocked_report = subcommands.add_parser("blocked-report", help="Write a JSON/Markdown review report for blocked sync-plan items.")
    blocked_report.add_argument("--local-root", required=True, help="Local installed skill root to scan.")
    blocked_report.add_argument("--remote-snapshot", required=True, help="Local remote snapshot/cache directory with index.json.")
    blocked_report.add_argument("--last-applied-record", help="Optional .apply-record.json or base record used as the common ancestor.")
    blocked_report.add_argument("--allow-new", action="store_true", help="Evaluate blocked items with new skills allowed.")
    blocked_report.add_argument("--allow-delete", action="store_true", help="Evaluate blocked items with delete propagation planned.")
    add_writer_policy_arg(blocked_report)
    blocked_report.add_argument("--out", required=True, help="Output directory for blocked-report.json and blocked-report.md.")
    blocked_report.add_argument("--fail-on-empty", action="store_true", help="Exit non-zero when no blocked items are found.")
    blocked_report.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    blocked_report.set_defaults(func=cmd_blocked_report)

    approved_push = subcommands.add_parser("approved-push", help="Publish explicitly approved blocked local push items to the remote.")
    approved_push.add_argument("--local-root", required=True, help="Local installed skill root to scan.")
    approved_push.add_argument("--remote-snapshot", required=True, help="Local remote snapshot/cache directory with index.json.")
    approved_push.add_argument("--last-applied-record", help="Optional .apply-record.json or base record used as the common ancestor.")
    approved_push.add_argument("--blocked-report", required=True, help="blocked-report.json generated by blocked-report, sync-cycle, or sync-daemon.")
    approved_push.add_argument("--skill-id", action="append", required=True, help="Blocked local skill id approved for this push. Repeat for multiple skills.")
    approved_push.add_argument("--allow-new", action="store_true", help="Allow approved local_new items to publish as push_new.")
    approved_push.add_argument("--base-record-out", help="Stable base record path to write after a successful approved push.")
    approved_push.add_argument("--out", required=True, help="Output directory for approved-push preview/record files.")
    add_common_remote_args(approved_push)
    approved_push_mode = approved_push.add_mutually_exclusive_group(required=True)
    approved_push_mode.add_argument("--dry-run", action="store_true", help="Validate approval and write a preview without uploading.")
    approved_push_mode.add_argument("--yes", action="store_true", help="Upload approved local changes and write the new base record.")
    approved_push.set_defaults(func=cmd_approved_push)

    reconcile_report = subcommands.add_parser("reconcile-report", help="Build a multi-writer adoption/reconcile report.")
    reconcile_source = reconcile_report.add_mutually_exclusive_group(required=True)
    reconcile_source.add_argument("--local-root", help="Local installed skill root to scan.")
    reconcile_source.add_argument("--local-inventory", help="Inventory JSON produced by scan --include-files or remote-inventory-py36.py.")
    reconcile_report.add_argument("--remote-snapshot", required=True, help="Local remote snapshot/cache directory with index.json and archives.")
    reconcile_report.add_argument("--previous-local-inventory", help="Previous inventory JSON for drift detection.")
    reconcile_report.add_argument("--label", help="Optional report label.")
    reconcile_report.add_argument("--out", help="Optional output directory for reconcile-report.json and reconcile-report.md.")
    reconcile_report.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    reconcile_report.set_defaults(func=cmd_reconcile_report)

    sync_cycle = subcommands.add_parser("sync-cycle", help="Run one safe pull-plan-apply cycle against a remote snapshot.")
    sync_cycle.add_argument("--local-root", help="Local installed skill root to scan and update.")
    sync_cycle.add_argument("--target", default="cc-switch-global", choices=APPLY_TARGETS, help="Apply target adapter.")
    sync_cycle.add_argument("--project-root", help="Project root for codex-project; defaults local root to <project-root>/skills.")
    sync_cycle_remote = sync_cycle.add_mutually_exclusive_group(required=True)
    sync_cycle_remote.add_argument("--remote", help="Remote URL. Supports file:// for tests and http(s) WebDAV.")
    sync_cycle_remote.add_argument("--cc-switch-webdav", action="store_true", help="Use WebDAV settings from ~/.cc-switch/settings.json.")
    sync_cycle.add_argument("--prefix", default="", help="Remote path prefix under the remote URL.")
    sync_cycle.add_argument("--username-env", default="SKILL_SYNC_WEBDAV_USER", help="Environment variable containing WebDAV username.")
    sync_cycle.add_argument("--password-env", default="SKILL_SYNC_WEBDAV_PASSWORD", help="Environment variable containing WebDAV password.")
    sync_cycle.add_argument("--cache-dir", required=True, help="Local cache directory for the downloaded remote snapshot.")
    sync_cycle.add_argument("--work-dir", required=True, help="Local work directory for conflict packages and tombstones.")
    sync_cycle.add_argument("--last-applied-record", help="Optional .apply-record.json used as the base version.")
    sync_cycle.add_argument("--allow-new", action="store_true", help="Allow new local/remote skills to be pushed or pulled.")
    sync_cycle.add_argument("--allow-delete", action="store_true", help="Plan deletions as tombstones; delete execution is not automatic.")
    add_writer_policy_arg(sync_cycle)
    sync_cycle_mode = sync_cycle.add_mutually_exclusive_group(required=True)
    sync_cycle_mode.add_argument("--dry-run", action="store_true", help="Download and plan without changing local or remote skill contents.")
    sync_cycle_mode.add_argument("--yes", action="store_true", help="Apply supported pull/push actions when the plan is safe.")
    sync_cycle.add_argument("--fail-on-blocked", action="store_true", help="Exit non-zero when the cycle is blocked.")
    sync_cycle.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    sync_cycle.set_defaults(func=cmd_sync_cycle)

    sync_daemon = subcommands.add_parser("sync-daemon", help="Run repeated safe sync-cycle passes against a remote snapshot.")
    sync_daemon.add_argument("--local-root", help="Local installed skill root to scan and update.")
    sync_daemon.add_argument("--target", default="cc-switch-global", choices=APPLY_TARGETS, help="Apply target adapter.")
    sync_daemon.add_argument("--project-root", help="Project root for codex-project; defaults local root to <project-root>/skills.")
    sync_daemon_remote = sync_daemon.add_mutually_exclusive_group(required=True)
    sync_daemon_remote.add_argument("--remote", help="Remote URL. Supports file:// for tests and http(s) WebDAV.")
    sync_daemon_remote.add_argument("--cc-switch-webdav", action="store_true", help="Use WebDAV settings from ~/.cc-switch/settings.json.")
    sync_daemon.add_argument("--prefix", default="", help="Remote path prefix under the remote URL.")
    sync_daemon.add_argument("--username-env", default="SKILL_SYNC_WEBDAV_USER", help="Environment variable containing WebDAV username.")
    sync_daemon.add_argument("--password-env", default="SKILL_SYNC_WEBDAV_PASSWORD", help="Environment variable containing WebDAV password.")
    sync_daemon.add_argument("--cache-dir", required=True, help="Local cache directory for the downloaded remote snapshot.")
    sync_daemon.add_argument("--work-dir", required=True, help="Local work directory for conflict packages and tombstones.")
    sync_daemon.add_argument("--last-applied-record", help="Optional .apply-record.json used as the base version.")
    sync_daemon.add_argument("--allow-new", action="store_true", help="Allow new local/remote skills to be pushed or pulled.")
    sync_daemon.add_argument("--allow-delete", action="store_true", help="Plan deletions as tombstones; delete execution is not automatic.")
    add_writer_policy_arg(sync_daemon)
    sync_daemon.add_argument("--interval-seconds", type=float, default=300.0, help="Seconds to sleep between cycles.")
    sync_daemon.add_argument("--max-cycles", type=int, help="Stop after this many cycles; omit for continuous daemon mode.")
    sync_daemon.add_argument("--continue-on-blocked", action="store_true", help="Keep polling when a cycle is blocked.")
    sync_daemon.add_argument("--state-file", help="Write latest daemon status JSON to this path after every cycle.")
    sync_daemon.add_argument("--base-record-file", help="Copy each successful apply/push base record to this stable path for future cycles.")
    sync_daemon_mode = sync_daemon.add_mutually_exclusive_group(required=True)
    sync_daemon_mode.add_argument("--dry-run", action="store_true", help="Poll and plan without changing local or remote skill contents.")
    sync_daemon_mode.add_argument("--yes", action="store_true", help="Apply supported pull/push actions when each cycle is safe.")
    sync_daemon.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    sync_daemon.set_defaults(func=cmd_sync_daemon)

    return parser


def add_common_scan_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root",
        action="append",
        help="Skill root to scan. Use source=/path to name a source. May be passed multiple times.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")


def add_common_remote_args(parser: argparse.ArgumentParser) -> None:
    remote_group = parser.add_mutually_exclusive_group(required=True)
    remote_group.add_argument("--remote", help="Remote URL. Supports file:// for tests and http(s) WebDAV.")
    remote_group.add_argument("--cc-switch-webdav", action="store_true", help="Use WebDAV settings from ~/.cc-switch/settings.json.")
    parser.add_argument("--prefix", default="", help="Remote path prefix under the remote URL.")
    parser.add_argument("--username-env", default="SKILL_SYNC_WEBDAV_USER", help="Environment variable containing WebDAV username.")
    parser.add_argument("--password-env", default="SKILL_SYNC_WEBDAV_PASSWORD", help="Environment variable containing WebDAV password.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")


def add_writer_policy_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--writer-policy",
        choices=WRITER_POLICIES,
        default="push-pull",
        help="Restrict automatic sync direction. Default preserves existing push/pull behavior.",
    )


def cmd_scan(args: argparse.Namespace) -> int:
    summary = scan_roots(args.root)
    if args.json:
        print(json.dumps(summary.to_dict(include_files=args.include_files), ensure_ascii=False, indent=2))
        return 0

    print(f"skills: {len(summary.skills)}")
    for skill in summary.skills:
        print(f"{skill.source}\t{skill.risk_level}\t{skill.skill_id}\t{skill.path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    summary = scan_roots(args.root)
    data = summary.to_dict()
    if args.json:
        print(json.dumps({key: data[key] for key in ("total", "by_source", "by_risk", "duplicates")}, ensure_ascii=False, indent=2))
        return 0

    print(f"total: {data['total']}")
    print("by_source:")
    for source, count in data["by_source"].items():
        print(f"  {source}: {count}")
    print("by_risk:")
    for risk, count in data["by_risk"].items():
        print(f"  {risk}: {count}")
    duplicates = data["duplicates"]
    print(f"duplicates: {len(duplicates)}")
    for skill_id, count in duplicates.items():
        print(f"  {skill_id}: {count}")
    return 0


def cmd_ops_status(args: argparse.Namespace) -> int:
    status = build_ops_status(
        Path(args.local_root),
        Path(args.remote_snapshot),
        base_record=Path(args.base_record) if args.base_record else None,
        state_file=Path(args.state_file) if args.state_file else None,
        blocked_report=Path(args.blocked_report) if args.blocked_report else None,
        openclaw_reconcile_report=Path(args.openclaw_reconcile_report) if args.openclaw_reconcile_report else None,
        openclaw_reconcile_root=Path(args.openclaw_reconcile_root) if args.openclaw_reconcile_root else None,
        allow_new=args.allow_new,
        allow_delete=args.allow_delete,
        writer_policy=args.writer_policy,
    )
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        print(render_ops_status_text(status))

    sync_plan = status.get("sync_plan")
    blocked = isinstance(sync_plan, dict) and sync_plan.get("blocked", 0)
    openclaw_gate = status.get("openclaw_gate")
    openclaw_blocked = isinstance(openclaw_gate, dict) and openclaw_gate.get("available") and not openclaw_gate.get("ok", True)
    if args.fail_on_error and status.get("error_count", 0):
        return 2
    if args.fail_on_blocked and (blocked or openclaw_blocked):
        return 3
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    try:
        peer_status_files = parse_peer_status_files(args.peer_status)
    except ValueError as exc:
        print(f"dashboard failed: {exc}", file=sys.stderr)
        return 2
    config = DashboardConfig(
        local_root=Path(args.local_root),
        remote_snapshot=Path(args.remote_snapshot),
        base_record=Path(args.base_record) if args.base_record else None,
        state_file=Path(args.state_file) if args.state_file else None,
        blocked_report=Path(args.blocked_report) if args.blocked_report else None,
        openclaw_reconcile_report=Path(args.openclaw_reconcile_report) if args.openclaw_reconcile_report else None,
        openclaw_reconcile_root=Path(args.openclaw_reconcile_root) if args.openclaw_reconcile_root else None,
        allow_new=args.allow_new,
        allow_delete=args.allow_delete,
        writer_policy=args.writer_policy,
        peer_status_files=peer_status_files,
    )
    serve_dashboard(args.host, args.port, config)
    return 0


def cmd_tool_projection(args: argparse.Namespace) -> int:
    try:
        adapters = [parse_tool_adapter_spec(value) for value in args.tool] if args.tool else None
        projection = build_tool_projection(Path(args.snapshot_dir), adapters=adapters)
    except (ProjectionError, ValueError) as exc:
        print(f"tool-projection failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(projection, ensure_ascii=False, indent=2))
    else:
        print(f"snapshot: {projection.get('snapshot_id')}")
        print(f"canonical_total: {projection.get('canonical_total')}")
        for tool in projection["tools"]:
            summary = tool["summary"]
            print(
                "{}: installed={} targeted={} missing={} drift={} not_targeted={} unsupported_scope={} blocked_error={} extra_local={}".format(
                    tool["name"],
                    tool["installed_total"],
                    tool["canonical_targeted"],
                    summary.get("missing", 0),
                    summary.get("drift", 0),
                    summary.get("not_targeted", 0),
                    summary.get("unsupported_scope", 0),
                    summary.get("blocked_error", 0),
                    len(tool["extra_local"]),
                )
            )
    return 0


def cmd_hub_import_diagnosis(args: argparse.Namespace) -> int:
    try:
        source_roots = [parse_hub_source_spec(value) for value in args.source_root] if args.source_root else None
        diagnosis = build_hub_import_diagnosis(Path(args.hub_root), source_roots=source_roots)
    except (HubImportDiagnosisError, ValueError) as exc:
        print(f"hub-import-diagnosis failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(diagnosis, ensure_ascii=False, indent=2))
    else:
        print(render_hub_import_diagnosis_text(diagnosis))
    return 0


def cmd_hub_import_preview(args: argparse.Namespace) -> int:
    try:
        source_roots = [parse_hub_source_spec(value) for value in args.source_root] if args.source_root else None
        package = build_hub_import_preview_package(
            Path(args.hub_root),
            source_roots=source_roots,
            out_dir=Path(args.out),
            max_diff_lines=max(1, args.max_diff_lines),
        )
    except (HubImportDiagnosisError, ValueError, OSError) as exc:
        print(f"hub-import-preview failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(package, ensure_ascii=False, indent=2))
    else:
        print(render_hub_import_preview_text(package))
    return 0


def parse_peer_status_files(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"--peer-status must be id=/path/status.json: {value}")
        peer_id, raw_path = value.split("=", 1)
        peer_id = peer_id.strip()
        raw_path = raw_path.strip()
        if not peer_id or not raw_path:
            raise ValueError(f"--peer-status must be id=/path/status.json: {value}")
        result[peer_id] = Path(raw_path).expanduser()
    return result


def cmd_openclaw_gate(args: argparse.Namespace) -> int:
    gate = build_openclaw_gate(
        report_path=Path(args.report) if args.report else None,
        report_root=Path(args.report_root) if args.report_root else None,
        require_complete=args.require_complete,
    )
    if args.json:
        print(json.dumps(gate, ensure_ascii=False, indent=2))
    else:
        print(render_openclaw_gate_text(gate))
    if args.fail_on_blocked and not gate.get("ok"):
        return 3
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    summary = scan_roots(args.root)
    data = summary.to_dict()
    warning_count = 0
    error_count = 0

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"checked: {data['total']} skills")
        for skill in summary.skills:
            if not skill.issues:
                continue
            print(f"\n{skill.source}/{skill.skill_id}")
            print(f"  path: {skill.path}")
            for issue in skill.issues:
                print(f"  [{issue.severity}] {issue.code}: {issue.message}")
                if issue.path:
                    print(f"    {issue.path}")

    for skill in summary.skills:
        for issue in skill.issues:
            if issue.severity == "error":
                error_count += 1
            elif issue.severity == "warning":
                warning_count += 1

    if error_count:
        return 2
    if warning_count and args.fail_on_warning:
        return 1
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    summary = scan_roots(args.root)
    index = write_snapshot(summary, Path(args.out), args.label)
    if args.json:
        print(json.dumps(index, ensure_ascii=False, indent=2))
    else:
        print(f"snapshot: {index['snapshot_id']}")
        print(f"skills: {index['total']}")
        print(f"out: {Path(args.out).resolve()}")
    return 0


def cmd_remote_status(args: argparse.Namespace) -> int:
    remote = open_remote_from_args(args)
    try:
        index_bytes = remote.get_bytes(_remote_index_path(args.prefix))
        index = json.loads(index_bytes.decode("utf-8"))
        payload = {
            "ok": True,
            "snapshot_id": index.get("snapshot_id"),
            "created_at": index.get("created_at"),
            "total": index.get("total"),
            "protocol_version": index.get("protocol_version"),
        }
    except RemoteError as exc:
        payload = {"ok": False, "error": str(exc)}

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif payload["ok"]:
        print(f"snapshot: {payload.get('snapshot_id')}")
        print(f"created_at: {payload.get('created_at')}")
        print(f"skills: {payload.get('total')}")
        print(f"protocol_version: {payload.get('protocol_version')}")
    else:
        print(f"remote unavailable: {payload['error']}", file=sys.stderr)
        return 2
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    snapshot_dir = Path(args.snapshot_dir)
    if not (snapshot_dir / "index.json").exists():
        print(f"snapshot directory has no index.json: {snapshot_dir}", file=sys.stderr)
        return 2

    plan = build_upload_plan(snapshot_dir)
    payload = {
        "dry_run": not args.yes,
        "files": len(plan.files),
        "bytes": plan.total_bytes,
        "remote": _remote_label(args),
        "prefix": args.prefix,
    }
    if not args.yes:
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("dry-run: no files uploaded")
            print(f"files: {payload['files']}")
            print(f"bytes: {payload['bytes']}")
            print(f"remote: {payload['remote']}")
            print("add --yes to upload")
        return 0

    guard_http_upload(args)
    remote = open_remote_from_args(args)
    try:
        upload_plan = upload_snapshot(snapshot_dir, remote, args.prefix)
    except RemoteError as exc:
        print(f"upload failed: {exc}", file=sys.stderr)
        return 2

    payload["dry_run"] = False
    payload["uploaded_files"] = len(upload_plan.files)
    payload["uploaded_bytes"] = upload_plan.total_bytes
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("upload complete")
        print(f"files: {payload['files']}")
        print(f"bytes: {payload['bytes']}")
        print(f"uploaded_files: {payload['uploaded_files']}")
        print(f"uploaded_bytes: {payload['uploaded_bytes']}")
        print(f"remote: {payload['remote']}")
    return 0


def cmd_pull_cache(args: argparse.Namespace) -> int:
    remote = open_remote_from_args(args)
    try:
        index = download_snapshot(remote, Path(args.out), args.prefix)
    except RemoteError as exc:
        print(f"pull-cache failed: {exc}", file=sys.stderr)
        return 2

    payload = {
        "snapshot_id": index.get("snapshot_id"),
        "created_at": index.get("created_at"),
        "total": index.get("total"),
        "out": str(Path(args.out).resolve()),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"snapshot: {payload['snapshot_id']}")
        print(f"skills: {payload['total']}")
        print(f"out: {payload['out']}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    diff = diff_snapshot_dirs(Path(args.left), Path(args.right))
    data = diff.to_dict()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    for key, value in data["summary"].items():
        print(f"{key}: {value}")
    for group in ("added", "removed", "changed", "risk_changed"):
        items = data[group]
        if items:
            print(f"{group}:")
            for item in items:
                print(f"  {item}")
    return 0


def cmd_stage(args: argparse.Namespace) -> int:
    try:
        stage_index = stage_snapshot(Path(args.snapshot_dir), Path(args.out), clean=args.clean)
    except StageError as exc:
        print(f"stage failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(stage_index, ensure_ascii=False, indent=2))
    else:
        print(f"snapshot: {stage_index['snapshot_id']}")
        print(f"skills: {stage_index['total']}")
        print(f"out: {Path(args.out).resolve() / str(stage_index['snapshot_id'])}")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    if args.yes and args.target in {"cc-switch-global", "mixed-scope-root", *TOOL_GLOBAL_TARGETS} and not args.target_root:
        print(f"real {args.target} apply requires explicit --target-root", file=sys.stderr)
        return 2
    if args.yes and args.target in TOOL_GLOBAL_TARGETS and not args.skill_id:
        print(f"real {args.target} apply requires at least one --skill-id allowlist entry", file=sys.stderr)
        return 2
    try:
        plan = build_apply_plan(
            Path(args.staged_dir),
            args.target,
            target_root=Path(args.target_root).expanduser() if args.target_root else None,
            project_root=Path(args.project_root).expanduser() if args.project_root else None,
            skill_ids=args.skill_id,
        )
    except ApplyPlanError as exc:
        print(f"apply plan failed: {exc}", file=sys.stderr)
        return 2

    if args.yes:
        try:
            result = execute_apply_plan(plan)
        except ApplyError as exc:
            print(f"apply failed: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"snapshot: {result['snapshot_id']}")
            print(f"target: {result['target']}")
            print(f"applied: {result['total_applied']}")
            print(f"skipped: {result['total_skipped']}")
            print(f"record: {result['record_path']}")
        return 0

    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        print(f"snapshot: {plan['snapshot_id']}")
        print(f"target: {plan['target']}")
        print(f"allowed: {plan['allowed']}")
        print(f"skipped: {plan['skipped']}")
        for item in plan["items"]:
            if item["allowed"]:
                print(f"install {item['key']} -> {item['target_path']}")
            else:
                print(f"skip {item['key']}: {item['reason']}")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    if not args.yes:
        print("rollback requires --yes", file=sys.stderr)
        return 2
    try:
        result = rollback_apply_record(Path(args.record).expanduser())
    except ApplyError as exc:
        print(f"rollback failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"rolled_back: {result['total']}")
        print(f"record: {result['rollback_record_path']}")
    return 0


def cmd_sync_status(args: argparse.Namespace) -> int:
    try:
        status = build_sync_status(
            Path(args.local_root).expanduser(),
            Path(args.remote_snapshot).expanduser(),
            Path(args.last_applied_record).expanduser() if args.last_applied_record else None,
        )
    except SyncStateError as exc:
        print(f"sync-status failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        print(f"total: {status['total']}")
        print(f"conflicts: {status['summary'].get('conflict', 0)}")
        print("summary:")
        for action, count in status["summary"].items():
            print(f"  {action}: {count}")
        for item in status["items"]:
            if item["action"] in {"conflict", "pull", "push", "local_new", "remote_new", "local_deleted", "remote_deleted"}:
                print(f"{item['action']} {item['skill_id']}: {item['reason']}")
    if args.fail_on_conflict and status["has_conflicts"]:
        return 3
    return 0


def cmd_sync_plan(args: argparse.Namespace) -> int:
    try:
        status = build_sync_status(
            Path(args.local_root).expanduser(),
            Path(args.remote_snapshot).expanduser(),
            Path(args.last_applied_record).expanduser() if args.last_applied_record else None,
        )
    except (SyncStateError, ValueError) as exc:
        print(f"sync-plan failed: {exc}", file=sys.stderr)
        return 2
    plan = build_sync_plan(status, allow_new=args.allow_new, allow_delete=args.allow_delete, writer_policy=args.writer_policy)
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        print("dry-run: no files changed")
        print(f"total: {plan['total']}")
        print(f"writer_policy: {plan['writer_policy']}")
        print(f"allowed: {plan['allowed']}")
        print(f"blocked: {plan['blocked']}")
        print("summary:")
        for action, count in plan["summary"].items():
            print(f"  {action}: {count}")
        for item in plan["items"]:
            if item["plan_action"] != "noop":
                print(f"{item['plan_action']} {item['skill_id']}: {item['reason']}")
    if args.fail_on_blocked and plan["blocked"]:
        return 3
    return 0


def cmd_sync_apply(args: argparse.Namespace) -> int:
    try:
        local_root, backup_root = sync_apply_roots_from_args(args)
    except ValueError as exc:
        print(f"sync-apply failed: {exc}", file=sys.stderr)
        return 2
    remote_snapshot = Path(args.remote_snapshot).expanduser()
    last_applied = Path(args.last_applied_record).expanduser() if args.last_applied_record else None

    if args.dry_run:
        try:
            preview = build_sync_apply_preview(
                local_root,
                remote_snapshot,
                last_applied_record=last_applied,
                allow_new=args.allow_new,
                allow_delete=args.allow_delete,
                writer_policy=args.writer_policy,
                target=args.target,
            )
        except (SyncStateError, ValueError) as exc:
            print(f"sync-apply failed: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(preview, ensure_ascii=False, indent=2))
        else:
            print("dry-run: no files changed")
            print(f"mode: {preview['mode']}")
            print(f"total: {preview['total']}")
            print(f"executable: {preview['executable']}")
            print(f"blocked: {preview['blocked']}")
            print(f"unsupported: {preview['unsupported']}")
            for item in preview["items"]:
                if item["sync_apply_action"] != "none" or not item["sync_apply_supported"]:
                    print(f"{item['plan_action']} {item['skill_id']}: {item['sync_apply_reason']}")
        return 0 if preview["supported_to_apply"] else 3

    remote = None
    if args.remote or args.cc_switch_webdav:
        try:
            guard_http_upload(args)
            remote = open_remote_from_args(args)
        except (ConfigError, RemoteError, SystemExit) as exc:
            print(f"sync-apply failed: {exc}", file=sys.stderr)
            return 2

    try:
        result = execute_sync_apply(
            local_root,
            remote_snapshot,
            last_applied_record=last_applied,
            allow_new=args.allow_new,
            allow_delete=args.allow_delete,
            writer_policy=args.writer_policy,
            remote=remote,
            remote_prefix=args.prefix,
            target=args.target,
            backup_root=backup_root,
        )
    except (SyncStateError, SyncApplyError, StageError, ApplyError, ValueError) as exc:
        print(f"sync-apply failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"status: {result['status']}")
        print(f"mode: {result['mode']}")
        print(f"applied: {result['applied']}")
        print(f"uploaded: {result['uploaded']}")
        apply_result = result.get("apply_result")
        if apply_result:
            print(f"record: {apply_result['record_path']}")
        if result.get("base_record_path"):
            print(f"base_record: {result['base_record_path']}")
    return 0


def cmd_adopt_base(args: argparse.Namespace) -> int:
    local_root = Path(args.local_root).expanduser()
    remote_snapshot = Path(args.remote_snapshot).expanduser()
    last_applied = Path(args.last_applied_record).expanduser() if args.last_applied_record else None
    out = Path(args.out).expanduser()

    try:
        if args.dry_run:
            result = build_base_adoption_preview(local_root, remote_snapshot, last_applied)
        else:
            result = execute_base_adoption(
                local_root,
                remote_snapshot,
                out,
                last_applied_record=last_applied,
                remote_prefix=args.prefix,
            )
    except (SyncStateError, BaseAdoptionError) as exc:
        print(f"adopt-base failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"mode: {result['mode']}")
        print(f"safe_to_adopt: {result['safe_to_adopt']}")
        print(f"total: {result['total']}")
        print(f"adoptable: {result['adoptable']}")
        print(f"blocked: {result['blocked']}")
        print("summary:")
        for action, count in result["summary"].items():
            print(f"  {action}: {count}")
        if result.get("record_path"):
            print(f"record: {result['record_path']}")
        for item in result.get("blocked_items", []):
            print(f"blocked {item['skill_id']}: {item['action']} {item['reason']}")

    return 0 if result["safe_to_adopt"] else 3


def sync_apply_roots_from_args(args: argparse.Namespace) -> Tuple[Path, Optional[Path]]:
    if args.target == "codex-project":
        if not args.project_root:
            raise ValueError("--project-root is required for --target codex-project")
        project_root = Path(args.project_root).expanduser()
        local_root = Path(args.local_root).expanduser() if args.local_root else project_root / "skills"
        return local_root, project_root / ".skill-sync-backups"
    if not args.local_root:
        raise ValueError(f"--local-root is required for --target {args.target}")
    return Path(args.local_root).expanduser(), None


def cmd_conflict_package(args: argparse.Namespace) -> int:
    try:
        result = build_conflict_packages(
            Path(args.local_root).expanduser(),
            Path(args.remote_snapshot).expanduser(),
            Path(args.out).expanduser(),
            Path(args.last_applied_record).expanduser() if args.last_applied_record else None,
        )
    except (SyncStateError, StageError, ConflictPackageError) as exc:
        print(f"conflict-package failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"conflicts: {result['total_conflicts']}")
        print(f"out: {result['out']}")
        for package in result["packages"]:
            print(f"{package['skill_id']}: {package['path']}")
    if args.fail_on_empty and result["total_conflicts"] == 0:
        return 3
    return 0


def cmd_tombstone(args: argparse.Namespace) -> int:
    try:
        result = build_tombstones(
            Path(args.local_root).expanduser(),
            Path(args.remote_snapshot).expanduser(),
            Path(args.out).expanduser(),
            Path(args.last_applied_record).expanduser(),
        )
    except (SyncStateError, StageError, TombstoneError) as exc:
        print(f"tombstone failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"tombstones: {result['total_tombstones']}")
        print(f"out: {result['out']}")
        for tombstone in result["tombstones"]:
            print(f"{tombstone['action']} {tombstone['skill_id']}: {tombstone['path']}")
    if args.fail_on_empty and result["total_tombstones"] == 0:
        return 3
    return 0


def cmd_blocked_report(args: argparse.Namespace) -> int:
    try:
        result = build_blocked_report(
            Path(args.local_root).expanduser(),
            Path(args.remote_snapshot).expanduser(),
            Path(args.out).expanduser(),
            Path(args.last_applied_record).expanduser() if args.last_applied_record else None,
            allow_new=args.allow_new,
            allow_delete=args.allow_delete,
            writer_policy=args.writer_policy,
        )
    except (SyncStateError, ValueError) as exc:
        print(f"blocked-report failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"blocked: {result['total']}")
        print(f"out: {result['out']}")
        print("summary:")
        for category, count in result["summary"].items():
            print(f"  {category}: {count}")
    if args.fail_on_empty and result["total"] == 0:
        return 3
    return 0


def cmd_approved_push(args: argparse.Namespace) -> int:
    try:
        if args.dry_run:
            preview = build_approved_push_preview(
                Path(args.local_root).expanduser(),
                Path(args.remote_snapshot).expanduser(),
                Path(args.blocked_report).expanduser(),
                args.skill_id,
                last_applied_record=Path(args.last_applied_record).expanduser() if args.last_applied_record else None,
                allow_new=args.allow_new or None,
            )
            result = write_approved_push_preview(preview, Path(args.out).expanduser())
        else:
            guard_http_upload(args)
            remote = open_remote_from_args(args)
            result = execute_approved_push(
                Path(args.local_root).expanduser(),
                Path(args.remote_snapshot).expanduser(),
                Path(args.blocked_report).expanduser(),
                args.skill_id,
                remote,
                remote_prefix=args.prefix,
                last_applied_record=Path(args.last_applied_record).expanduser() if args.last_applied_record else None,
                allow_new=args.allow_new or None,
                base_record_out=Path(args.base_record_out).expanduser() if args.base_record_out else None,
                out_dir=Path(args.out).expanduser(),
            )
    except (ApprovedPushError, SyncStateError, ConfigError, RemoteError) as exc:
        print(f"approved-push failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"dry_run: {result['dry_run']}")
        print(f"approved: {result['approved']}")
        print(f"skills: {', '.join(result['approved_skill_ids'])}")
        if result.get("uploaded_files") is not None:
            print(f"uploaded_files: {result['uploaded_files']}")
        if result.get("base_record_path"):
            print(f"base_record: {result['base_record_path']}")
        print(f"out: {result['out']}")
    return 0


def cmd_reconcile_report(args: argparse.Namespace) -> int:
    try:
        if args.local_inventory:
            local_inventory = load_inventory(Path(args.local_inventory).expanduser())
        else:
            local_inventory = scan_roots([f"local={Path(args.local_root).expanduser()}"]).to_dict(include_files=True)
        previous = load_inventory(Path(args.previous_local_inventory).expanduser()) if args.previous_local_inventory else None
        report = build_reconcile_report(
            local_inventory,
            Path(args.remote_snapshot).expanduser(),
            previous_local_inventory=previous,
            label=args.label,
        )
        outputs = write_reconcile_outputs(report, Path(args.out).expanduser()) if args.out else None
    except ReconcileError as exc:
        print(f"reconcile-report failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        payload = dict(report)
        if outputs:
            payload["outputs"] = outputs
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"local: {report['local_total']}")
        print(f"remote: {report['remote_total']}")
        print(f"safe_to_auto_apply: {report['safe_to_auto_apply']}")
        print("summary:")
        for status, count in report["summary"].items():
            print(f"  {status}: {count}")
        changed = report.get("changed_since_previous")
        if isinstance(changed, dict):
            print(f"changed_since_previous: {changed['changed_count']}")
        if outputs:
            print(f"json: {outputs['json']}")
            print(f"markdown: {outputs['markdown']}")
    return 0


def cmd_sync_cycle(args: argparse.Namespace) -> int:
    try:
        local_root, backup_root = sync_apply_roots_from_args(args)
        if args.yes:
            guard_http_upload(args)
        remote = open_remote_from_args(args)
        result = run_sync_cycle(
            local_root,
            remote,
            args.prefix,
            Path(args.cache_dir).expanduser(),
            Path(args.work_dir).expanduser(),
            last_applied_record=Path(args.last_applied_record).expanduser() if args.last_applied_record else None,
            allow_new=args.allow_new,
            allow_delete=args.allow_delete,
            writer_policy=args.writer_policy,
            dry_run=args.dry_run,
            target=args.target,
            backup_root=backup_root,
        )
    except (ValueError, ConfigError, RemoteError, SyncStateError, StageError, ApplyError, SyncApplyError, ConflictPackageError, TombstoneError) as exc:
        print(f"sync-cycle failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        plan = result["sync_plan"]
        print(f"status: {result['status']}")
        print(f"reason: {result['reason']}")
        print(f"snapshot: {result['snapshot_id']}")
        print(f"total: {plan['total']}")
        print(f"allowed: {plan['allowed']}")
        print(f"blocked: {plan['blocked']}")
        print("summary:")
        for action, count in plan["summary"].items():
            print(f"  {action}: {count}")
        conflicts = result.get("conflicts")
        tombstones = result.get("tombstones")
        if conflicts:
            print(f"conflicts: {conflicts['total_conflicts']} -> {conflicts['out']}")
        if tombstones:
            print(f"tombstones: {tombstones['total_tombstones']} -> {tombstones['out']}")
        apply_result = result.get("apply_result")
        if apply_result:
            print(f"applied: {apply_result['applied']}")
            print(f"uploaded: {apply_result['uploaded']}")

    if args.fail_on_blocked and (result["status"] == "blocked" or result["sync_plan"]["blocked"]):
        return 3
    return 0


def cmd_sync_daemon(args: argparse.Namespace) -> int:
    if args.max_cycles is not None and args.max_cycles < 1:
        print("sync-daemon failed: --max-cycles must be >= 1", file=sys.stderr)
        return 2
    if args.interval_seconds < 0:
        print("sync-daemon failed: --interval-seconds must be >= 0", file=sys.stderr)
        return 2
    try:
        local_root, backup_root = sync_apply_roots_from_args(args)
        if args.yes:
            guard_http_upload(args)
        remote = open_remote_from_args(args)
        result = run_sync_daemon(
            local_root,
            remote,
            args.prefix,
            Path(args.cache_dir).expanduser(),
            Path(args.work_dir).expanduser(),
            last_applied_record=Path(args.last_applied_record).expanduser() if args.last_applied_record else None,
            allow_new=args.allow_new,
            allow_delete=args.allow_delete,
            writer_policy=args.writer_policy,
            dry_run=args.dry_run,
            target=args.target,
            backup_root=backup_root,
            interval_seconds=args.interval_seconds,
            max_cycles=args.max_cycles,
            stop_on_blocked=not args.continue_on_blocked,
            state_file=Path(args.state_file).expanduser() if args.state_file else None,
            base_record_file=Path(args.base_record_file).expanduser() if args.base_record_file else None,
        )
    except (ValueError, ConfigError, RemoteError, SyncStateError, StageError, ApplyError, SyncApplyError, ConflictPackageError, TombstoneError) as exc:
        print(f"sync-daemon failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"status: {result['status']}")
        print(f"cycles_run: {result['cycles_run']}")
        for index, cycle in enumerate(result["cycles"], start=1):
            print(f"cycle {index}: {cycle['status']} {cycle['summary']} blocked={cycle['blocked']}")
    return 0


def _remote_index_path(prefix: str) -> str:
    clean = prefix.strip("/")
    return f"{clean}/index.json" if clean else "index.json"


def open_remote_from_args(args: argparse.Namespace):
    if getattr(args, "cc_switch_webdav", False):
        try:
            settings = load_cc_switch_webdav_settings()
        except ConfigError as exc:
            raise SystemExit(str(exc))
        return open_remote(
            settings.base_url,
            args.username_env,
            args.password_env,
            username=settings.username,
            password=settings.password,
        )
    return open_remote(args.remote, args.username_env, args.password_env)


def guard_http_upload(args: argparse.Namespace) -> None:
    remote_url = _remote_url_for_guard(args)
    scheme = urlparse(remote_url).scheme
    if scheme not in {"http", "https"}:
        return
    clean_prefix = args.prefix.strip("/")
    if not clean_prefix:
        raise SystemExit("refusing HTTP upload without --prefix; use a dedicated test prefix")
    if clean_prefix == "cc-switch-sync" or clean_prefix.startswith("cc-switch-sync/"):
        raise SystemExit("refusing to upload to official cc-switch-sync prefix")


def _remote_url_for_guard(args: argparse.Namespace) -> str:
    if getattr(args, "cc_switch_webdav", False):
        return load_cc_switch_webdav_settings().base_url
    return args.remote or ""


def _remote_label(args: argparse.Namespace) -> str:
    if getattr(args, "cc_switch_webdav", False):
        return "cc-switch-webdav"
    return _redact_remote(args.remote)


def _redact_remote(remote: str) -> str:
    if "@" not in remote:
        return remote
    scheme, rest = remote.split("://", 1) if "://" in remote else ("", remote)
    host = rest.split("@", 1)[1]
    return f"{scheme}://***@{host}" if scheme else f"***@{host}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BrokenPipeError:
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
