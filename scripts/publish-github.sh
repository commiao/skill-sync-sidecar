#!/usr/bin/env bash
set -euo pipefail

REMOTE_URL="${1:-${SKILL_SYNC_GITHUB_REMOTE:-git@github.com:commiao/skill-sync-sidecar.git}}"
REMOTE_NAME="${SKILL_SYNC_GITHUB_REMOTE_NAME:-origin}"
BRANCH="${SKILL_SYNC_GITHUB_BRANCH:-main}"
TAG="${SKILL_SYNC_RELEASE_TAG:-v0.1.3}"

if [ -n "$(git status --porcelain)" ]; then
  echo "refusing to publish with a dirty worktree" >&2
  git status --short >&2
  exit 2
fi

if ! git rev-parse --verify "$TAG" >/dev/null 2>&1; then
  echo "release tag not found: $TAG" >&2
  exit 2
fi

if git remote get-url "$REMOTE_NAME" >/dev/null 2>&1; then
  current_remote="$(git remote get-url "$REMOTE_NAME")"
  if [ "$current_remote" != "$REMOTE_URL" ]; then
    echo "remote $REMOTE_NAME already points to $current_remote" >&2
    echo "expected $REMOTE_URL" >&2
    exit 2
  fi
else
  git remote add "$REMOTE_NAME" "$REMOTE_URL"
fi

git push "$REMOTE_NAME" "$BRANCH"
git push "$REMOTE_NAME" "$TAG"

printf 'publish_github=ok\n'
printf 'remote=%s\n' "$REMOTE_URL"
printf 'branch=%s\n' "$BRANCH"
printf 'tag=%s\n' "$TAG"
