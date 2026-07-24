#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/openclaw-approved-push-batch-all.sh [--yes] [--no-refresh] [--max N] [--dry-run] [--source "oc-vps / OpenClaw"]

Publish all current OpenClaw writer_policy pending items discovered from monitor API in one command.

Default mode is dry-run (safe preview). Use --yes to perform approved-push.

Options:
  --yes          publish instead of dry-run
  --no-refresh   do not publish OpenClaw peer status after --yes
  --max N        limit to first N skill ids
  --dry-run      explicit dry-run (same as default)
  --print-ids-only  only print extracted ids (no batch invocation)
  --source S     filter source prefix (default: "oc-vps / OpenClaw")
  --help         show this help

Environment:
  SKILL_SYNC_MONITOR_URL or SUMMARY_FILE required:
    - SKILL_SYNC_MONITOR_URL defaults to http://100.123.208.32:8765/api/overview
    - SKILL_SYNC_MONITOR_SUMMARY_FILE, if set, reads monitor JSON from local file

Notes:
  Only candidate items are exported:
  - source starts with the provided --source
  - category == writer_policy
  - status_action in {push, push_new, local_new}

This workflow still intentionally excludes conflict/delete categories.
USAGE
}

MODE_FLAG="--dry-run"
NO_REFRESH=0
MAX_COUNT=""
SOURCE_FILTER="oc-vps / OpenClaw"
PRINT_ONLY=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes)
      MODE_FLAG="--yes"
      ;;
    --no-refresh)
      NO_REFRESH=1
      ;;
    --dry-run)
      MODE_FLAG="--dry-run"
      ;;
    --print-ids-only)
      PRINT_ONLY=1
      ;;
    --max)
      shift
      if [ -z "${1:-}" ]; then
        echo "--max requires an integer" >&2
        exit 2
      fi
      MAX_COUNT="$1"
      ;;
    --source)
      shift
      if [ -z "${1:-}" ]; then
        echo "--source requires a value" >&2
        exit 2
      fi
      SOURCE_FILTER="$1"
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
      echo "unexpected argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
  
done

if [ -n "${SKILL_SYNC_MONITOR_SUMMARY_FILE:-}" ]; then
  MONITOR_SUMMARY_FILE="$SKILL_SYNC_MONITOR_SUMMARY_FILE"
  if [ ! -r "$MONITOR_SUMMARY_FILE" ]; then
    echo "SUMMARY_FILE not readable: $MONITOR_SUMMARY_FILE" >&2
    exit 2
  fi
  DASHBOARD_JSON="$(cat "$MONITOR_SUMMARY_FILE")"
else
  MONITOR_URL="${SKILL_SYNC_MONITOR_URL:-http://100.123.208.32:8765/api/overview}"
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl not found" >&2
    exit 2
  fi
  DASHBOARD_JSON="$(curl -fsSL "$MONITOR_URL")"
fi

if [ -n "$MAX_COUNT" ] && ! printf '%s' "$MAX_COUNT" | grep -Eq '^[0-9]+$'; then
  echo "--max must be a positive integer" >&2
  exit 2
fi

tmp_summary=$(mktemp /tmp/skill-sync-openclaw-all.XXXXXX)
trap 'rm -f "$tmp_summary"' EXIT
printf '%s' "$DASHBOARD_JSON" > "$tmp_summary"

IDS=$(python3 - "$tmp_summary" "$MAX_COUNT" "$SOURCE_FILTER" <<'PY'
import json
import sys

path = sys.argv[1]
max_count = int(sys.argv[2]) if sys.argv[2] else None
source_filter = sys.argv[3]

summary = json.loads(open(path, encoding="utf-8").read())
blocked = summary.get("dashboard", {}).get("blocked_items") or []
ids = [
    item.get("skill_id")
    for item in blocked
    if isinstance(item, dict)
    and item.get("source", "").startswith(source_filter)
    and item.get("category") == "writer_policy"
    and item.get("status_action") in {"push", "push_new", "local_new"}
]
ids = [i for i in ids if i]
if max_count:
    ids = ids[:max_count]
print(" ".join(ids))
PY
)

if [ -z "$IDS" ]; then
  echo "openclaw_pending_ids="
  echo "no actionable openclaw writer_policy entries found"
  exit 0
fi

echo "openclaw_pending_ids=$IDS"
if [ "$PRINT_ONLY" -eq 1 ]; then
  printf '%s\n' "$IDS"
  exit 0
fi

CMD=(bash scripts/openclaw-approved-push-batch.sh)
if [ "$MODE_FLAG" = "--yes" ]; then
  CMD+=(--yes)
else
  CMD+=(--dry-run)
fi
if [ "${SKILL_SYNC_APPROVED_PUSH_REFRESH_STATUS:-0}" = "1" ]; then
  CMD+=(--refresh-peer-status)
elif [ "$MODE_FLAG" = "--yes" ] && [ "$NO_REFRESH" -eq 0 ]; then
  CMD+=(--refresh-peer-status)
fi

# shellcheck disable=SC2086
CMD+=( $IDS )

echo "command=${CMD[*]}"
"${CMD[@]}"
