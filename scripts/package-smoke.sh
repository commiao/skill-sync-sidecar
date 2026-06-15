#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
OUT_DIR="${OUT_DIR:-$ROOT_DIR/dist}"
WORK_DIR="${WORK_DIR:-/private/tmp/skill-sync-package-smoke}"
export PIP_NO_CACHE_DIR=1

rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR/skill-root/demo" "$OUT_DIR"

cat > "$WORK_DIR/skill-root/demo/SKILL.md" <<'EOF'
---
name: demo
description: Demo skill for package smoke validation
---

Smoke test body.
EOF

cd "$ROOT_DIR"
"$PYTHON_BIN" -m pip wheel --no-deps --no-build-isolation . -w "$OUT_DIR"

wheel="$(ls -t "$OUT_DIR"/skill_sync_sidecar-*.whl | head -n 1)"
"$PYTHON_BIN" -m venv "$WORK_DIR/venv"
env -u PYTHONPATH "$WORK_DIR/venv/bin/python" -m pip install --no-deps --force-reinstall "$wheel"

env -u PYTHONPATH "$WORK_DIR/venv/bin/skill-sync" --version
env -u PYTHONPATH "$WORK_DIR/venv/bin/skill-sync" status --root "smoke=$WORK_DIR/skill-root"
env -u PYTHONPATH "$WORK_DIR/venv/bin/skill-sync" snapshot --root "smoke=$WORK_DIR/skill-root" --out "$WORK_DIR/snapshot"
env -u PYTHONPATH "$WORK_DIR/venv/bin/skill-sync" remote-status --remote "file://$WORK_DIR/snapshot"

printf 'package_smoke=ok\n'
printf 'wheel=%s\n' "$wheel"
printf 'work_dir=%s\n' "$WORK_DIR"
