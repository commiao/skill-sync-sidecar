#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON:-python3}"
url="${SKILL_SYNC_MONITOR_URL:-http://100.123.208.32:8765/api/overview}"
timeout_seconds="${SKILL_SYNC_MONITOR_TIMEOUT_SECONDS:-60}"
stale_after_seconds="${SKILL_SYNC_MONITOR_STALE_AFTER_SECONDS:-1800}"

args=(
  -m skill_sync_sidecar
  monitor-summary
  --brief
  --url "$url"
  --timeout-seconds "$timeout_seconds"
  --stale-after-seconds "$stale_after_seconds"
)

if [ "${SKILL_SYNC_MONITOR_FAIL_ON_ALERT:-0}" = "1" ]; then
  args+=(--fail-on-alert)
fi

PYTHONPATH="$repo_root/src" "$python_bin" "${args[@]}"
