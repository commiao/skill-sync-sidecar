#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/openclaw-approved-push-quick.sh [--yes] [--no-refresh] [--source "oc-vps / OpenClaw"] [--max N]

One-command workflow for "openclaw writer_policy pending items":
1. Find actionable pending IDs from dashboard summary.
2. Run approved-push dry-run (default) or --yes publish.
3. Print a concise result and next step.

Options:
  --yes         publish mode (safe preview if omitted)
  --no-refresh  do not run publish-openclaw-peer-status automatically
  --source S    source prefix filter (default: "oc-vps / OpenClaw")
  --max N       limit to first N IDs
  --help        show help

Environment:
  SKILL_SYNC_MONITOR_URL or SKILL_SYNC_MONITOR_SUMMARY_FILE can be used by the same
  env names as openclaw-approved-push-batch-all.sh.
USAGE
}

MODE_FLAG="--dry-run"
MODE_NAME="dry-run"
NO_REFRESH=0
SOURCE_FILTER="oc-vps / OpenClaw"
MAX_COUNT=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes)
      MODE_FLAG="--yes"
      MODE_NAME="publish"
      ;;
    --no-refresh)
      NO_REFRESH=1
      ;;
    --source)
      shift
      if [ -z "${1:-}" ]; then
        echo "--source requires a value" >&2
        exit 2
      fi
      SOURCE_FILTER="$1"
      ;;
    --max)
      shift
      if [ -z "${1:-}" ] || ! printf '%s' "$1" | grep -Eq '^[0-9]+$'; then
        echo "--max requires a non-negative integer" >&2
        exit 2
      fi
      MAX_COUNT="$1"
      ;;
    --help)
      usage
      exit 0
      ;;
    --)
      shift
      break
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

SCRIPT_ROOT="$(cd "$(dirname "$0")" && pwd)"
BATCH_ALL="${SCRIPT_ROOT}/openclaw-approved-push-batch-all.sh"
BATCH="${SCRIPT_ROOT}/openclaw-approved-push-batch.sh"

if [ ! -x "$BATCH_ALL" ] || [ ! -x "$BATCH" ]; then
  echo "missing helper scripts, expect $BATCH_ALL and $BATCH" >&2
  exit 2
fi

tmp_summary="$(mktemp /tmp/skill-sync-openclaw-quick-summary.XXXXXX)"
tmp_batch="$(mktemp /tmp/skill-sync-openclaw-quick-batch.XXXXXX)"
trap 'rm -f "$tmp_summary" "$tmp_batch"' EXIT

if [ -n "$MAX_COUNT" ]; then
  MAX_FLAG=(--max "$MAX_COUNT")
else
  MAX_FLAG=()
fi

parse_ids() {
  local line="$1"
  printf '%s' "$line" | sed -n 's/^openclaw_pending_ids=//p'
}

echo "[1] 提取待发布候选..."
if ! (
  cd "$SCRIPT_ROOT/.."
  if [ -n "${SKILL_SYNC_MONITOR_SUMMARY_FILE:-}" ]; then
    export SKILL_SYNC_MONITOR_SUMMARY_FILE
  fi
  if [ -n "${SKILL_SYNC_MONITOR_URL:-}" ]; then
    export SKILL_SYNC_MONITOR_URL
  fi
  bash "$BATCH_ALL" --print-ids-only --source "$SOURCE_FILTER" ${MAX_FLAG[@]+"${MAX_FLAG[@]}"} > "$tmp_summary" 2>&1
) ; then
  echo "提取待发布候选失败；请先确认 NAS/网关可达。"
  cat "$tmp_summary"
  exit 2
fi

ids="$(parse_ids "$(grep -m1 '^openclaw_pending_ids=' "$tmp_summary" || true)")"
pending_count="$(grep -m1 '^openclaw_pending_count=' "$tmp_summary" | cut -d= -f2 || echo 0)"

if [ -z "$ids" ]; then
  echo "openclaw_pending_count=${pending_count:-0}"
  echo "没有检测到可发布的 writer_policy 项。"
  if [ -n "${pending_count:-}" ] && [ "$pending_count" -gt 0 ]; then
    echo "请检查上一步输出：脚本未返回可用 skill_id。"
  fi
  exit 0
fi

echo "openclaw_pending_count=${pending_count:-0}"
echo "openclaw_pending_ids=${ids}"
read -r -a id_arr <<< "$ids"

echo
if [ "$MODE_FLAG" = "--yes" ]; then
  echo "[2] 执行发布（已确认，实际写入）..."
  batch_mode=(--yes)
  if [ "$NO_REFRESH" -eq 1 ]; then
    batch_mode=(--yes)
  else
    batch_mode=(--yes --refresh-peer-status)
  fi
  (
    cd "$SCRIPT_ROOT/.."
    bash "$BATCH" "${batch_mode[@]}" "${id_arr[@]}" > "$tmp_batch" 2>&1
  )
else
  echo "[2] 执行 dry-run 预检..."
  (
    cd "$SCRIPT_ROOT/.."
    bash "$BATCH" --dry-run "${id_arr[@]}" > "$tmp_batch" 2>&1
  )
fi

json="$(python3 - "$tmp_batch" <<'PY'
import json
import re
import sys

text = open(sys.argv[1], encoding="utf-8").read()
matches = list(re.finditer(r'(?m)^\{', text))
for idx in range(len(matches)-1, -1, -1):
    start = matches[idx].start()
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except Exception:
        continue
    print(json.dumps(obj, ensure_ascii=False))
    break
else:
    sys.exit(1)
PY
)" || {
  echo "未能解析 helper JSON 输出，保留完整日志以便排查："
  cat "$tmp_batch"
  exit 2
}

python3 - <<'PY' "$tmp_batch" "$MODE_NAME" "$json" <<'PY2'
import json
import sys

raw = json.loads(sys.argv[3])
safe = bool(raw.get("safe_to_push", False))
approved = int(raw.get("approved", 0) or 0)
reason = (raw.get("reason") or "").strip()
mode = "发布" if sys.argv[2] == "publish" else "预检"
print(f"mode={mode}")
print(f"safe_to_push={str(safe).lower()}")
print(f"approved={approved}")
ids = raw.get("approved_skill_ids") or []
if isinstance(ids, list):
  print("approved_skill_ids=" + " ".join(str(i) for i in ids))
if reason:
  print(f"reason={reason}")

if not safe and approved == 0:
  print("结论=本次未写入；可能是非候选/状态已变更/权限未启用。")
  if sys.argv[2] == "dry-run":
    print("建议：如确认可发布，改用 --yes 重试。")
  else:
    print("建议：通常是候选已变更或冲突状态，建议等待状态稳定后重跑。")
elif approved == 0:
  print("结论=未新增发布条目；通常是因为候选项已被其它动作处理。")
elif safe and approved > 0 and sys.argv[2] == "dry-run":
  print("结论=预检通过，可改为 --yes 发布。")
else:
  print("结论=已按脚本返回写入；建议立即刷新 peer-status。")
PY2

if [ "$MODE_FLAG" = "--yes" ]; then
  echo "已执行发布动作，若仍有黄色待审可再次运行本脚本。"
fi
