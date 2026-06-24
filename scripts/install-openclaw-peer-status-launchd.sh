#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
interval_seconds="${OPENCLAW_PEER_REFRESH_INTERVAL_SECONDS:-300}"
label="${OPENCLAW_PEER_REFRESH_LABEL:-com.skill-sync-sidecar.openclaw-peer-status}"
launch_agents_dir="$HOME/Library/LaunchAgents"
logs_dir="$HOME/Library/Logs"
plist_path="$launch_agents_dir/${label}.plist"
out_file="${OPENCLAW_PEER_STATUS_OUT:-$HOME/Library/Application Support/skill-sync-sidecar/peers/openclaw-status.json}"

mkdir -p "$launch_agents_dir" "$logs_dir" "$(dirname "$out_file")"

PLIST_PATH="$plist_path" \
REPO_ROOT="$repo_root" \
LABEL="$label" \
INTERVAL_SECONDS="$interval_seconds" \
LOGS_DIR="$logs_dir" \
OUT_FILE="$out_file" \
python3 - <<'PY'
import os
from pathlib import Path
from plistlib import dump

repo_root = Path(os.environ["REPO_ROOT"])
label = os.environ["LABEL"]
logs_dir = Path(os.environ["LOGS_DIR"])
out_file = os.environ["OUT_FILE"]
plist = {
    "Label": label,
    "ProgramArguments": [
        str(repo_root / "scripts" / "refresh-openclaw-peer-status.sh"),
    ],
    "EnvironmentVariables": {
        "OPENCLAW_PEER_STATUS_OUT": out_file,
    },
    "RunAtLoad": True,
    "StartInterval": int(os.environ["INTERVAL_SECONDS"]),
    "StandardOutPath": str(logs_dir / "skill-sync-openclaw-peer-status.out.log"),
    "StandardErrorPath": str(logs_dir / "skill-sync-openclaw-peer-status.err.log"),
}

target = Path(os.environ["PLIST_PATH"])
tmp = target.with_name(f"{target.name}.tmp")
with tmp.open("wb") as fh:
    dump(plist, fh, sort_keys=False)
tmp.replace(target)
PY

chmod +x "$repo_root/scripts/refresh-openclaw-peer-status.sh"
plutil -lint "$plist_path"
launchctl bootout "gui/$(id -u)" "$plist_path" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$plist_path"
launchctl kickstart -k "gui/$(id -u)/$label"
sleep 3

echo "openclaw_peer_status_launchd_ok=true"
echo "label=$label"
echo "plist=$plist_path"
echo "out_file=$out_file"
launchctl print "gui/$(id -u)/$label" | sed -n '1,35p'
