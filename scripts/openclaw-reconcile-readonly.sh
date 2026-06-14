#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${OPENCLAW_HOST:-root@oc-vps-aliyun-us}"
OPENCLAW_SKILL_ROOT="${OPENCLAW_SKILL_ROOT:-/home/admin/clawd/skills}"
PREFIX="${SKILL_SYNC_PREFIX:-skill-sync-sidecar-dev/current-mac}"
OUT="${1:-/private/tmp/openclaw-skill-sync-validate/openclaw-$(date +%Y%m%d%H%M%S)}"
PREVIOUS_INVENTORY="${PREVIOUS_INVENTORY:-}"
REMOTE_CACHE="${REMOTE_CACHE:-}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

mkdir -p "$OUT"

ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" \
  "python3 - '$OPENCLAW_SKILL_ROOT' --source openclaw --include-files" \
  < "$ROOT_DIR/scripts/remote-inventory-py36.py" \
  > "$OUT/openclaw-inventory.json"

if [[ -n "$REMOTE_CACHE" ]]; then
  if [[ ! -f "$REMOTE_CACHE/index.json" ]]; then
    printf 'REMOTE_CACHE has no index.json: %s\n' "$REMOTE_CACHE" >&2
    exit 2
  fi
  printf '{"reused_remote_cache": "%s"}\n' "$REMOTE_CACHE" > "$OUT/pull-cache.json"
  remote_snapshot="$REMOTE_CACHE"
else
  PYTHONPATH="$ROOT_DIR/src" "$PYTHON_BIN" -m skill_sync_sidecar pull-cache \
    --cc-switch-webdav \
    --prefix "$PREFIX" \
    --out "$OUT/remote-cache" \
    --json \
    > "$OUT/pull-cache.json"
  remote_snapshot="$OUT/remote-cache"
fi

args=(
  -m skill_sync_sidecar reconcile-report
  --local-inventory "$OUT/openclaw-inventory.json"
  --remote-snapshot "$remote_snapshot"
  --label "openclaw-$(date +%Y%m%d%H%M%S)"
  --out "$OUT/reconcile"
)

if [[ -n "$PREVIOUS_INVENTORY" ]]; then
  args+=(--previous-local-inventory "$PREVIOUS_INVENTORY")
fi

PYTHONPATH="$ROOT_DIR/src" "$PYTHON_BIN" "${args[@]}" | tee "$OUT/reconcile-summary.txt"

PYTHONPATH="$ROOT_DIR/src" "$PYTHON_BIN" -m skill_sync_sidecar openclaw-gate \
  --report "$OUT/reconcile/reconcile-report.json" \
  --json \
  > "$OUT/openclaw-gate.json"

PYTHONPATH="$ROOT_DIR/src" "$PYTHON_BIN" -m skill_sync_sidecar openclaw-gate \
  --report "$OUT/reconcile/reconcile-report.json" \
  | tee "$OUT/openclaw-gate.txt"

printf 'out=%s\n' "$OUT"
