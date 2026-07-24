#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  SKILL_SYNC_NAS_HOST=100.123.208.32 \
  SKILL_SYNC_NAS_SSH_USER=commiao \
  [SKILL_SYNC_EXPECTED_COMMIT=...] \
  /bin/bash scripts/validate-nas-sidecar.sh

This runs:
1) /bin/bash scripts/check-nas-dashboard-remote.sh
2) /bin/bash scripts/verify-nas-gateway-deploy.sh
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

echo "step=remote_dashboard_check"
/bin/bash scripts/check-nas-dashboard-remote.sh

echo "step=nas_gateway_verify"
EXPECTED_COMMIT="${SKILL_SYNC_EXPECTED_COMMIT:-}"
if [ -n "${EXPECTED_COMMIT}" ]; then
  /bin/bash scripts/verify-nas-gateway-deploy.sh "${EXPECTED_COMMIT}"
else
  /bin/bash scripts/verify-nas-gateway-deploy.sh
fi
