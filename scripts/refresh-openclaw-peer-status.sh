#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENCLAW_SSH_TARGET="${OPENCLAW_SSH_TARGET:-root@100.79.177.102}"
OPENCLAW_CONNECT_TIMEOUT="${OPENCLAW_CONNECT_TIMEOUT:-10}"
OPENCLAW_RELEASE="${OPENCLAW_RELEASE:-2e4499f}"
OPENCLAW_PYTHON="${OPENCLAW_PYTHON:-/opt/skill-sync-sidecar/venv-0.1.3/bin/python}"
OUT_FILE="${OPENCLAW_PEER_STATUS_OUT:-$HOME/Library/Application Support/skill-sync-sidecar/peers/openclaw-status.json}"
LOG_PREFIX="${LOG_PREFIX:-skill-sync-openclaw-peer}"

mkdir -p "$(dirname "$OUT_FILE")"
tmp_file="${OUT_FILE}.tmp"

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
echo "${LOG_PREFIX}: wrote ${OUT_FILE}"
