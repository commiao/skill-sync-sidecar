#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec "$SCRIPT_DIR/watch-sync-health.sh" \
  --interval-seconds 1800 \
  --max-iterations 0

