#!/usr/bin/env bash
set -euo pipefail

if [ "${SKILL_SYNC_ALLOW_PRIVATE_WEBDAV_UPLOAD:-}" != "1" ]; then
  echo "refusing to upload private skills without SKILL_SYNC_ALLOW_PRIVATE_WEBDAV_UPLOAD=1" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON:-python3}"
local_root="${SKILL_SYNC_LOCAL_ROOT:-$HOME/.cc-switch/skills}"
device_name="$(hostname -s | tr -c 'A-Za-z0-9._-' '-' | sed 's/-$//')"
prefix="${SKILL_SYNC_PREFIX:-skill-sync-sidecar-dev/current-${device_name}}"
mode="${SKILL_SYNC_DAEMON_MODE:-yes}"
interval_seconds="${SKILL_SYNC_INTERVAL_SECONDS:-300}"
writer_policy="${SKILL_SYNC_WRITER_POLICY:-push-pull}"

case "$mode" in
  dry-run|yes) ;;
  *)
    echo "SKILL_SYNC_DAEMON_MODE must be dry-run or yes" >&2
    exit 2
    ;;
esac

case "$writer_policy" in
  push-pull|pull-only|push-only|no-writes) ;;
  *)
    echo "SKILL_SYNC_WRITER_POLICY must be push-pull, pull-only, push-only, or no-writes" >&2
    exit 2
    ;;
esac

