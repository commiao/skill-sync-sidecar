#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON:-python3}"

device_id="${SKILL_SYNC_DEVICE_ID:-mac}"
device_name="${SKILL_SYNC_DEVICE_NAME:-Mac 本机}"
peer_id="${SKILL_SYNC_PEER_ID:-$device_id}"
status_path="${SKILL_SYNC_PEER_STATUS_PATH:-skill-sync-sidecar-peer-status/mac.json}"
local_root="${SKILL_SYNC_LOCAL_ROOT:-$HOME/.cc-switch/skills}"
remote_snapshot="${SKILL_SYNC_REMOTE_SNAPSHOT:-$HOME/public-sync/skill-sync-sidecar-dev/current-mac}"
base_record="${SKILL_SYNC_BASE_RECORD:-$HOME/Library/Application Support/skill-sync-sidecar/base-record.json}"
state_file="${SKILL_SYNC_STATE_FILE:-$HOME/Library/Application Support/skill-sync-sidecar/state.json}"
openclaw_reconcile_root="${SKILL_SYNC_OPENCLAW_RECONCILE_ROOT:-/private/tmp/openclaw-skill-sync-validate}"
writer_policy="${SKILL_SYNC_WRITER_POLICY:-push-pull}"

args=(
  -m skill_sync_sidecar
  publish-peer-status
  --cc-switch-webdav
  --peer-id "$peer_id"
  --peer-name "$device_name"
  --status-path "$status_path"
  --local-root "$local_root"
  --remote-snapshot "$remote_snapshot"
  --base-record "$base_record"
  --state-file "$state_file"
  --openclaw-reconcile-root "$openclaw_reconcile_root"
  --writer-policy "$writer_policy"
  --allow-new
)

if [ -n "${SKILL_SYNC_BLOCKED_REPORT:-}" ]; then
  args+=(--blocked-report "$SKILL_SYNC_BLOCKED_REPORT")
fi
if [ -n "${SKILL_SYNC_OPENCLAW_RECONCILE_REPORT:-}" ]; then
  args+=(--openclaw-reconcile-report "$SKILL_SYNC_OPENCLAW_RECONCILE_REPORT")
fi
if [ "${SKILL_SYNC_ALLOW_DELETE:-0}" = "1" ]; then
  args+=(--allow-delete)
fi

PYTHONPATH="$repo_root/src" "$python_bin" "${args[@]}"
