#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/openclaw-restore-from-central.sh [--dry-run|--yes] SKILL_ID...

Restore selected skills from the WebDAV central snapshot into OpenClaw's local
skill root. This never deletes central files and never restarts OpenClaw.

Environment overrides:
  OPENCLAW_SSH_TARGET       default: root@100.79.177.102
  OPENCLAW_CONNECT_TIMEOUT  default: 20
  OPENCLAW_RELEASE          default: peer-status-v1
  OPENCLAW_PYTHON           default: /opt/skill-sync-sidecar/venv-0.1.3/bin/python
  SKILL_SYNC_PREFIX         default: skill-sync-sidecar-dev/current-mac
USAGE
}

mode="--dry-run"
skill_ids=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)
      mode="--dry-run"
      ;;
    --yes)
      mode="--yes"
      ;;
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

if [ "${#skill_ids[@]}" -eq 0 ]; then
  echo "at least one SKILL_ID is required" >&2
  usage >&2
  exit 2
fi

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
out_dir="/opt/skill-sync-sidecar/work/current-mac-pullonly/central-restore-${timestamp}"
cache_dir="/opt/skill-sync-sidecar/cache/current-mac-pullonly"
stage_root="${out_dir}/stage"
stage_json="${out_dir}/stage.json"
local_stage_json="$(mktemp "${TMPDIR:-/tmp}/skill-sync-openclaw-stage.XXXXXX")"
local_result_json="$(mktemp "${TMPDIR:-/tmp}/skill-sync-openclaw-restore.XXXXXX")"
trap 'rm -f "$local_stage_json" "$local_result_json"' EXIT

echo "openclaw_central_restore_mode=${mode#--}"
echo "skills=${skill_ids[*]}"
echo "out=${out_dir}"

run_remote mkdir -p "$out_dir"

run_remote "${python_cmd[@]}" pull-cache \
  --cc-switch-webdav \
  --prefix "$SKILL_SYNC_PREFIX" \
  --out "$cache_dir" \
  --json

run_remote "${python_cmd[@]}" stage \
  --snapshot-dir "$cache_dir" \
  --out "$stage_root" \
  --clean \
  --json > "$local_stage_json"

"${ssh_cmd[@]}" "${admin_prefix[*]} mkdir -p $(printf '%q' "$out_dir") && cat > $(printf '%q' "$stage_json")" < "$local_stage_json"

staged_dir="$(python3 - "$local_stage_json" "$stage_root" <<'PY'
import json
import sys
from pathlib import PurePosixPath

stage_json = sys.argv[1]
stage_root = sys.argv[2]
data = json.loads(open(stage_json, encoding="utf-8").read())
print(PurePosixPath(stage_root) / str(data["snapshot_id"]))
PY
)"

apply_args=(
  "${python_cmd[@]}" apply
  --staged-dir "$staged_dir"
  --target mixed-scope-root
  --target-root /home/admin/clawd/skills
)

for skill_id in "${skill_ids[@]}"; do
  apply_args+=(--skill-id "$skill_id")
done

if [ "$mode" = "--yes" ]; then
  apply_args+=(--yes)
else
  apply_args+=(--dry-run)
fi
apply_args+=(--json)

run_remote "${apply_args[@]}" > "$local_result_json"

python3 - "$mode" "$local_result_json" "${skill_ids[@]}" <<'PY'
import json
import sys

mode = sys.argv[1].removeprefix("--")
result_json = sys.argv[2]
skill_ids = sys.argv[3:]
payload = json.load(open(result_json, encoding="utf-8"))
allowed = int(payload.get("allowed") or payload.get("total_applied") or 0)
if mode == "yes":
    payload = {
        **payload,
        "skipped": [],
        "applied": payload.get("applied", []),
    }
else:
    payload = {
        **payload,
        "items": [item for item in payload.get("items", []) if item.get("allowed")],
    }
print(json.dumps({
    "ok": True,
    "record_type": "skill-sync-openclaw-central-restore",
    "mode": "restore" if mode == "yes" else "dry_run",
    "dry_run": mode != "yes",
    "safe_to_restore": True,
    "skill_ids": skill_ids,
    "planned": allowed if mode != "yes" else None,
    "restored": allowed if mode == "yes" else None,
    "result_path": result_json,
    "result": payload,
}, ensure_ascii=False, indent=2))
PY
