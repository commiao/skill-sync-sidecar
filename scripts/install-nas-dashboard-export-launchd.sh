#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
label="${SKILL_SYNC_NAS_DASHBOARD_EXPORT_LABEL:-com.skill-sync-sidecar.nas-dashboard-export}"
interval_seconds="${SKILL_SYNC_NAS_DASHBOARD_EXPORT_INTERVAL_SECONDS:-300}"
out_dir="${SKILL_SYNC_NAS_DASHBOARD_OUT:-$HOME/public-sync/skill-sync-sidecar-dashboard}"
launch_agents_dir="$HOME/Library/LaunchAgents"
logs_dir="$HOME/Library/Logs"
plist_path="$launch_agents_dir/${label}.plist"

case "$interval_seconds" in
  ''|*[!0-9]*)
    echo "SKILL_SYNC_NAS_DASHBOARD_EXPORT_INTERVAL_SECONDS must be an integer" >&2
    exit 2
    ;;
esac

mkdir -p "$launch_agents_dir" "$logs_dir" "$out_dir"
chmod +x "$repo_root/scripts/export-nas-dashboard.sh"

PLIST_PATH="$plist_path" \
REPO_ROOT="$repo_root" \
LABEL="$label" \
INTERVAL_SECONDS="$interval_seconds" \
LOGS_DIR="$logs_dir" \
OUT_DIR="$out_dir" \
python3 - <<'PY'
import os
from pathlib import Path
from plistlib import dump

repo_root = Path(os.environ["REPO_ROOT"])
logs_dir = Path(os.environ["LOGS_DIR"])
plist = {
    "Label": os.environ["LABEL"],
    "ProgramArguments": [
        str(repo_root / "scripts" / "export-nas-dashboard.sh"),
    ],
    "EnvironmentVariables": {
        "SKILL_SYNC_NAS_DASHBOARD_OUT": os.environ["OUT_DIR"],
    },
    "RunAtLoad": True,
    "StartInterval": int(os.environ["INTERVAL_SECONDS"]),
    "StandardOutPath": str(logs_dir / "skill-sync-nas-dashboard-export.out.log"),
    "StandardErrorPath": str(logs_dir / "skill-sync-nas-dashboard-export.err.log"),
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

echo "nas_dashboard_export_launchd_ok=true"
echo "label=$label"
echo "plist=$plist_path"
echo "out_dir=$out_dir"
echo "index=$out_dir/index.html"
launchctl print "gui/$(id -u)/$label" | sed -n '1,35p'
