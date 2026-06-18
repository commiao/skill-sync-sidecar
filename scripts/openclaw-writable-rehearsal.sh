#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'EOF'
Usage: scripts/openclaw-writable-rehearsal.sh

Run a gated one-cycle writable sync-daemon rehearsal for OpenClaw.

The script never edits systemd units and never starts a long-running service.
It first requires a clean OpenClaw reconcile gate, then runs:

  sync-daemon --yes --max-cycles 1 --interval-seconds 0 --writer-policy pull-only

Environment overrides:
  PYTHON_BIN                         Python 3.9+ interpreter. Default: python3
  SKILL_SYNC_LOCAL_ROOT              Target skill root. Default: /home/admin/clawd/skills
  SKILL_SYNC_PREFIX                  WebDAV prefix. Default: skill-sync-sidecar-dev/current-mac
  SKILL_SYNC_REMOTE                  Optional remote URL. If unset, uses --cc-switch-webdav.
  SKILL_SYNC_USE_CC_SWITCH_WEBDAV    Set 0 to require SKILL_SYNC_REMOTE. Default: 1
  SKILL_SYNC_CACHE_DIR               Download cache. Default: /opt/skill-sync-sidecar/cache/openclaw-writable-rehearsal
  SKILL_SYNC_WORK_DIR                Work dir. Default: /opt/skill-sync-sidecar/work/openclaw-writable-rehearsal
  SKILL_SYNC_STATE_FILE              State JSON. Default: /opt/skill-sync-sidecar/state/openclaw-writable-rehearsal-state.json
  SKILL_SYNC_BASE_RECORD_FILE        Stable base record. Default: /opt/skill-sync-sidecar/state/openclaw-writable-rehearsal-base-record.json
  SKILL_SYNC_LAST_APPLIED_RECORD     Optional base/apply record. Defaults to SKILL_SYNC_BASE_RECORD_FILE when it exists.
  SKILL_SYNC_WRITER_POLICY           Sync direction policy. Default: pull-only
  OPENCLAW_RECONCILE_REPORT          Explicit reconcile-report.json for the strict gate.
  OPENCLAW_RECONCILE_ROOT            Report root when no explicit report is set.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
LOCAL_ROOT="${SKILL_SYNC_LOCAL_ROOT:-/home/admin/clawd/skills}"
PREFIX="${SKILL_SYNC_PREFIX:-skill-sync-sidecar-dev/current-mac}"
REMOTE_URL="${SKILL_SYNC_REMOTE:-}"
USE_CC_SWITCH_WEBDAV="${SKILL_SYNC_USE_CC_SWITCH_WEBDAV:-1}"
CACHE_DIR="${SKILL_SYNC_CACHE_DIR:-/opt/skill-sync-sidecar/cache/openclaw-writable-rehearsal}"
WORK_DIR="${SKILL_SYNC_WORK_DIR:-/opt/skill-sync-sidecar/work/openclaw-writable-rehearsal}"
STATE_FILE="${SKILL_SYNC_STATE_FILE:-/opt/skill-sync-sidecar/state/openclaw-writable-rehearsal-state.json}"
BASE_RECORD_FILE="${SKILL_SYNC_BASE_RECORD_FILE:-/opt/skill-sync-sidecar/state/openclaw-writable-rehearsal-base-record.json}"
LAST_APPLIED_RECORD="${SKILL_SYNC_LAST_APPLIED_RECORD:-}"
WRITER_POLICY="${SKILL_SYNC_WRITER_POLICY:-pull-only}"
OPENCLAW_RECONCILE_REPORT="${OPENCLAW_RECONCILE_REPORT:-}"
OPENCLAW_RECONCILE_ROOT="${OPENCLAW_RECONCILE_ROOT:-/private/tmp/openclaw-skill-sync-validate}"

if [[ -z "$LAST_APPLIED_RECORD" && -f "$BASE_RECORD_FILE" ]]; then
  LAST_APPLIED_RECORD="$BASE_RECORD_FILE"
fi
if [[ -n "$LAST_APPLIED_RECORD" && ! -f "$LAST_APPLIED_RECORD" ]]; then
  printf 'last applied record not found: %s\n' "$LAST_APPLIED_RECORD" >&2
  exit 2
fi

mkdir -p "$CACHE_DIR" "$WORK_DIR" "$(dirname "$STATE_FILE")" "$(dirname "$BASE_RECORD_FILE")"

gate_args=(
  -m skill_sync_sidecar openclaw-gate
  --require-complete
  --fail-on-blocked
)
if [[ -n "$OPENCLAW_RECONCILE_REPORT" ]]; then
  gate_args+=(--report "$OPENCLAW_RECONCILE_REPORT")
else
  gate_args+=(--report-root "$OPENCLAW_RECONCILE_ROOT")
fi

printf '[openclaw-writable-rehearsal] strict gate\n'
PYTHONPATH="$ROOT_DIR/src" "$PYTHON_BIN" "${gate_args[@]}"

daemon_args=(
  -m skill_sync_sidecar sync-daemon
  --local-root "$LOCAL_ROOT"
  --target cc-switch-global
  --prefix "$PREFIX"
  --cache-dir "$CACHE_DIR"
  --work-dir "$WORK_DIR"
  --state-file "$STATE_FILE"
  --base-record-file "$BASE_RECORD_FILE"
  --writer-policy "$WRITER_POLICY"
  --yes
  --max-cycles 1
  --interval-seconds 0
  --json
)

if [[ -n "$LAST_APPLIED_RECORD" ]]; then
  daemon_args+=(--last-applied-record "$LAST_APPLIED_RECORD")
fi
if [[ -n "$REMOTE_URL" ]]; then
  daemon_args+=(--remote "$REMOTE_URL")
elif [[ "$USE_CC_SWITCH_WEBDAV" == "1" ]]; then
  daemon_args+=(--cc-switch-webdav)
else
  printf 'SKILL_SYNC_REMOTE is required when SKILL_SYNC_USE_CC_SWITCH_WEBDAV=0\n' >&2
  exit 2
fi

printf '[openclaw-writable-rehearsal] one-cycle writable daemon\n'
PYTHONPATH="$ROOT_DIR/src" "$PYTHON_BIN" "${daemon_args[@]}"

printf '[openclaw-writable-rehearsal] state=%s\n' "$STATE_FILE"
