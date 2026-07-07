#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${OPENCLAW_PYTHON:-/opt/skill-sync-sidecar/venv-0.1.3/bin/python}"
PEER_ID="${OPENCLAW_PEER_ID:-oc-vps}"
STATUS_PATH="${OPENCLAW_PEER_STATUS_PATH:-skill-sync-sidecar-peer-status/oc-vps.json}"
LOCAL_ROOT="${OPENCLAW_SKILL_ROOT:-/home/admin/clawd/skills}"
REMOTE_SNAPSHOT="${OPENCLAW_REMOTE_SNAPSHOT:-/opt/skill-sync-sidecar/cache/current-mac-pullonly}"
BASE_RECORD="${OPENCLAW_BASE_RECORD:-/opt/skill-sync-sidecar/state/openclaw-base-record.json}"
STATE_FILE="${OPENCLAW_STATE_FILE:-/opt/skill-sync-sidecar/state/openclaw-daemon-pullonly-state.json}"
STATUS_FILE="${OPENCLAW_PEER_STATUS_FILE:-/opt/skill-sync-sidecar/state/openclaw-peer-status.json}"
SKILL_SYNC_PREFIX="${SKILL_SYNC_PREFIX:-skill-sync-sidecar-dev/current-mac}"
LOG_PREFIX="${LOG_PREFIX:-skill-sync-openclaw-local-peer}"

mkdir -p "$(dirname "$STATUS_FILE")"
tmp_file="$(mktemp "${STATUS_FILE}.tmp.XXXXXX")"
tools_file="$(mktemp "${STATUS_FILE}.tools.XXXXXX")"
trap 'rm -f "$tmp_file" "$tools_file"' EXIT

PYTHONPATH="$ROOT_DIR/src" "$PYTHON_BIN" -m skill_sync_sidecar pull-cache \
  --cc-switch-webdav \
  --prefix "$SKILL_SYNC_PREFIX" \
  --out "$REMOTE_SNAPSHOT" \
  --json >/dev/null

PYTHONPATH="$ROOT_DIR/src" "$PYTHON_BIN" -m skill_sync_sidecar ops-status \
  --local-root "$LOCAL_ROOT" \
  --remote-snapshot "$REMOTE_SNAPSHOT" \
  --base-record "$BASE_RECORD" \
  --state-file "$STATE_FILE" \
  --allow-new \
  --writer-policy pull-only \
  --json > "$tmp_file"

PYTHONPATH="$ROOT_DIR/src" "$PYTHON_BIN" - "$PEER_ID" "$LOCAL_ROOT" > "$tools_file" <<'PY'
import json
import sys
from pathlib import Path

from skill_sync_sidecar.tool_status import build_device_status, build_device_tool_status, build_peer_capabilities

peer_id = sys.argv[1]
local_root = Path(sys.argv[2])
payload = {
    "peer_status_version": 1,
    "device": build_device_status(peer_id),
    "capabilities": build_peer_capabilities(),
    "tools": build_device_tool_status(
        [
            ("openclaw", "OpenClaw", [local_root], "OpenClaw 实际使用目录"),
        ]
    ),
}
print(json.dumps(payload, ensure_ascii=False))
PY

PYTHONPATH="$ROOT_DIR/src" "$PYTHON_BIN" - "$tmp_file" "$tools_file" "$PEER_ID" <<'PY'
import json
import sys
from pathlib import Path

status_path = Path(sys.argv[1])
tools_path = Path(sys.argv[2])
peer_id = sys.argv[3]

status = json.loads(status_path.read_text(encoding="utf-8"))
tools = json.loads(tools_path.read_text(encoding="utf-8"))
if not isinstance(status, dict):
    raise SystemExit("ops-status JSON is not an object")
if not isinstance(tools, dict):
    raise SystemExit("tool-status JSON is not an object")

status.update(tools)
status["peer_id"] = peer_id
status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

PYTHONPATH="$ROOT_DIR/src" "$PYTHON_BIN" - "$tmp_file" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if data.get("peer_status_version") != 1:
    raise SystemExit("peer status JSON is not v1")
if not data.get("health"):
    raise SystemExit("peer status JSON does not contain health")
tools = data.get("tools")
if not isinstance(tools, list) or not tools or tools[0].get("id") != "openclaw":
    raise SystemExit("peer status JSON does not contain OpenClaw tool status")
PY

mv "$tmp_file" "$STATUS_FILE"
trap - EXIT

PYTHONPATH="$ROOT_DIR/src" "$PYTHON_BIN" -m skill_sync_sidecar publish-peer-status \
  --cc-switch-webdav \
  --peer-id "$PEER_ID" \
  --status-path "$STATUS_PATH" \
  --status-file "$STATUS_FILE"

echo "${LOG_PREFIX}: wrote ${STATUS_FILE}"
