#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/openclaw-approved-push-diagnose.sh [--yes] [--no-refresh] SKILL_ID...

One-command diagnostics for "OpenClaw approved-push returns approved=0 / no change".
Workflow:
  1) Run openclaw-approved-push-batch in dry-run (or --yes).
  2) Show result + reason.
  3) Refresh OpenClaw peer-status (unless --no-refresh).
  4) Print blocked queue snapshot for manual inspection.

Options:
  --yes         switch approved-push from dry-run to publish
  --no-refresh  skip publish-openclaw-peer-status
  -h, --help    show this help

If approved is 0 and reason is
"none of the requested skills are currently blocked publish candidates",
that usually means the items are already handled or changed out of queue-time.
Use this tool again later when the writer has finished syncing.
USAGE
}

MODE_FLAG="--dry-run"
REFRESH_PEER_STATUS=1
SKILL_IDS=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes)
      MODE_FLAG="--yes"
      ;;
    --no-refresh)
      REFRESH_PEER_STATUS=0
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
      SKILL_IDS+=("$1")
      ;;
  esac
  shift
done

if [ "${#SKILL_IDS[@]}" -eq 0 ]; then
  echo "at least one SKILL_ID is required" >&2
  usage >&2
  exit 2
fi

script_root="$(cd "$(dirname "$0")" && pwd)"
helper="${script_root}/openclaw-approved-push-batch.sh"
if [ ! -x "$helper" ]; then
  echo "helper not executable: $helper" >&2
  exit 2
fi

refresh_script="${script_root}/publish-openclaw-peer-status.sh"
blocked_script="${script_root}/blocked-queue.sh"

run_with_parse() {
  local out_file="$1"
  shift
  local exit_code
  set +e
  "$@" > "$out_file" 2>&1
  exit_code=$?
  set -e
  cat "$out_file"
  return $exit_code
}

parse_result_json() {
  local out_file="$1"
  python3 - "$out_file" <<'PY'
import json
import sys
import re
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
decoder = json.JSONDecoder()
starts = [m.start() for m in re.finditer(r"(?m)^\{", text)]
for offset in reversed(starts):
    try:
        parsed, end = decoder.raw_decode(text[offset:])
    except Exception:
        continue
    print(json.dumps(parsed, ensure_ascii=False))
    break
else:
    raise SystemExit(1)
PY
}

batch_out="$(mktemp "/tmp/skill-sync-approved-push-diagnose.XXXXXX")"
run_out="$(mktemp "/tmp/skill-sync-approved-push-diagnose-run.XXXXXX")"
trap 'rm -f "$batch_out" "$run_out"' EXIT

echo "========== openclaw-approved-push diagnose =========="
echo "mode=${MODE_FLAG#--}"
echo "skills=${SKILL_IDS[*]}"

echo
echo "[1/3] 触发 approved-push"
if ! run_with_parse "$batch_out" bash "$helper" "$MODE_FLAG" "${SKILL_IDS[@]}"; then
  echo
  echo "命令返回非 0，可尝试先检查 SSH/环境后重试。"
  parse_exit=$?
else
  parse_exit=0
fi

if ! parsed_json="$(parse_result_json "$batch_out")"; then
  echo "未能解析 helper 的 JSON 输出，请用 -x 打开脚本确认。"
  exit 1
fi

safe_to_push="$(python3 -c 'import json,sys; obj=json.loads(sys.argv[1]); print("true" if bool(obj.get("safe_to_push", False)) else "false")' "$parsed_json")"
approved="$(python3 -c 'import json,sys; obj=json.loads(sys.argv[1]); print(obj.get("approved", 0))' "$parsed_json")"
reason="$(python3 -c 'import json,sys; obj=json.loads(sys.argv[1]); print(obj.get("reason", ""))' "$parsed_json")"
approved_skill_ids="$(python3 -c 'import json,sys; obj=json.loads(sys.argv[1]); ids=obj.get("approved_skill_ids", []); print(" ".join(ids) if isinstance(ids, list) else "")' "$parsed_json")"

echo
echo "[2/3] 结果摘要"
echo "safe_to_push=${safe_to_push}"
echo "approved=${approved:-0}"
echo "approved_skill_ids=${approved_skill_ids}"
if [ -n "$reason" ]; then
  echo "reason=${reason}"
fi

if [ "$parse_exit" -ne 0 ]; then
  echo
  echo "执行失败，已保留原始输出在临时日志。请在本地先修复错误再继续。"
  exit $parse_exit
fi

if [ "$MODE_FLAG" = "--dry-run" ]; then
  if [ "${safe_to_push}" = "true" ] && [ "${approved:-0}" = "0" ]; then
    echo
    echo "解释：目前这批技能已不在当前 blocked publish 队列中，可能本地修改已被处理或尚未稳定。"
    echo "建议：按队列波次继续观察，或稍后重试一次。"
  elif [ "${approved:-0}" -eq 0 ]; then
    echo
    echo "解释：本次 dry-run 阶段未写入任何内容，请先查看推荐命令/原因。"
  else
    echo
    echo "当前可写预检通过，可用 --yes 执行正式发布。"
  fi
fi

if [ "$MODE_FLAG" = "--yes" ] && [ "$approved_skill_ids" != "" ]; then
  echo
  echo "本次发布已执行，继续观察被处理技能是否从 blocked-queue 消失。"
fi

if [ "$REFRESH_PEER_STATUS" -eq 1 ] && [ -x "$refresh_script" ]; then
  echo
  echo "[3/3] 刷新 OpenClaw peer-status"
  if bash "$refresh_script" > "$run_out" 2>&1; then
    echo "peer-status publish: ok"
  else
    echo "peer-status publish: failed"
  fi
  cat "$run_out"
fi

if [ -x "$blocked_script" ]; then
  echo
  echo "[4/3] 当前 blocked queue（核查本地是否仍有待审项）"
  bash "$blocked_script"
else
  echo "[4/3] skipped: blocked queue script missing: $blocked_script"
fi
