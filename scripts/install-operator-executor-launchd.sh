#!/usr/bin/env bash
set -euo pipefail

label="${SKILL_SYNC_EXECUTOR_LABEL:-com.skill-sync-sidecar.operator-executor}"
repo_root="${SKILL_SYNC_EXECUTOR_REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
host="${SKILL_SYNC_EXECUTOR_HOST:-127.0.0.1}"
port="${SKILL_SYNC_EXECUTOR_PORT:-18765}"
logs_dir="${SKILL_SYNC_EXECUTOR_LOGS_DIR:-$HOME/Library/Logs}"
plist="$HOME/Library/LaunchAgents/${label}.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$logs_dir"

python3 - "$plist" "$label" "$repo_root" "$host" "$port" "$logs_dir" <<'PY'
import plistlib
import sys
from pathlib import Path

plist = Path(sys.argv[1])
label = sys.argv[2]
repo_root = Path(sys.argv[3]).resolve()
host = sys.argv[4]
port = sys.argv[5]
logs_dir = Path(sys.argv[6]).expanduser()
payload = {
    "Label": label,
    "ProgramArguments": [
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
    ],
    "EnvironmentVariables": {
        "PYTHONPATH": str(repo_root / "src"),
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
echo "plist=$plist"
