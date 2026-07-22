#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/openclaw-conflict-package.sh [SKILL_ID...]

Generate read-only conflict review packages on OpenClaw for selected skills.
This does not write WebDAV, does not change /home/admin/clawd/skills, and does
not restart OpenClaw.

Environment overrides:
  OPENCLAW_SSH_TARGET       default: root@100.79.177.102
  OPENCLAW_CONNECT_TIMEOUT  default: 20
  OPENCLAW_RELEASE          default: peer-status-v1
  OPENCLAW_PYTHON           default: /opt/skill-sync-sidecar/venv-0.1.3/bin/python
  SKILL_SYNC_PREFIX         default: skill-sync-sidecar-dev/current-mac
USAGE
}

skill_ids=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --*)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      skill_ids+=("$1")
      ;;
  esac
  shift
done

OPENCLAW_SSH_TARGET="${OPENCLAW_SSH_TARGET:-root@100.79.177.102}"
OPENCLAW_CONNECT_TIMEOUT="${OPENCLAW_CONNECT_TIMEOUT:-20}"
OPENCLAW_RELEASE="${OPENCLAW_RELEASE:-peer-status-v1}"
OPENCLAW_PYTHON="${OPENCLAW_PYTHON:-/opt/skill-sync-sidecar/venv-0.1.3/bin/python}"
SKILL_SYNC_PREFIX="${SKILL_SYNC_PREFIX:-skill-sync-sidecar-dev/current-mac}"

remote_env=(
  "PYTHONPATH=/opt/skill-sync-sidecar/releases/${OPENCLAW_RELEASE}/src"
)
python_cmd=("${remote_env[@]}" "$OPENCLAW_PYTHON" -m skill_sync_sidecar)
ssh_cmd=(ssh -o BatchMode=yes -o ConnectTimeout="$OPENCLAW_CONNECT_TIMEOUT" "$OPENCLAW_SSH_TARGET")
admin_prefix=(sudo -iu admin env)

run_remote() {
  local quoted=""
  local arg
  for arg in "$@"; do
    printf -v quoted '%s%q ' "$quoted" "$arg"
  done
  "${ssh_cmd[@]}" "${admin_prefix[*]} ${quoted}"
}

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
out_dir="/opt/skill-sync-sidecar/work/current-mac-pullonly/conflicts-${timestamp}"
cache_dir="/opt/skill-sync-sidecar/cache/current-mac-pullonly"
local_result_json="$(mktemp "${TMPDIR:-/tmp}/skill-sync-openclaw-conflicts.XXXXXX")"
trap 'rm -f "$local_result_json"' EXIT

echo "openclaw_conflict_package_mode=read_only"
echo "skills=${skill_ids[*]:-all}"
echo "out=${out_dir}"

run_remote "${python_cmd[@]}" pull-cache \
  --cc-switch-webdav \
  --prefix "$SKILL_SYNC_PREFIX" \
  --out "$cache_dir" \
  --json

run_remote "${python_cmd[@]}" conflict-package \
  --local-root /home/admin/clawd/skills \
  --remote-snapshot "$cache_dir" \
  --last-applied-record /opt/skill-sync-sidecar/state/openclaw-base-record.json \
  --out "$out_dir" \
  --json > "$local_result_json"

python3 - "$local_result_json" "${skill_ids[@]}" <<'PY'
import json
import sys

result_json = sys.argv[1]
requested = [item for item in sys.argv[2:] if item]
payload = json.load(open(result_json, encoding="utf-8"))
packages = payload.get("packages", [])
if requested:
    wanted = set(requested)
    packages = [item for item in packages if item.get("skill_id") in wanted]
print(json.dumps({
    "ok": True,
    "record_type": "skill-sync-openclaw-conflict-package",
    "mode": "conflict_package",
    "read_only": True,
    "skill_ids": requested,
    "total_conflicts": len(packages),
    "out": payload.get("out"),
    "packages": packages,
}, ensure_ascii=False, indent=2))
PY
