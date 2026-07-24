#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MONITOR_URL="${SKILL_SYNC_MONITOR_URL:-http://100.123.208.32:8765/api/overview}"

exec "$SCRIPT_DIR/watch-sync-health.sh" \
  --max-iterations 1 \
  --monitor-url "$MONITOR_URL"

