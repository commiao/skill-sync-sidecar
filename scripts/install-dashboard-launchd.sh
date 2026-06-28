#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON:-python3}"
label="${SKILL_SYNC_DASHBOARD_LABEL:-com.skill-sync-sidecar.dashboard}"
host="${SKILL_SYNC_DASHBOARD_HOST:-127.0.0.1}"
port="${SKILL_SYNC_DASHBOARD_PORT:-8765}"
local_root="${SKILL_SYNC_LOCAL_ROOT:-$HOME/.cc-switch/skills}"
remote_snapshot="${SKILL_SYNC_REMOTE_SNAPSHOT:-$HOME/public-sync/skill-sync-sidecar-dev/current-mac}"
app_dir="$HOME/Library/Application Support/skill-sync-sidecar"
base_record="${SKILL_SYNC_BASE_RECORD:-$app_dir/base-record.json}"
state_file="${SKILL_SYNC_STATE_FILE:-$app_dir/state.json}"
peer_status="${SKILL_SYNC_OPENCLAW_PEER_STATUS:-$app_dir/peers/openclaw-status.json}"
writer_policy="${SKILL_SYNC_WRITER_POLICY:-push-pull}"
allow_new="${SKILL_SYNC_ALLOW_NEW:-1}"
launch_agents_dir="$HOME/Library/LaunchAgents"
logs_dir="$HOME/Library/Logs"
plist_path="$launch_agents_dir/${label}.plist"

case "$writer_policy" in
  push-pull|pull-only|push-only|no-writes) ;;
  *)
    echo "SKILL_SYNC_WRITER_POLICY must be push-pull, pull-only, push-only, or no-writes" >&2
    exit 2
    ;;
esac

case "$allow_new" in
  0|1) ;;
  *)
    echo "SKILL_SYNC_ALLOW_NEW must be 0 or 1" >&2
    exit 2
    ;;
esac

if [ ! -d "$local_root" ]; then
  echo "local skill root not found: $local_root" >&2
  exit 2
fi

if [ ! -f "$remote_snapshot/index.json" ]; then
  echo "remote snapshot index not found: $remote_snapshot/index.json" >&2
  exit 2
fi

if [ ! -f "$base_record" ]; then
  echo "base record not found: $base_record" >&2
  exit 2
fi

mkdir -p "$launch_agents_dir" "$logs_dir" "$app_dir/peers"

if [ ! -f "$peer_status" ]; then
  echo "peer status file not found; refreshing once: $peer_status" >&2
  OPENCLAW_PEER_STATUS_OUT="$peer_status" "$repo_root/scripts/refresh-openclaw-peer-status.sh"
fi

PLIST_PATH="$plist_path" \
PYTHON_BIN="$python_bin" \
REPO_ROOT="$repo_root" \
LABEL="$label" \
HOST="$host" \
PORT="$port" \
LOCAL_ROOT="$local_root" \
REMOTE_SNAPSHOT="$remote_snapshot" \
BASE_RECORD="$base_record" \
STATE_FILE="$state_file" \
PEER_STATUS="$peer_status" \
WRITER_POLICY="$writer_policy" \
ALLOW_NEW="$allow_new" \
LOGS_DIR="$logs_dir" \
"$python_bin" - <<'PY'
import os
from pathlib import Path
from plistlib import dump

program_args = [
    os.environ["PYTHON_BIN"],
    "-m",
    "skill_sync_sidecar",
    "dashboard",
    "--local-root",
    os.environ["LOCAL_ROOT"],
    "--remote-snapshot",
    os.environ["REMOTE_SNAPSHOT"],
    "--base-record",
    os.environ["BASE_RECORD"],
    "--state-file",
    os.environ["STATE_FILE"],
    "--writer-policy",
    os.environ["WRITER_POLICY"],
    "--peer-status",
    f"openclaw={os.environ['PEER_STATUS']}",
    "--host",
    os.environ["HOST"],
    "--port",
    os.environ["PORT"],
]
if os.environ["ALLOW_NEW"] == "1":
    program_args.append("--allow-new")

plist = {
    "Label": os.environ["LABEL"],
    "ProgramArguments": program_args,
    "EnvironmentVariables": {
        "PYTHONPATH": str(Path(os.environ["REPO_ROOT"]) / "src"),
    },
    "RunAtLoad": True,
    "KeepAlive": True,
    "StandardOutPath": str(Path(os.environ["LOGS_DIR"]) / "skill-sync-dashboard.out.log"),
    "StandardErrorPath": str(Path(os.environ["LOGS_DIR"]) / "skill-sync-dashboard.err.log"),
}

target = Path(os.environ["PLIST_PATH"])
tmp = target.with_name(f"{target.name}.tmp")
with tmp.open("wb") as fh:
    dump(plist, fh, sort_keys=False)
tmp.replace(target)
PY

plutil -lint "$plist_path"
launchctl bootout "gui/$(id -u)" "$plist_path" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$plist_path"
launchctl kickstart -k "gui/$(id -u)/$label"
sleep 3

echo "dashboard_launchd_ok=true"
echo "label=$label"
echo "plist=$plist_path"
echo "url=http://$host:$port"
echo "local_root=$local_root"
echo "remote_snapshot=$remote_snapshot"
echo "base_record=$base_record"
echo "state_file=$state_file"
echo "peer_status=$peer_status"
launchctl print "gui/$(id -u)/$label" | sed -n '1,35p'
