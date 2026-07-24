#!/usr/bin/env bash
set -euo pipefail

label="${SKILL_SYNC_EXECUTOR_LABEL:-com.skill-sync-sidecar.operator-executor}"
repo_root="${SKILL_SYNC_EXECUTOR_REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
host="${SKILL_SYNC_EXECUTOR_HOST:-127.0.0.1}"
port="${SKILL_SYNC_EXECUTOR_PORT:-18765}"
allow_local_writes="${SKILL_SYNC_EXECUTOR_ALLOW_LOCAL_WRITES:-1}"
allow_publish="${SKILL_SYNC_EXECUTOR_ALLOW_PUBLISH:-0}"
device_id="${SKILL_SYNC_DEVICE_ID:-mac}"
device_name="${SKILL_SYNC_DEVICE_NAME:-Mac 本机}"
logs_dir="${SKILL_SYNC_EXECUTOR_LOGS_DIR:-$HOME/Library/Logs}"
plist="$HOME/Library/LaunchAgents/${label}.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$logs_dir"

python3 - "$plist" "$label" "$repo_root" "$host" "$port" "$allow_local_writes" "$allow_publish" "$device_id" "$device_name" "$logs_dir" <<'PY'
import plistlib
import sys
from pathlib import Path

plist = Path(sys.argv[1])
label = sys.argv[2]
repo_root = Path(sys.argv[3]).resolve()
host = sys.argv[4]
port = sys.argv[5]
allow_local_writes = sys.argv[6] == "1"
allow_publish = sys.argv[7] == "1"
device_id = sys.argv[8]
device_name = sys.argv[9]
logs_dir = Path(sys.argv[10]).expanduser()
program_arguments = [
    "/usr/bin/python3",
    "-c",
    "from skill_sync_sidecar.cli import main; raise SystemExit(main())",
    "operator-executor",
    "--repo-root",
    str(repo_root),
    "--host",
    host,
    "--port",
    port,
]
if allow_local_writes:
    program_arguments.append("--allow-local-writes")
if allow_publish:
    program_arguments.append("--allow-publish")
payload = {
    "Label": label,
    "ProgramArguments": program_arguments,
    "EnvironmentVariables": {
        "PYTHONPATH": str(repo_root / "src"),
        "SKILL_SYNC_DEVICE_ID": device_id,
        "SKILL_SYNC_DEVICE_NAME": device_name,
    },
    "WorkingDirectory": str(repo_root),
    "RunAtLoad": True,
    "KeepAlive": True,
    "StandardOutPath": str(logs_dir / "skill-sync-operator-executor.out.log"),
    "StandardErrorPath": str(logs_dir / "skill-sync-operator-executor.err.log"),
}
plist.write_bytes(plistlib.dumps(payload, sort_keys=False))
PY

launchctl bootout "gui/$(id -u)" "$plist" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$plist"
launchctl enable "gui/$(id -u)/$label"

echo "operator_executor_launchd_ok=true"
echo "label=$label"
echo "url=http://$host:$port/healthz"
echo "allow_local_writes=$allow_local_writes"
echo "allow_publish=$allow_publish"
echo "device_id=$device_id"
echo "device_name=$device_name"
echo "plist=$plist"
