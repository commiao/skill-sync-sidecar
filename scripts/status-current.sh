#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
# 不设 SKILL_SYNC_LOCAL_ROOT 时用配置里的本机正本（未迁移的机器仍是 ~/.cc-switch/skills）
LOCAL_ROOT="${SKILL_SYNC_LOCAL_ROOT:-$(PYTHONPATH="$ROOT_DIR/src" "$PYTHON_BIN" -c 'from skill_sync_sidecar.config import configured_local_root; print(configured_local_root())')}"
REMOTE_SNAPSHOT="${SKILL_SYNC_REMOTE_SNAPSHOT:-$HOME/public-sync/skill-sync-sidecar-dev/current-mac}"
BASE_RECORD="${SKILL_SYNC_BASE_RECORD:-$HOME/Library/Application Support/skill-sync-sidecar/base-record.json}"
STATE_FILE="${SKILL_SYNC_STATE_FILE:-$HOME/Library/Application Support/skill-sync-sidecar/state.json}"
OPENCLAW_RECONCILE_ROOT="${OPENCLAW_RECONCILE_ROOT:-/private/tmp/openclaw-skill-sync-validate}"

cd "$ROOT_DIR"

PYTHONPATH="$ROOT_DIR/src" "$PYTHON_BIN" -m skill_sync_sidecar ops-status \
  --local-root "$LOCAL_ROOT" \
  --remote-snapshot "$REMOTE_SNAPSHOT" \
  --base-record "$BASE_RECORD" \
  --state-file "$STATE_FILE" \
  --openclaw-reconcile-root "$OPENCLAW_RECONCILE_ROOT" \
  --allow-new \
  --fail-on-blocked \
  --fail-on-error
