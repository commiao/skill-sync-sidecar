#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON:-python3}"

peer_id="${SKILL_SYNC_OPENCLAW_PEER_ID:-oc-vps}"
status_path="${SKILL_SYNC_OPENCLAW_PEER_STATUS_PATH:-skill-sync-sidecar-peer-status/oc-vps.json}"
status_file="${OPENCLAW_PEER_STATUS_OUT:-$HOME/Library/Application Support/skill-sync-sidecar/peers/openclaw-status.json}"

OPENCLAW_PEER_STATUS_OUT="$status_file" "$repo_root/scripts/refresh-openclaw-peer-status.sh"

PYTHONPATH="$repo_root/src" "$python_bin" -m skill_sync_sidecar publish-peer-status \
  --cc-switch-webdav \
  --peer-id "$peer_id" \
  --status-path "$status_path" \
  --status-file "$status_file"
