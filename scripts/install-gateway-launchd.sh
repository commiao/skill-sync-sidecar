#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON:-python3}"
label="${SKILL_SYNC_GATEWAY_LABEL:-com.skill-sync-sidecar.gateway}"
host="${SKILL_SYNC_GATEWAY_HOST:-127.0.0.1}"
port="${SKILL_SYNC_GATEWAY_PORT:-8877}"
prefix="${SKILL_SYNC_GATEWAY_PREFIX:-skill-sync-sidecar-dev/current-mac}"
cache_dir="${SKILL_SYNC_GATEWAY_CACHE_DIR:-$HOME/Library/Caches/skill-sync-sidecar/gateway/current}"
refresh_interval_seconds="${SKILL_SYNC_GATEWAY_REFRESH_INTERVAL_SECONDS:-60}"
app_dir="$HOME/Library/Application Support/skill-sync-sidecar"
peer_status="${SKILL_SYNC_OPENCLAW_PEER_STATUS:-$app_dir/peers/openclaw-status.json}"
launch_agents_dir="$HOME/Library/LaunchAgents"
logs_dir="$HOME/Library/Logs"
plist_path="$launch_agents_dir/${label}.plist"

case "$refresh_interval_seconds" in
  ''|*[!0-9.]*)
    echo "SKILL_SYNC_GATEWAY_REFRESH_INTERVAL_SECONDS must be numeric" >&2
    exit 2
    ;;
esac

mkdir -p "$launch_agents_dir" "$logs_dir" "$cache_dir" "$app_dir/peers"

peer_args=()
if [ -f "$peer_status" ]; then
  peer_args=(--peer-status "oc-vps=$peer_status")
fi

PLIST_PATH="$plist_path" \
PYTHON_BIN="$python_bin" \
REPO_ROOT="$repo_root" \
LABEL="$label" \
HOST="$host" \
PORT="$port" \
PREFIX="$prefix" \
CACHE_DIR="$cache_dir" \
REFRESH_INTERVAL_SECONDS="$refresh_interval_seconds" \
PEER_STATUS="$peer_status" \
HAS_PEER_STATUS="$([ -f "$peer_status" ] && echo 1 || echo 0)" \
LOGS_DIR="$logs_dir" \
"$python_bin" - <<'PY'
import os
from pathlib import Path
from plistlib import dump

program_args = [
    os.environ["PYTHON_BIN"],
    "-m",
    "skill_sync_sidecar",
    "gateway",
    "--cc-switch-webdav",
    "--prefix",
    os.environ["PREFIX"],
    "--cache-dir",
    os.environ["CACHE_DIR"],
    "--refresh-interval-seconds",
    os.environ["REFRESH_INTERVAL_SECONDS"],
    "--host",
    os.environ["HOST"],
    "--port",
    os.environ["PORT"],
]
if os.environ["HAS_PEER_STATUS"] == "1":
    program_args.extend(["--peer-status", f"oc-vps={os.environ['PEER_STATUS']}"])

plist = {
    "Label": os.environ["LABEL"],
    "ProgramArguments": program_args,
    "EnvironmentVariables": {
        "PYTHONPATH": str(Path(os.environ["REPO_ROOT"]) / "src"),
    },
    "RunAtLoad": True,
    "KeepAlive": True,
    "StandardOutPath": str(Path(os.environ["LOGS_DIR"]) / "skill-sync-gateway.out.log"),
    "StandardErrorPath": str(Path(os.environ["LOGS_DIR"]) / "skill-sync-gateway.err.log"),
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

echo "gateway_launchd_ok=true"
echo "label=$label"
echo "plist=$plist_path"
echo "url=http://$host:$port"
echo "prefix=$prefix"
echo "cache_dir=$cache_dir"
echo "peer_status=$([ -f "$peer_status" ] && echo "$peer_status" || echo "not_configured")"
launchctl print "gui/$(id -u)/$label" | sed -n '1,35p'
