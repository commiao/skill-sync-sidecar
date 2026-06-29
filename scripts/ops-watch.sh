#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON:-python3}"
url="${SKILL_SYNC_MONITOR_URL:-http://100.123.208.32:8765/api/summary}"
timeout_seconds="${SKILL_SYNC_MONITOR_TIMEOUT_SECONDS:-60}"
stale_after_seconds="${SKILL_SYNC_MONITOR_STALE_AFTER_SECONDS:-1800}"
mac_label="${SKILL_SYNC_MAC_PEER_STATUS_LABEL:-com.skill-sync-sidecar.mac-peer-status}"
openclaw_label="${OPENCLAW_PEER_REFRESH_LABEL:-com.skill-sync-sidecar.openclaw-peer-status}"
log_lines="${SKILL_SYNC_OPS_WATCH_LOG_LINES:-12}"

launchctl_status() {
  local label="$1"
  if launchctl print "gui/$(id -u)/${label}" >/tmp/skill-sync-launchctl.$$ 2>/dev/null; then
    python3 - "$label" /tmp/skill-sync-launchctl.$$ <<'PY'
import re
import sys
from pathlib import Path

label = sys.argv[1]
text = Path(sys.argv[2]).read_text(encoding="utf-8", errors="replace")

def find(pattern: str, default: str = "unknown") -> str:
    match = re.search(pattern, text)
    return match.group(1).strip() if match else default

state = find(r"state = ([^\n]+)")
runs = find(r"runs = ([^\n]+)")
last_exit = find(r"last exit code = ([^\n]+)")
interval = find(r"run interval = ([^\n]+)")
print(f"{label}: state={state} runs={runs} last_exit={last_exit} interval={interval}")
PY
  else
    echo "${label}: not_loaded"
  fi
  rm -f /tmp/skill-sync-launchctl.$$
}

show_log_tail() {
  local title="$1"
  local path="$2"
  echo
  echo "== ${title} =="
  if [ -f "$path" ]; then
    tail -n "$log_lines" "$path"
  else
    echo "missing: $path"
  fi
}

echo "== Skill Sync Monitor =="
PYTHONPATH="$repo_root/src" "$python_bin" -m skill_sync_sidecar monitor-summary \
  --url "$url" \
  --timeout-seconds "$timeout_seconds" \
  --stale-after-seconds "$stale_after_seconds"

echo
echo "== LaunchAgents =="
launchctl_status "$mac_label"
launchctl_status "$openclaw_label"

show_log_tail "Mac peer-status stdout" "$HOME/Library/Logs/skill-sync-mac-peer-status.out.log"
show_log_tail "Mac peer-status stderr" "$HOME/Library/Logs/skill-sync-mac-peer-status.err.log"
show_log_tail "OpenClaw peer-status stdout" "$HOME/Library/Logs/skill-sync-openclaw-peer-status.out.log"
show_log_tail "OpenClaw peer-status stderr" "$HOME/Library/Logs/skill-sync-openclaw-peer-status.err.log"
