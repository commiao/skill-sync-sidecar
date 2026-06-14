#!/usr/bin/env bash
set -euo pipefail

if [ "${SKILL_SYNC_ALLOW_PRIVATE_WEBDAV_UPLOAD:-}" != "1" ]; then
  echo "refusing to upload private skills without SKILL_SYNC_ALLOW_PRIVATE_WEBDAV_UPLOAD=1" >&2
  exit 2
fi

local_root="${1:-$HOME/.cc-switch/skills}"
prefix="${2:-skill-sync-sidecar-dev/real-$(hostname -s)-$(date +%Y%m%d%H%M%S)}"

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

python_bin="${PYTHON:-python3}"
export PYTHONPATH="${PYTHONPATH:-src}"

base="${SKILL_SYNC_REAL_WEBDAV_BASE:-/private/tmp/skill-sync-real-webdav-$(date +%Y%m%d%H%M%S)}"
snapshot="$base/snapshot"
cache="$base/cache"
work="$base/work"
state="$base/state.json"
mkdir -p "$base"

"$python_bin" -m skill_sync_sidecar snapshot \
  --root cc-switch="$local_root" \
  --out "$snapshot" \
  --label real-webdav-dryrun \
  --json >"$base/snapshot.json"

"$python_bin" -m skill_sync_sidecar push \
  --snapshot-dir "$snapshot" \
  --cc-switch-webdav \
  --prefix "$prefix" \
  --yes \
  --json >"$base/push.json"

"$python_bin" -m skill_sync_sidecar remote-status \
  --cc-switch-webdav \
  --prefix "$prefix" \
  --json >"$base/remote-status.json"

"$python_bin" -m skill_sync_sidecar pull-cache \
  --cc-switch-webdav \
  --prefix "$prefix" \
  --out "$cache" \
  --json >"$base/pull-cache.json"

"$python_bin" -m skill_sync_sidecar sync-daemon \
  --local-root "$local_root" \
  --cc-switch-webdav \
  --prefix "$prefix" \
  --cache-dir "$cache" \
  --work-dir "$work" \
  --state-file "$state" \
  --dry-run \
  --max-cycles 1 \
  --json >"$base/daemon.json"

BASE="$base" PREFIX="$prefix" "$python_bin" - <<'PY'
import json
import os
from pathlib import Path

base = Path(os.environ["BASE"])
prefix = os.environ["PREFIX"]
snapshot = json.loads((base / "snapshot.json").read_text(encoding="utf-8"))
push = json.loads((base / "push.json").read_text(encoding="utf-8"))
status = json.loads((base / "remote-status.json").read_text(encoding="utf-8"))
pull = json.loads((base / "pull-cache.json").read_text(encoding="utf-8"))
daemon = json.loads((base / "daemon.json").read_text(encoding="utf-8"))
state = json.loads((base / "state.json").read_text(encoding="utf-8"))

print("real_webdav_dryrun_ok=true")
print(f"base={base}")
print(f"prefix={prefix}")
print(f"snapshot_total={snapshot['total']}")
print(f"push_files={push['files']}")
print(f"push_bytes={push['bytes']}")
print(f"remote_status_ok={str(status['ok']).lower()}")
print(f"remote_total={status['total']}")
print(f"pull_total={pull['total']}")
print(f"daemon_cycles_run={daemon['cycles_run']}")
print(f"daemon_cycle_status={daemon['cycles'][0]['status']}")
print(f"daemon_cycle_summary={json.dumps(daemon['cycles'][0]['summary'], ensure_ascii=False, sort_keys=True)}")
print(f"state_daemon_status={state['daemon_status']}")
PY
