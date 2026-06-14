#!/usr/bin/env bash
set -euo pipefail

prefix_base="${1:-skill-sync-sidecar-dev/smoke-$(date +%Y%m%d%H%M%S)}"
case "$prefix_base" in
  ""|"/"|cc-switch-sync|cc-switch-sync/*)
    echo "refusing unsafe WebDAV smoke prefix: $prefix_base" >&2
    exit 2
    ;;
esac

python_bin="${PYTHON:-python3}"
export PYTHONPATH="${PYTHONPATH:-src}"

base="${SKILL_SYNC_SMOKE_BASE:-/private/tmp/skill-sync-webdav-smoke-$(date +%Y%m%d%H%M%S)}"
mkdir -p "$base"

run_canary() {
  local empty="$base/canary-empty"
  local snapshot="$base/canary-snapshot"
  local cache="$base/canary-cache"
  local prefix="$prefix_base/canary"
  mkdir -p "$empty"

  "$python_bin" -m skill_sync_sidecar snapshot \
    --root canary="$empty" \
    --out "$snapshot" \
    --label webdav-canary \
    --json >/dev/null
  "$python_bin" -m skill_sync_sidecar push \
    --snapshot-dir "$snapshot" \
    --cc-switch-webdav \
    --prefix "$prefix" \
    --yes \
    --json >/dev/null
  "$python_bin" -m skill_sync_sidecar remote-status \
    --cc-switch-webdav \
    --prefix "$prefix" \
    --json
  "$python_bin" -m skill_sync_sidecar pull-cache \
    --cc-switch-webdav \
    --prefix "$prefix" \
    --out "$cache" \
    --json >/dev/null
}

run_sync_cycle_e2e() {
  local source="$base/cycle-source"
  local snapshot="$base/cycle-snapshot"
  local target="$base/cycle-target"
  local cache="$base/cycle-cache"
  local work="$base/cycle-work"
  local prefix="$prefix_base/cycle"
  mkdir -p "$source/demo-webdav-cycle" "$target"
  printf '%s\n' '---' 'name: demo-webdav-cycle' 'description: Synthetic WebDAV sync-cycle skill' '---' '' 'cycle body' >"$source/demo-webdav-cycle/SKILL.md"

  "$python_bin" -m skill_sync_sidecar snapshot \
    --root cc-switch="$source" \
    --out "$snapshot" \
    --label webdav-cycle \
    --json >/dev/null
  "$python_bin" -m skill_sync_sidecar push \
    --snapshot-dir "$snapshot" \
    --cc-switch-webdav \
    --prefix "$prefix" \
    --yes \
    --json >/dev/null
  "$python_bin" -m skill_sync_sidecar sync-cycle \
    --local-root "$target" \
    --cc-switch-webdav \
    --prefix "$prefix" \
    --cache-dir "$cache" \
    --work-dir "$work" \
    --allow-new \
    --yes \
    --json >/dev/null

  test -f "$target/demo-webdav-cycle/SKILL.md"
}

run_sync_daemon_e2e() {
  local source="$base/daemon-source"
  local snapshot="$base/daemon-snapshot"
  local target="$base/daemon-target"
  local cache="$base/daemon-cache"
  local work="$base/daemon-work"
  local state_file="$base/daemon-state.json"
  local prefix="$prefix_base/daemon"
  mkdir -p "$source/demo-webdav-daemon" "$target"
  printf '%s\n' '---' 'name: demo-webdav-daemon' 'description: Synthetic WebDAV sync-daemon skill' '---' '' 'daemon body' >"$source/demo-webdav-daemon/SKILL.md"

  "$python_bin" -m skill_sync_sidecar snapshot \
    --root cc-switch="$source" \
    --out "$snapshot" \
    --label webdav-daemon \
    --json >/dev/null
  "$python_bin" -m skill_sync_sidecar push \
    --snapshot-dir "$snapshot" \
    --cc-switch-webdav \
    --prefix "$prefix" \
    --yes \
    --json >/dev/null
  "$python_bin" -m skill_sync_sidecar sync-daemon \
    --local-root "$target" \
    --cc-switch-webdav \
    --prefix "$prefix" \
    --cache-dir "$cache" \
    --work-dir "$work" \
    --state-file "$state_file" \
    --allow-new \
    --yes \
    --max-cycles 1 \
    --json >/dev/null

  test -f "$target/demo-webdav-daemon/SKILL.md"
  test -f "$state_file"
}

run_canary >/tmp/skill-sync-webdav-smoke-canary.json
run_sync_cycle_e2e
run_sync_daemon_e2e

cat <<EOF
webdav_smoke_ok=true
base=$base
prefix_base=$prefix_base
canary_prefix=$prefix_base/canary
cycle_prefix=$prefix_base/cycle
daemon_prefix=$prefix_base/daemon
EOF
