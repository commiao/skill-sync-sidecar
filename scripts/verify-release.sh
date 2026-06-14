#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PYTHONPATH_VALUE="${PYTHONPATH:-$ROOT_DIR/src}"
PYCACHE_PREFIX="${PYTHONPYCACHEPREFIX:-/private/tmp/skill-sync-pycache}"

cd "$ROOT_DIR"

export PYTHONPATH="$PYTHONPATH_VALUE"
export PYTHONPYCACHEPREFIX="$PYCACHE_PREFIX"

"$PYTHON_BIN" -m unittest discover -s tests
"$PYTHON_BIN" -m compileall -q src tests

PYTHON_BIN="$PYTHON_BIN" scripts/package-smoke.sh

if [ "${SKILL_SYNC_SKIP_OPS_STATUS:-}" != "1" ]; then
  "$PYTHON_BIN" -m skill_sync_sidecar ops-status --allow-new --fail-on-blocked --fail-on-error
fi

printf 'verify_release=ok\n'
