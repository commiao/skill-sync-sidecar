#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
OUT_DIR="${OUT_DIR:-$ROOT_DIR/dist}"
RELEASE_TAG="${RELEASE_TAG:-}"

cd "$ROOT_DIR"

if [ -z "$RELEASE_TAG" ]; then
  RELEASE_TAG="$(git describe --tags --exact-match 2>/dev/null || true)"
fi

if [ -z "$RELEASE_TAG" ]; then
  echo "RELEASE_TAG is required when HEAD is not exactly tagged" >&2
  exit 2
fi

if ! git rev-parse --verify "$RELEASE_TAG^{commit}" >/dev/null 2>&1; then
  echo "release tag not found: $RELEASE_TAG" >&2
  exit 2
fi

VERSION="${RELEASE_TAG#v}"
mkdir -p "$OUT_DIR"

rm -f \
  "$OUT_DIR/skill_sync_sidecar-$VERSION-"*.whl \
  "$OUT_DIR/skill-sync-sidecar-$VERSION-source.tar.gz" \
  "$OUT_DIR/skill-sync-sidecar-$VERSION.bundle"

"$PYTHON_BIN" -m pip wheel --no-deps --no-build-isolation . -w "$OUT_DIR"

git archive --format=tar.gz \
  --prefix="skill-sync-sidecar-$VERSION/" \
  -o "$OUT_DIR/skill-sync-sidecar-$VERSION-source.tar.gz" \
  "$RELEASE_TAG"

git bundle create "$OUT_DIR/skill-sync-sidecar-$VERSION.bundle" "$RELEASE_TAG"
git bundle verify "$OUT_DIR/skill-sync-sidecar-$VERSION.bundle"

printf 'release_assets=ok\n'
printf 'tag=%s\n' "$RELEASE_TAG"
printf 'out_dir=%s\n' "$OUT_DIR"
find "$OUT_DIR" -maxdepth 1 -type f \( \
  -name "skill_sync_sidecar-$VERSION-*.whl" \
  -o -name "skill-sync-sidecar-$VERSION-source.tar.gz" \
  -o -name "skill-sync-sidecar-$VERSION.bundle" \
\) -print | sort
