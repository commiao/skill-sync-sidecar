#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON:-python3}"
label="${SKILL_SYNC_MAC_PEER_STATUS_LABEL:-com.skill-sync-sidecar.mac-peer-status}"
interval_seconds="${SKILL_SYNC_MAC_PEER_STATUS_INTERVAL_SECONDS:-300}"
launch_agents_dir="$HOME/Library/LaunchAgents"
logs_dir="$HOME/Library/Logs"
plist_path="$launch_agents_dir/${label}.plist"

case "$interval_seconds" in
  ''|*[!0-9]*)
    echo "SKILL_SYNC_MAC_PEER_STATUS_INTERVAL_SECONDS must be an integer" >&2
    exit 2
    ;;
esac

mkdir -p "$launch_agents_dir" "$logs_dir"
chmod +x "$repo_root/scripts/publish-mac-peer-status.sh"

PLIST_PATH="$plist_path" \
PYTHON_BIN="$python_bin" \
REPO_ROOT="$repo_root" \
LABEL="$label" \
INTERVAL_SECONDS="$interval_seconds" \
LOGS_DIR="$logs_dir" \
"$python_bin" - <<'PY'
import os
from pathlib import Path
from plistlib import dump

program_args = [
    str(Path(os.environ["REPO_ROOT"]) / "scripts" / "publish-mac-peer-status.sh"),
]

plist = {
    "Label": os.environ["LABEL"],
    "ProgramArguments": program_args,
    "EnvironmentVariables": {
        "PYTHON": os.environ["PYTHON_BIN"],
    },
    "RunAtLoad": True,
    "StartInterval": int(os.environ["INTERVAL_SECONDS"]),
    "StandardOutPath": str(Path(os.environ["LOGS_DIR"]) / "skill-sync-mac-peer-status.out.log"),
    "StandardErrorPath": str(Path(os.environ["LOGS_DIR"]) / "skill-sync-mac-peer-status.err.log"),
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

echo "mac_peer_status_launchd_ok=true"
echo "label=$label"
echo "plist=$plist_path"
echo "interval_seconds=$interval_seconds"
launchctl print "gui/$(id -u)/$label" | sed -n '1,35p'
