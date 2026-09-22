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

max_attempts="${SKILL_SYNC_PUBLISH_RETRY_ATTEMPTS:-3}"
retry_delays="${SKILL_SYNC_PUBLISH_RETRY_DELAYS:-15 45}"

# One blink of the network is not a job failure -- the next run is 300s away.
#
# T-0143 read the whole error log rather than guessing: 45 network failures over
# several months, 31 of which the in-run retry above already rescued. Of the 14
# that gave up, 6 were "nodename nor servname provided" / "Network is
# unreachable" -- the Mac simply had no network. No amount of extra retrying
# reaches a WebDAV server that has no route to it, and every one of those runs
# was followed 300s later by one that succeeded.
#
# So the threshold is not "retry harder", it is "how many runs in a row failed".
# That is the same shape monitor.py already uses for the dashboard poll
# (DEFAULT_FETCH_FAILURE_ALERT_THRESHOLD): transient below the threshold,
# sustained at or above it. Copying that shape rather than inventing a second
# one keeps both from drifting apart.
#
# Below the threshold the Python stderr is *withheld*, not just the exit code.
# This matters twice over: that traceback is what grew this job's error log to
# 223KB of failures nobody read, and it is what the new job-failure sweep in
# fleet-ops keys on. Printing it for a blink would light that sweep up on every
# blink -- a lamp that is always on is not a lamp.
failure_threshold="${SKILL_SYNC_PUBLISH_FAILURE_THRESHOLD:-3}"
failure_state="${SKILL_SYNC_PUBLISH_FAILURE_STATE:-$HOME/Library/Application Support/skill-sync-sidecar/peer-status-publish-failures}"

read_failures() {
  # An unreadable or garbage counter must not make the job fail; treat it as 0
  # and let the run itself decide. A broken counter should never be the reason
  # publishing stops.
  local raw
  raw="$(cat "$failure_state" 2>/dev/null || true)"
  case "$raw" in
    ''|*[!0-9]*) echo 0 ;;
    *) echo "$raw" ;;
  esac
}

write_failures() {
  mkdir -p "$(dirname "$failure_state")" 2>/dev/null || true
  printf '%s\n' "$1" >"$failure_state" 2>/dev/null || true
}

stderr_capture="$(mktemp -t publish-peer-status-stderr)"
trap 'rm -f "$stderr_capture"' EXIT

attempt=1
while true; do
  if PYTHONPATH="$repo_root/src" "$python_bin" "${args[@]}" 2>"$stderr_capture"; then
    cat "$stderr_capture" >&2
    write_failures 0
    exit 0
  else
    status=$?
  fi
  if [ "$attempt" -ge "$max_attempts" ]; then
    break
  fi
  delay="$(printf '%s\n' $retry_delays | sed -n "${attempt}p")"
  delay="${delay:-45}"
  # Only the retry notices go to stderr between attempts; the captured Python
  # stderr is held back until we know whether this is transient or sustained.
  echo "publish-peer-status attempt ${attempt}/${max_attempts} failed with exit=${status}; retrying in ${delay}s" >&2
  sleep "$delay"
  attempt=$((attempt + 1))
done

failures="$(read_failures)"
failures=$((failures + 1))
write_failures "$failures"

if [ "$failures" -lt "$failure_threshold" ]; then
  # Transient: say so in one line, keep the traceback out of the log, exit 0.
  # The next run is 300s away and has always been the thing that fixed this.
  reason="$(grep -o 'urlopen error [^>]*' "$stderr_capture" | tail -1 || true)"
  echo "publish-peer-status failed transiently (${failures}/${failure_threshold} runs in a row); next run in 300s${reason:+ -- ${reason}}" >&2
  exit 0
fi

# Sustained: this is no longer a blink. Let the captured stderr through so the
# log has something to diagnose with, and fail loudly.
cat "$stderr_capture" >&2
echo "publish-peer-status failed after ${attempt} attempt(s) in ${failures} runs in a row; last exit=${status}" >&2
exit "$status"
