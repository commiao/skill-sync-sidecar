#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENCLAW_SSH_TARGET="${OPENCLAW_SSH_TARGET:-root@100.79.177.102}"
OPENCLAW_CONNECT_TIMEOUT="${OPENCLAW_CONNECT_TIMEOUT:-10}"
OPENCLAW_RELEASE="${OPENCLAW_RELEASE:-peer-status-v1}"
OPENCLAW_PYTHON="${OPENCLAW_PYTHON:-/opt/skill-sync-sidecar/venv-0.1.3/bin/python}"
OPENCLAW_PEER_ID="${OPENCLAW_PEER_ID:-oc-vps}"
OUT_FILE="${OPENCLAW_PEER_STATUS_OUT:-$HOME/Library/Application Support/skill-sync-sidecar/peers/openclaw-status.json}"
LOG_PREFIX="${LOG_PREFIX:-skill-sync-openclaw-peer}"

mkdir -p "$(dirname "$OUT_FILE")"
tmp_file="$(mktemp "${OUT_FILE}.tmp.XXXXXX")"
tools_file="$(mktemp "${OUT_FILE}.tools.XXXXXX")"
trap 'rm -f "$tmp_file" "$tools_file"' EXIT

ssh -o BatchMode=yes -o ConnectTimeout="$OPENCLAW_CONNECT_TIMEOUT" "$OPENCLAW_SSH_TARGET" \
  "PYTHONPATH=/opt/skill-sync-sidecar/releases/${OPENCLAW_RELEASE}/src ${OPENCLAW_PYTHON} -m skill_sync_sidecar ops-status \
    --local-root /home/admin/clawd/skills \
    --remote-snapshot /opt/skill-sync-sidecar/cache/current-mac-pullonly \
    --base-record /opt/skill-sync-sidecar/state/openclaw-base-record.json \
    --state-file /opt/skill-sync-sidecar/state/openclaw-daemon-pullonly-state.json \
    --blocked-report /opt/skill-sync-sidecar/work/current-mac-pullonly/blocked-report/blocked-report.json \
    --allow-new \
    --writer-policy pull-only \
    --json" > "$tmp_file"

if ssh -o BatchMode=yes -o ConnectTimeout="$OPENCLAW_CONNECT_TIMEOUT" "$OPENCLAW_SSH_TARGET" \
  "PYTHONPATH=/opt/skill-sync-sidecar/releases/${OPENCLAW_RELEASE}/src ${OPENCLAW_PYTHON} -" > "$tools_file" <<'PY'
import json
from pathlib import Path

from skill_sync_sidecar.tool_status import build_device_status, build_device_tool_status, build_peer_capabilities

tool_roots = [
    ("openclaw", "OpenClaw", [Path("/home/admin/clawd/skills")], "OpenClaw 实际使用目录"),
]

payload = {
    "peer_status_version": 1,
    "device": build_device_status("oc-vps"),
    "capabilities": build_peer_capabilities(),
    "tools": build_device_tool_status(tool_roots),
}
print(json.dumps(payload, ensure_ascii=False))
PY
then
  python3 - "$tmp_file" "$tools_file" "$OPENCLAW_PEER_ID" <<'PY'
import json
import sys
from pathlib import Path

status_path = Path(sys.argv[1])
tools_path = Path(sys.argv[2])
peer_id = sys.argv[3]
status = json.loads(status_path.read_text(encoding="utf-8"))
tools = json.loads(tools_path.read_text(encoding="utf-8"))
if isinstance(status, dict) and isinstance(tools, dict):
    status.update(tools)
    status["peer_id"] = peer_id
    status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
else
  echo "${LOG_PREFIX}: warning: OpenClaw tool status unavailable; publishing legacy ops status" >&2
fi

python3 - "$tmp_file" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
if not isinstance(data, dict) or "health" not in data:
    raise SystemExit("peer status JSON does not contain health")
PY

mv "$tmp_file" "$OUT_FILE"
trap - EXIT
echo "${LOG_PREFIX}: wrote ${OUT_FILE}"
