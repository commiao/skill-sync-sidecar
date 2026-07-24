#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: watch-sync-health.sh [--interval-seconds N] [--max-iterations N] [--monitor-url URL]

Periodic read-only health watcher for Skill Sync.
- monitor-url: sidecar overview endpoint (default  http://100.123.208.32:8765/api/overview)
- monitor-summary-file: optional local JSON file path (default SKILL_SYNC_MONITOR_SUMMARY_FILE or none)
- interval-seconds: sleep between checks (default 1800)
- max-iterations: stop after N rounds (0 = run forever)
USAGE
}

interval_seconds="${WATCH_SYNC_HEALTH_INTERVAL_SECONDS:-1800}"
max_iterations="${WATCH_SYNC_HEALTH_MAX_ITERATIONS:-0}"
monitor_url="${SKILL_SYNC_MONITOR_URL:-http://100.123.208.32:8765/api/overview}"
monitor_summary_file="${SKILL_SYNC_MONITOR_SUMMARY_FILE:-}"

while [[ ${#} -gt 0 ]]; do
  case "${1:-}" in
    -h|--help)
      usage
      exit 0
      ;;
    --interval-seconds)
      shift
      interval_seconds="${1:?--interval-seconds requires a value}"
      ;;
    --max-iterations)
      shift
      max_iterations="${1:?--max-iterations requires a value}"
      ;;
    --monitor-url)
      shift
      monitor_url="${1:?--monitor-url requires a value}"
      ;;
    --monitor-summary-file)
      shift
      monitor_summary_file="${1:?--monitor-summary-file requires a value}"
      ;;
    *)
      echo "error: unknown argument: ${1}" >&2
      usage
      exit 1
      ;;
  esac
  shift

done

if ! [[ "$interval_seconds" =~ ^[0-9]+$ ]]; then
  echo "error: --interval-seconds must be an integer" >&2
  exit 1
fi

if ! [[ "$max_iterations" =~ ^[0-9]+$ ]]; then
  echo "error: --max-iterations must be an integer" >&2
  exit 1
fi

if [ -n "$monitor_summary_file" ] && [ ! -r "$monitor_summary_file" ]; then
  echo "error: --monitor-summary-file / SKILL_SYNC_MONITOR_SUMMARY_FILE must be readable: $monitor_summary_file" >&2
  exit 1
fi

prev_health=""
iter=0
tmp_dir="${TMPDIR:-/tmp}/skill-sync-watch"
mkdir -p "$tmp_dir"

while true; do
  iter=$((iter+1))
  ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  state="healthy"

  check_blocked_file="${tmp_dir}/blocked-${$}.txt"
  check_monitor_file="${tmp_dir}/monitor-${$}.json"

  blocked_queue_cmd="$(cd "$(dirname "$0")" && pwd)/blocked-queue.sh"

  if [ -n "$monitor_summary_file" ]; then
    SKILL_SYNC_BLOCKED_QUEUE_SUMMARY_FILE="$monitor_summary_file" \
      bash "$blocked_queue_cmd" >"$check_blocked_file" 2>&1
  else
    bash "$blocked_queue_cmd" >"$check_blocked_file" 2>&1
  fi

  blocked_queue_status=$?

  if [ "$blocked_queue_status" -eq 0 ]; then
    blocked_status="$(sed -n '1,6p' "$check_blocked_file" | tr '\n' '; ')"
    blocked_count="$(awk '/blocked:/ {print $2}' "$check_blocked_file" | tail -n1)"
  else
    if [ -n "$monitor_summary_file" ] && [ -r "$monitor_summary_file" ]; then
      if ! blocked_count="$(python3 - "$monitor_summary_file" <<'PY'
import json
import sys

path = sys.argv[1]
summary = json.loads(open(path, encoding="utf-8").read())
items = summary.get("dashboard", {}).get("blocked_items") if isinstance(summary.get("dashboard", {}), dict) else []
print(len(items) if isinstance(items, list) else 0)
PY
)"; then
        blocked_count="unknown"
        blocked_status="blocked-queue.sh failed; summary fallback failed"
        state="degraded"
      else
        blocked_count="${blocked_count:-0}"
        blocked_status="blocked-queue.sh failed; fallback to monitor summary (count=${blocked_count})"
      fi
    else
      blocked_count="unknown"
      blocked_status="blocked-queue.sh failed"
      state="degraded"
    fi
  fi

  if MONITOR_URL="$monitor_url" MONITOR_SUMMARY_FILE="$monitor_summary_file" python3 - <<'PY' >"$check_monitor_file" 2>/dev/null; then
import json, os, urllib.request
summary_file = os.environ.get("MONITOR_SUMMARY_FILE", "").strip()
if summary_file:
    with open(summary_file, encoding="utf-8") as f:
        data = json.load(f)
else:
    with urllib.request.urlopen(os.environ["MONITOR_URL"], timeout=30) as r:
        data = json.load(r)
d = data.get('dashboard', data)
print(f"health={d.get('health')}")
print(f"blocked={d.get('blocked', 'unknown')}")
print(f"alerts={d.get('alerts', 0)}")
print(f"warnings={d.get('warnings', 0)}")
snapshot = d.get('snapshot', None)
if not snapshot:
    snapshot = data.get('snapshot')
if not snapshot and 'remote_snapshot' in data:
    rs = data.get('remote_snapshot') or {}
    snapshot = rs.get('snapshot_id') if isinstance(rs, dict) else None
if not snapshot and 'remote_snapshot' in d:
    rs = d.get('remote_snapshot') or {}
    snapshot = rs.get('snapshot_id') if isinstance(rs, dict) else None
print(f"snapshot={snapshot or ''}")
PY
    summary_health="$(awk -F'=' '/^health=/{print $2}' "$check_monitor_file")"
    summary_blocked="$(awk -F'=' '/^blocked=/{print $2}' "$check_monitor_file")"
    alerts="$(awk -F'=' '/^alerts=/{print $2}' "$check_monitor_file")"
    warnings="$(awk -F'=' '/^warnings=/{print $2}' "$check_monitor_file")"
    snapshot="$(awk -F'=' '/^snapshot=/{print $2}' "$check_monitor_file")"
    if [ "$summary_health" != "green" ]; then
      state="degraded"
    fi
    monitor_status="health=$summary_health blocked=$summary_blocked alerts=$alerts warnings=$warnings snapshot=$snapshot"
  else
    summary_health="unknown"
    summary_blocked="unknown"
    alerts="unknown"
    warnings="unknown"
    snapshot=""
    monitor_status="monitor-summary failed"
    state="degraded"
  fi

  if [ "$prev_health" != "$summary_health:$summary_blocked:$alerts:$warnings" ]; then
    changed="*"
    prev_health="$summary_health:$summary_blocked:$alerts:$warnings"
  else
    changed=" "
  fi

  echo "[${ts}] ${changed} state=${state} ${monitor_status}"
  echo "[${ts}] blocked_queue=${blocked_count}; ${blocked_status}"

  if [ "$state" = "degraded" ]; then
    echo "--- blocked queue ---"
    cat "$check_blocked_file"
    echo "--- api snapshot ---"
    [ -f "$check_monitor_file" ] && cat "$check_monitor_file"
  fi

  if [ "$max_iterations" != "0" ] && [ "$iter" -ge "$max_iterations" ]; then
    rm -f "$check_blocked_file"
    echo "[${ts}] reached max-iterations=${max_iterations}, stop."
    exit 0
  fi

  rm -f "$check_blocked_file" "$check_monitor_file"
  sleep "$interval_seconds"
done
