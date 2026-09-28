#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/openclaw-approved-push-batch.sh [--dry-run|--yes] [--no-allow-new] [--allow-conflict-local-wins] [--refresh-peer-status] [--expect SKILL_ID=LOCAL_HASH]... SKILL_ID...

Safely publish explicitly reviewed OpenClaw-local skill changes to WebDAV.

Default mode is --dry-run. The script:
  1. refreshes the OpenClaw WebDAV cache,
  2. regenerates the pull-only blocked report,
  3. runs approved-push for the explicit SKILL_ID list.
If you pass --refresh-peer-status, it publishes peer status after a successful
--yes publish (so the pending queue can clear faster on NAS).

--expect pins a skill to the OpenClaw local hash seen during review. A skill
whose current local hash differs is skipped (reported as changed_skipped_skill_ids)
while the remaining skills are still processed.

It does not change OpenClaw's unattended pull-only service policy and does not
restart systemd units.

Environment overrides:
  OPENCLAW_SSH_TARGET       default: root@100.79.177.102
  OPENCLAW_CONNECT_TIMEOUT  default: 20
  OPENCLAW_RELEASE          default: peer-status-v1
  OPENCLAW_PYTHON           default: /opt/skill-sync-sidecar/venv-0.1.3/bin/python
  SKILL_SYNC_PREFIX         default: skill-sync-sidecar-dev/current-mac
  SKILL_SYNC_APPROVAL_LABEL default: approved-push-openclaw
  SKILL_SYNC_APPROVED_PUSH_REFRESH_STATUS
                          set to 1 to auto-refresh OpenClaw peer status after --yes
USAGE
}

mode="--dry-run"
allow_new=1
allow_conflict_local_wins=0
refresh_peer_status=0
skill_ids=()
expectations=()
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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
    --allow-conflict-local-wins)
      allow_conflict_local_wins=1
      ;;
    --refresh-peer-status)
      refresh_peer_status=1
      ;;
    --expect)
      if [ "$#" -lt 2 ] || [[ "$2" != *=?* ]]; then
        echo "--expect requires SKILL_ID=LOCAL_HASH" >&2
        exit 2
      fi
      expectations+=("$2")
      shift
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
SKILL_SYNC_APPROVED_PUSH_REFRESH_STATUS="${SKILL_SYNC_APPROVED_PUSH_REFRESH_STATUS:-${refresh_peer_status}}"

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

filter_file="$(mktemp "${TMPDIR:-/tmp}/skill-sync-approved-push-filter.XXXXXX")"
approved_output_file="$(mktemp "${TMPDIR:-/tmp}/skill-sync-approved-push-output.XXXXXX")"
trap 'rm -f "$filter_file" "$approved_output_file"' EXIT
run_remote cat /opt/skill-sync-sidecar/work/current-mac-pullonly/blocked-report/blocked-report.json > "$filter_file"

requested_skill_ids=("${skill_ids[@]}")

filtered_output="$(python3 - "$filter_file" "$allow_conflict_local_wins" "${#expectations[@]}" ${expectations[@]+"${expectations[@]}"} "${skill_ids[@]}" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
allow_conflict_local_wins = sys.argv[2] == "1"
expectation_count = int(sys.argv[3])
expectations = dict(raw.split("=", 1) for raw in sys.argv[4:4 + expectation_count])
requested = sys.argv[4 + expectation_count:]
present = {
    item.get("skill_id"): item
    for item in report.get("items", [])
    if (
        item.get("category") == "writer_policy"
        and item.get("status_action") in {"push", "push_new", "local_new"}
    )
    or (
        allow_conflict_local_wins
        and item.get("category") == "conflict"
        and item.get("status_action") == "conflict"
    )
}
for skill_id in requested:
    item = present.get(skill_id)
    if item is None:
        print(f"stale\t{skill_id}")
    elif skill_id in expectations and expectations[skill_id] != item.get("local_hash"):
        print(f"changed\t{skill_id}")
    else:
        print(f"publish\t{skill_id}")
PY
)"
filtered_skill_ids=()
stale_skill_ids=()
changed_skill_ids=()
while IFS=$'\t' read -r kind skill_id; do
  case "$kind" in
    publish) filtered_skill_ids+=("$skill_id") ;;
    stale) stale_skill_ids+=("$skill_id") ;;
    changed) changed_skill_ids+=("$skill_id") ;;
  esac
done <<< "$filtered_output"

if [ "${#filtered_skill_ids[@]}" -ne "${#skill_ids[@]}" ]; then
  echo "requested_skills=${skill_ids[*]}"
  echo "current_blocked_publish_skills=${filtered_skill_ids[*]:-}"
  echo "stale_or_non_publish_skills_skipped=${stale_skill_ids[*]:-}"
  echo "changed_since_review_skills_skipped=${changed_skill_ids[*]:-}"
fi

print_batch_result() {
  python3 - "$filter_file" "$1" "$mode" "$allow_conflict_local_wins" \
    "$(IFS=,; echo "${requested_skill_ids[*]}")" \
    "$(IFS=,; echo "${stale_skill_ids[*]:-}")" \
    "$(IFS=,; echo "${changed_skill_ids[*]:-}")" <<'PY'
import json
import sys
from json import JSONDecoder
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
approved_output = Path(sys.argv[2]).read_text(encoding="utf-8") if sys.argv[2] else ""
mode = sys.argv[3].removeprefix("--")
allow_conflict_local_wins = sys.argv[4] == "1"
requested, stale, changed = ([name for name in raw.split(",") if name] for raw in sys.argv[5:8])

result = None
decoder = JSONDecoder()
index = 0
while True:
    start = approved_output.find("{", index)
    if start < 0:
        break
    try:
        value, end = decoder.raw_decode(approved_output[start:])
    except json.JSONDecodeError:
        index = start + 1
        continue
    if isinstance(value, dict):
        result = value
    index = start + end

if result is None:
    result = {
        "ok": True,
        "mode": "publish" if mode == "yes" else "dry_run",
        "safe_to_push": True,
        "approved": 0,
        "approved_skill_ids": [],
        "allow_conflict_local_wins": allow_conflict_local_wins,
        "reason": "none of the requested skills are currently publishable at the reviewed version",
    }
result["requested_skill_ids"] = requested
result["stale_skipped_skill_ids"] = stale
result["changed_skipped_skill_ids"] = changed
result["blocked_report_total"] = report.get("total", 0)
print(json.dumps(result, ensure_ascii=False, indent=2))
PY
}

if [ "${#filtered_skill_ids[@]}" -eq 0 ]; then
  print_batch_result ""
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

if [ "$allow_conflict_local_wins" = "1" ]; then
  approved_args+=(--allow-conflict-local-wins)
fi

approved_args+=(
  --base-record-out /opt/skill-sync-sidecar/state/openclaw-base-record.json
  --out "$out_dir"
  --cc-switch-webdav
  --prefix "$SKILL_SYNC_PREFIX"
  "$mode"
  --json
)

approved_rc=0
run_remote "${approved_args[@]}" > "$approved_output_file" || approved_rc=$?
if [ "$approved_rc" -ne 0 ]; then
  cat "$approved_output_file"
  exit "$approved_rc"
fi
print_batch_result "$approved_output_file"

if [ "$mode" = "--yes" ] && [ "${SKILL_SYNC_APPROVED_PUSH_REFRESH_STATUS}" = "1" ]; then
  if bash "${repo_root}/scripts/publish-openclaw-peer-status.sh"; then
    echo "peer_status_refresh=ok"
  else
    echo "peer_status_refresh=failed"
  fi
fi