case "$prefix" in
  ""|"/"|cc-switch-sync|cc-switch-sync/*)
    echo "refusing unsafe WebDAV prefix: $prefix" >&2
    exit 2
    ;;
esac

if [ ! -d "$local_root" ]; then
  echo "local skill root not found: $local_root" >&2
  exit 2
fi

app_dir="$HOME/Library/Application Support/skill-sync-sidecar"
cache_dir="$HOME/Library/Caches/skill-sync-sidecar/cache"
work_dir="$app_dir/work"
state_file="$app_dir/state.json"
base_record_file="$app_dir/base-record.json"
preflight_state="$app_dir/preflight-state.json"
logs_dir="$HOME/Library/Logs"
launch_agents_dir="$HOME/Library/LaunchAgents"
plist_path="$launch_agents_dir/com.skill-sync-sidecar.plist"
run_base="${SKILL_SYNC_INSTALL_BASE:-/private/tmp/skill-sync-install-current-$(date +%Y%m%d%H%M%S)}"
snapshot_dir="$run_base/snapshot"
pull_cache="$run_base/pull-cache"

mkdir -p "$app_dir" "$cache_dir" "$work_dir" "$logs_dir" "$launch_agents_dir" "$run_base"

export PYTHONPATH="$repo_root/src"

"$python_bin" -m skill_sync_sidecar snapshot \
  --root cc-switch="$local_root" \
  --out "$snapshot_dir" \
  --label "current-${device_name}" \
  --json >"$run_base/snapshot.json"

"$python_bin" -m skill_sync_sidecar push \
  --snapshot-dir "$snapshot_dir" \
  --cc-switch-webdav \
  --prefix "$prefix" \
  --yes \
  --json >"$run_base/push.json"

"$python_bin" -m skill_sync_sidecar pull-cache \
  --cc-switch-webdav \
  --prefix "$prefix" \
  --out "$pull_cache" \
  --json >"$run_base/pull-cache.json"

BASE_RECORD_FILE="$base_record_file" PULL_CACHE="$pull_cache" LOCAL_ROOT="$local_root" PREFIX="$prefix" "$python_bin" - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

index = json.loads((Path(os.environ["PULL_CACHE"]) / "index.json").read_text(encoding="utf-8"))
record = {
    "protocol_version": 0,
    "record_type": "skill-sync-base",
    "sync_id": "launchd-initial-base",
    "created_at": datetime.now(timezone.utc).isoformat(),
    "target_root": str(Path(os.environ["LOCAL_ROOT"]).expanduser().resolve()),
    "remote_prefix": os.environ["PREFIX"],
    "snapshot_id": index.get("snapshot_id"),
    "applied": [
        {
            "skill_id": skill.get("skill_id"),
            "content_hash": skill.get("content_hash"),
        }
        for skill in index.get("skills", [])
        if skill.get("skill_id") and skill.get("content_hash")
    ],
}
target = Path(os.environ["BASE_RECORD_FILE"])
target.parent.mkdir(parents=True, exist_ok=True)
tmp = target.with_name(f"{target.name}.tmp")
tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp.replace(target)
PY

"$python_bin" -m skill_sync_sidecar sync-daemon \
  --local-root "$local_root" \
  --cc-switch-webdav \
  --prefix "$prefix" \
  --cache-dir "$cache_dir" \
  --work-dir "$work_dir" \
  --state-file "$preflight_state" \
  --base-record-file "$base_record_file" \
  --last-applied-record "$base_record_file" \
  --writer-policy "$writer_policy" \
  --dry-run \
  --max-cycles 1 \
  --json >"$run_base/preflight-daemon.json"

daemon_flag="--$mode"

PLIST_PATH="$plist_path" PYTHON_BIN="$python_bin" REPO_ROOT="$repo_root" LOCAL_ROOT="$local_root" PREFIX="$prefix" CACHE_DIR="$cache_dir" WORK_DIR="$work_dir" STATE_FILE="$state_file" BASE_RECORD_FILE="$base_record_file" DAEMON_FLAG="$daemon_flag" INTERVAL_SECONDS="$interval_seconds" WRITER_POLICY="$writer_policy" LOGS_DIR="$logs_dir" "$python_bin" - <<'PY'
import os
from pathlib import Path
from plistlib import dump

plist = {
    "Label": "com.skill-sync-sidecar",
    "ProgramArguments": [
        os.environ["PYTHON_BIN"],
        "-m",
        "skill_sync_sidecar",
        "sync-daemon",
        "--local-root",
        os.environ["LOCAL_ROOT"],
        "--cc-switch-webdav",
        "--prefix",
        os.environ["PREFIX"],
        "--cache-dir",
        os.environ["CACHE_DIR"],
        "--work-dir",
        os.environ["WORK_DIR"],
        "--state-file",
        os.environ["STATE_FILE"],
        "--base-record-file",
        os.environ["BASE_RECORD_FILE"],
        "--last-applied-record",
        os.environ["BASE_RECORD_FILE"],
        "--writer-policy",
        os.environ["WRITER_POLICY"],
        os.environ["DAEMON_FLAG"],
        "--interval-seconds",
        os.environ["INTERVAL_SECONDS"],
    ],
    "EnvironmentVariables": {
        "PYTHONPATH": str(Path(os.environ["REPO_ROOT"]) / "src"),
    },
    "RunAtLoad": True,
    "KeepAlive": False,
    "StandardOutPath": str(Path(os.environ["LOGS_DIR"]) / "skill-sync-sidecar.out.log"),
    "StandardErrorPath": str(Path(os.environ["LOGS_DIR"]) / "skill-sync-sidecar.err.log"),
}

target = Path(os.environ["PLIST_PATH"])
target.parent.mkdir(parents=True, exist_ok=True)
tmp = target.with_name(f"{target.name}.tmp")
with tmp.open("wb") as fh:
    dump(plist, fh, sort_keys=False)
tmp.replace(target)
PY

plutil -lint "$plist_path"
launchctl bootout "gui/$(id -u)" "$plist_path" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$plist_path"
launchctl kickstart -k "gui/$(id -u)/com.skill-sync-sidecar"
sleep 5

RUN_BASE="$run_base" PREFIX="$prefix" PLIST_PATH="$plist_path" STATE_FILE="$state_file" PREFLIGHT_STATE="$preflight_state" BASE_RECORD_FILE="$base_record_file" MODE="$mode" "$python_bin" - <<'PY'
import json
import os
from pathlib import Path

run_base = Path(os.environ["RUN_BASE"])
snapshot = json.loads((run_base / "snapshot.json").read_text(encoding="utf-8"))
push = json.loads((run_base / "push.json").read_text(encoding="utf-8"))
preflight = json.loads((run_base / "preflight-daemon.json").read_text(encoding="utf-8"))
state_path = Path(os.environ["STATE_FILE"])
state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

print("launchd_install_ok=true")
print(f"mode={os.environ['MODE']}")
print(f"prefix={os.environ['PREFIX']}")
print(f"plist={os.environ['PLIST_PATH']}")
print(f"state_file={state_path}")
print(f"base_record_file={os.environ['BASE_RECORD_FILE']}")
print(f"writer_policy={state.get('writer_policy')}")
print(f"run_base={run_base}")
print(f"snapshot_total={snapshot['total']}")
print(f"push_files={push['files']}")
print(f"push_bytes={push['bytes']}")
print(f"preflight_cycles_run={preflight['cycles_run']}")
print(f"preflight_summary={json.dumps(preflight['cycles'][0]['summary'], ensure_ascii=False, sort_keys=True)}")
print(f"state_exists={str(state_path.exists()).lower()}")
print(f"state_daemon_status={state.get('daemon_status')}")
print(f"state_cycles_run={state.get('cycles_run')}")
print(f"state_last_summary={json.dumps(state.get('cycles', [{}])[-1].get('summary', {}), ensure_ascii=False, sort_keys=True)}")
PY
