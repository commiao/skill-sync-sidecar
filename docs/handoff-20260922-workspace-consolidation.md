# Skill Sync Sidecar Workspace Handoff

This handoff records the 2026-09-22 workspace consolidation. Check live Git and
runtime state before making a release decision; commit IDs, skill counts, and
health reports from the original handoff are historical.

## Source and runtime boundaries

- Canonical shared source checkout: `/Users/mac/work-ai/skill-sync-sidecar`, on
  `work/shared`. Preserve other users' changes there.
- For a task branch, use a separate clean worktree from current `origin/main`.
  Merge through a reviewed pull request and release only a commit on main.
- Mac installed runtime: `/Users/mac/.local/share/skill-sync-sidecar/current`.
  Its `RELEASE.json` identifies the actual installed commit. LaunchAgents must
  point to `current/`, not a fixed release SHA or development checkout.
- Mac WebDAV mirror: `/Users/mac/public-sync/skill-sync-sidecar-dev`.
  The desktop WebDAV client uploads this mirror to the private server. Verify
  server state separately with `remote-status --cc-switch-webdav`.
- NAS Gateway deployment root: `/volume1/docker/skill-sync-gateway`; Gateway
  URL: `http://100.123.208.32:8765`. The Gateway is read-only.
- NAS DeepSeek Harness is a separate device. Its observer remains read-only;
  profile plugins are not syncable skills.

The installed release, WebDAV mirror, and NAS deployment directory are runtime
state, not source worktrees. Do not move or edit them as workspace cleanup.
Historical evidence from the former workspace is kept under the gitignored
`archive/2026-09-22/` directory.

## Session start and verification

Before changing files, read this handoff and inspect the live checkout:

```sh
cd /Users/mac/work-ai/skill-sync-sidecar
git status -sb
git log --oneline -5
GIT_TERMINAL_PROMPT=0 git ls-remote --heads origin main
```

Use the current source and installed release to check each deployment surface:

```sh
scripts/verify-release.sh full
python3 /Users/mac/.local/share/fleet-ops/current/bin/mac-release.py \
  skill-sync-sidecar --repo /Users/mac/work-ai/skill-sync-sidecar --check
SKILL_SYNC_NAS_HOST=100.123.208.32 \
SKILL_SYNC_NAS_SSH_USER=commiao \
  /bin/bash scripts/validate-nas-sidecar.sh
```

Run the full release gate in a clean checkout of the exact main commit to be
published. The Mac check above reports the installed commit; a green status
does not by itself mean the new commit has been deployed. The NAS validation
reports its independently deployed commit and current Gateway health.

## Release safety

- Follow the `deploy-standard` skill and the service's `docs/release.md` and
  `docs/nas-gateway-deployment.md` contracts. Compare the intended commit with
  real `origin/main` before release.
- The Mac and NAS are separate release surfaces. Verify both after deployment.
  Do not treat a NAS dashboard-only exception as permission to skip the full
  gate for Mac installation or sync behavior changes.
- Before any NAS deployment, read the deployed commit, Gateway health, agent
  logs, and `dsh-personal` uptime. Preserve its read-only observer.
- Keep OpenClaw unattended sync `pull-only`; do not restart its gateway, change
  system Python, or publish local differences merely to clear yellow status.
- Never delete central skills to clear a device-side deletion warning. Review
  each conflict against local, central, and common-base content.
- `fleet-ops/skills/deploy-standard` is the source of that skill. Tool roots,
  the central snapshot, and NAS Harness are installed copies. Compare their
  hashes before changing them.

The original 2026-09-22 observations and one-time authorization boundary are
preserved in Git history. They do not describe the current runtime state.
