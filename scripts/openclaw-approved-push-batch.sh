#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/openclaw-approved-push-batch.sh [--dry-run|--yes] [--no-allow-new] SKILL_ID...

Safely publish explicitly reviewed OpenClaw-local skill changes to WebDAV.

Default mode is --dry-run. The script:
  1. refreshes the OpenClaw WebDAV cache,
  2. regenerates the pull-only blocked report,
  3. runs approved-push for the explicit SKILL_ID list.

It does not change OpenClaw's unattended pull-only service policy and does not
restart systemd units.

Environment overrides:
  OPENCLAW_SSH_TARGET       default: root@100.79.177.102
  OPENCLAW_CONNECT_TIMEOUT  default: 20
  OPENCLAW_RELEASE          default: peer-status-v1
  OPENCLAW_PYTHON           default: /opt/skill-sync-sidecar/venv-0.1.3/bin/python
  SKILL_SYNC_PREFIX         default: skill-sync-sidecar-dev/current-mac
  SKILL_SYNC_APPROVAL_LABEL default: approved-push-openclaw
USAGE
}

mode="--dry-run"
allow_new=1
skill_ids=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)
      mode="--dry-run"
      ;;
    --yes)
      mode="--yes"
      ;;
    --no-allow-new)
      allow_new=0
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
SKILL_SYNC_APPROVAL_LABEL="${SKILL_SYNC_APPROVAL_LABEL:-approved-push-openclaw}"

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
out_dir="/opt/skill-sync-sidecar/work/current-mac-pullonly/${SKILL_SYNC_APPROVAL_LABEL}-${timestamp}"

echo "openclaw_approved_push_batch_mode=${mode#--}"
echo "skills=${skill_ids[*]}"
echo "out=${out_dir}"

run_remote "${python_cmd[@]}" pull-cache \
  --cc-switch-webdav \
  --prefix "$SKILL_SYNC_PREFIX" \
  --out /opt/skill-sync-sidecar/cache/current-mac-pullonly \
  --json

run_remote "${python_cmd[@]}" sync-cycle \
  --local-root /home/admin/clawd/skills \
  --target mixed-scope-root \
  --last-applied-record /opt/skill-sync-sidecar/state/openclaw-base-record.json \
  --cache-dir /opt/skill-sync-sidecar/cache/current-mac-pullonly \
  --work-dir /opt/skill-sync-sidecar/work/current-mac-pullonly \
  --allow-new \
  --writer-policy pull-only \
  --cc-switch-webdav \
  --prefix "$SKILL_SYNC_PREFIX" \
  --dry-run \
  --json

filter_file="$(mktemp "/tmp/skill-sync-approved-push-filter.XXXXXX.json")"
trap 'rm -f "$filter_file"' EXIT
run_remote cat /opt/skill-sync-sidecar/work/current-mac-pullonly/blocked-report/blocked-report.json > "$filter_file"

mapfile -t filtered_skill_ids < <(python3 - "$filter_file" "${skill_ids[@]}" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
requested = sys.argv[2:]
present = {
    item.get("skill_id")
    for item in report.get("items", [])
    if item.get("category") == "writer_policy"
    and item.get("status_action") in {"push", "push_new", "local_new"}
}
for skill_id in requested:
    if skill_id in present:
        print(skill_id)
PY
)

if [ "${#filtered_skill_ids[@]}" -ne "${#skill_ids[@]}" ]; then
  echo "requested_skills=${skill_ids[*]}"
  echo "current_blocked_publish_skills=${filtered_skill_ids[*]:-}"
  echo "stale_or_non_publish_skills_skipped=true"
fi

if [ "${#filtered_skill_ids[@]}" -eq 0 ]; then
  python3 - "$filter_file" "$mode" "${skill_ids[@]}" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
mode = sys.argv[2].removeprefix("--")
requested = sys.argv[3:]
print(json.dumps({
    "ok": True,
    "mode": "publish" if mode == "yes" else "dry_run",
    "safe_to_push": True,
    "approved": 0,
    "approved_skill_ids": [],
    "requested_skill_ids": requested,
    "stale_skipped_skill_ids": requested,
    "blocked_report_total": report.get("total", 0),
    "reason": "none of the requested skills are currently blocked publish candidates",
}, ensure_ascii=False, indent=2))
PY
  exit 0
fi

skill_ids=("${filtered_skill_ids[@]}")

approved_args=(
  "${python_cmd[@]}" approved-push
  --local-root /home/admin/clawd/skills
  --remote-snapshot /opt/skill-sync-sidecar/cache/current-mac-pullonly
  --last-applied-record /opt/skill-sync-sidecar/state/openclaw-base-record.json
  --blocked-report /opt/skill-sync-sidecar/work/current-mac-pullonly/blocked-report/blocked-report.json
)

for skill_id in "${skill_ids[@]}"; do
  approved_args+=(--skill-id "$skill_id")
done

if [ "$allow_new" = "1" ]; then
  approved_args+=(--allow-new)
fi

approved_args+=(
  --base-record-out /opt/skill-sync-sidecar/state/openclaw-base-record.json
  --out "$out_dir"
  --cc-switch-webdav
  --prefix "$SKILL_SYNC_PREFIX"
  "$mode"
  --json
)

run_remote "${approved_args[@]}"
