#!/usr/bin/env bash
set -euo pipefail

local_root="${1:-$HOME/.cc-switch/skills}"
if [ ! -d "$local_root" ]; then
  echo "local skill root not found: $local_root" >&2
  exit 2
fi

python_bin="${PYTHON:-python3}"
export PYTHONPATH="${PYTHONPATH:-src}"

base="${SKILL_SYNC_LOCAL_DRYRUN_BASE:-/private/tmp/skill-sync-real-local-file-dryrun-$(date +%Y%m%d%H%M%S)}"
snapshot="$base/snapshot"
remote="$base/file-remote"
cache="$base/cache"
work="$base/work"
state="$base/state.json"
prefix="snapshots/current"
mkdir -p "$base"

"$python_bin" -m skill_sync_sidecar snapshot \
  --root cc-switch="$local_root" \
  --out "$snapshot" \
  --label local-real-dryrun \
  --json >"$base/snapshot.json"

"$python_bin" -m skill_sync_sidecar push \
  --snapshot-dir "$snapshot" \
  --remote "file://$remote" \
  --prefix "$prefix" \
  --yes \
  --json >"$base/push.json"

"$python_bin" -m skill_sync_sidecar sync-daemon \
  --local-root "$local_root" \
  --remote "file://$remote" \
  --prefix "$prefix" \
  --cache-dir "$cache" \
  --work-dir "$work" \
  --state-file "$state" \
  --dry-run \
  --max-cycles 1 \
  --json >"$base/daemon.json"

BASE="$base" "$python_bin" - <<'PY'
import json
import os
from pathlib import Path

base = Path(os.environ["BASE"])
snapshot = json.loads((base / "snapshot.json").read_text(encoding="utf-8"))
push = json.loads((base / "push.json").read_text(encoding="utf-8"))
daemon = json.loads((base / "daemon.json").read_text(encoding="utf-8"))
state = json.loads((base / "state.json").read_text(encoding="utf-8"))

print("local_real_dryrun_ok=true")
print(f"base={base}")
print(f"snapshot_total={snapshot['total']}")
print(f"push_files={push['files']}")
print(f"daemon_cycles_run={daemon['cycles_run']}")
print(f"daemon_cycle_status={daemon['cycles'][0]['status']}")
print(f"daemon_cycle_summary={json.dumps(daemon['cycles'][0]['summary'], ensure_ascii=False, sort_keys=True)}")
print(f"state_daemon_status={state['daemon_status']}")
PY
