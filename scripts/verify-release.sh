#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PYTHONPATH_VALUE="${PYTHONPATH:-$ROOT_DIR/src}"
PYCACHE_PREFIX="${PYTHONPYCACHEPREFIX:-/private/tmp/skill-sync-pycache}"
PROFILE="${1:-full}"

case "$PROFILE" in
  full|nas-dashboard-only) ;;
  -h|--help)
    cat <<'EOF'
Usage: scripts/verify-release.sh [full|nas-dashboard-only]

full                 Full package release; requires a green local sync status.
nas-dashboard-only   Read-only NAS Gateway dashboard release. It accepts only
                     the dashboard contract allowlist and still runs the full
                     tests, compile check, and package smoke test.
EOF
    exit 0
    ;;
  *)
    echo "unknown release profile: $PROFILE" >&2
    exit 64
    ;;
esac

cd "$ROOT_DIR"

export PYTHONPATH="$PYTHONPATH_VALUE"
export PYTHONPYCACHEPREFIX="$PYCACHE_PREFIX"

"$PYTHON_BIN" -m unittest discover -s tests
"$PYTHON_BIN" -m compileall -q src tests

PYTHON_BIN="$PYTHON_BIN" scripts/package-smoke.sh

if [ "$PROFILE" = "nas-dashboard-only" ]; then
  "$PYTHON_BIN" scripts/verify-nas-dashboard-release-contract.py --repo "$ROOT_DIR" --base origin/main
else
  if [ "${SKILL_SYNC_SKIP_OPS_STATUS:-}" != "1" ]; then
    "$PYTHON_BIN" -m skill_sync_sidecar ops-status --allow-new --fail-on-blocked --fail-on-error
  fi
fi

printf 'verify_release=ok profile=%s\n' "$PROFILE"
