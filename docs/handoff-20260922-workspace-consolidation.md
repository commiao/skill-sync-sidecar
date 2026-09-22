# Skill Sync Sidecar Handoff - 2026-09-22

This document makes `/Users/mac/work-ai/skill-sync-sidecar` the only source
worktree for future Skill Sync Sidecar work.

## Canonical Locations

- Source worktree: `/Users/mac/work-ai/skill-sync-sidecar`
- Remote: `git@github.com:commiao/skill-sync-sidecar.git`
- Source branch: `work/shared`, containing `origin/main` through `70ec677`
  plus the handoff commits. Read the current HEAD with `git rev-parse --short HEAD`.
- Installed Mac runtime: `/Users/mac/.local/share/skill-sync-sidecar/current`
  (release `70ec6770de2618df57d20d22f06d1e5ec89740dc`)
- Mac WebDAV mirror/cache: `/Users/mac/public-sync/skill-sync-sidecar-dev`
- NAS deployment root: `/volume1/docker/skill-sync-gateway`
- NAS dashboard: `http://100.123.208.32:8765`

The installed runtime and WebDAV paths are operational state, not source
worktrees. Do not move, delete, or repoint them as part of workspace cleanup.

## New Session Fast Start

Start from the canonical worktree and read this handoff before taking a write
action:

```bash
cd /Users/mac/work-ai/skill-sync-sidecar
git status -sb
git log --oneline -5
sed -n '1,180p' docs/handoff-20260922-workspace-consolidation.md
```

When creating a fresh checkout rather than reusing the canonical worktree,
start from the handoff branch:

```bash
git clone --branch work/shared git@github.com:commiao/skill-sync-sidecar.git
```

Then use read-only checks in this order:

```bash
# Local source/test baseline.
PYTHONPATH=src python3 -m unittest discover -s tests -q

# Current Gateway decision summary. This does not write WebDAV.
scripts/operator-status.sh

# NAS deployment and dashboard provenance. This does not restart containers.
SKILL_SYNC_NAS_HOST=100.123.208.32 \
SKILL_SYNC_NAS_SSH_USER=commiao \
/bin/bash scripts/validate-nas-sidecar.sh
```

Only after those checks should a session consider a publish, install, or NAS
deploy. The `Deploy-Standard Work In This Session` section below is the current
write boundary and takes precedence over any stale dashboard recommendation.

## Workspace Consolidation

The former source worktree
`/Users/mac/workspace_claudeCode/skill-sync-sidecar` was byte-for-byte equal
to this worktree at the same clean commit, and LaunchAgents point to the
installed release rather than either checkout. It can therefore be retired
after the archive move without losing source or runtime state.

Historical dashboard screenshots, the original project kickoff export, and
the early `skill-inventory` / `capability-reconciler` research are retained
under `archive/2026-09-22/`. The archive is intentionally gitignored: it is
local evidence, not deployable application source.

## Current Product State

- WebDAV is the central shared skill store.
- The NAS Gateway is a read-only dashboard/aggregator.
- Mac and OpenClaw publish peer status. Windows remains deferred.
- NAS DeepSeek Harness is a distinct device with a read-only observer.
  It reports Harness skills and Harness profile plugins separately.
- Last verified Harness observation: `0` skills and `3` plugins. Plugin
  dependencies are display-only and must never be published as skills.

The NAS deployment record `a4a94e3` (plugin inventory support) is historical
evidence, not a current health verdict. The source branch now includes the
later `4d8b781` monitor-noise fix and `70ec677` LaunchAgent reload fix, but
verify the live NAS commit before any new deployment instead of assuming it is
current.

## Verification Boundary - 2026-09-22

- The latest reported source verification ran 229 tests successfully.
- This execution environment cannot connect to `127.0.0.1:18765`, even though
  the Mac executor process was reported as listening there. It therefore does
  not establish an executor failure.
- This execution environment also cannot reach the NAS address. Gateway,
  deployment, and Harness health are currently **unverified**, not failed.

Repeat the local executor and NAS checks from an environment permitted to make
those connections. Do not turn an environment access restriction into a
service incident.

## Deploy-Standard Work In This Session

Source skill:

`/Users/mac/workspace_claudeCode/fleet-ops/skills/deploy-standard`

This `fleet-ops` source directory is the **content authority**. The sidecar
canonical root, tool installations, and eventual WebDAV package are derived
copies and must not be edited as competing sources. At last verification, the
central snapshot contained 107 skills and did not contain `deploy-standard`;
its current contents remain unverified, so it is not an authority for this
skill yet.

The source repository already has a user-owned edit to `SKILL.md`. This
session added an uncommitted adjacent `manifest.json` only; it declares:

- `skill_id`: `deploy-standard`
- `scope`: `global`
- targets: Claude Code, Codex, Cursor, and DeepSeek Harness

Local installation is complete:

- sidecar canonical root: `~/.cc-switch/skills/deploy-standard`
- Codex: `~/.codex/skills/deploy-standard`
- Cursor: `~/.cursor/skills-cursor/deploy-standard`
- Claude Code: `~/.claude/skills/deploy-standard`

Claude Code's former symlink-backed version was preserved at:

`~/.claude/skills/.skill-sync-backups/20260922-125900-154315/deploy-standard`

Current content comparison is not fully converged:

- `fleet-ops` source, cc-switch canonical copy, and Cursor copy match.
- Codex and Claude Code copies differ from the current source.

Do not overwrite those two divergent copies blindly. First have the source
owner confirm the current uncommitted `fleet-ops` version is intended, then
generate a read-only diff and install the confirmed source version through
sidecar with its existing backup records.

The central publish dry-run passed and would have added only this skill,
increasing the snapshot from 107 to 108 skills at that time. Actual WebDAV
publishing was blocked by the execution safety gate because the package
documents company Codeup and local deployment information. Do not bypass that
decision.
Continue only after the user explicitly authorizes this exact action:

`Allow the complete deploy-standard package to be published to the private WebDAV central repository and installed on NAS DeepSeek Harness.`

DeepSeek Harness installation is not yet implemented or performed. Its NAS
sidecar is intentionally observer-only with read-only binds. The safe follow-up
is a narrowly scoped, one-shot apply operation that downloads the approved
central snapshot, writes only `/data/home/.dsh/skills/deploy-standard`, creates
a backup/apply record, verifies the package hash, and does not restart
`dsh-personal`. Do not make the observer writable or add broad sync privileges.

## Known Follow-Up Items

1. Stabilize the `deploy-standard` authority: have the source owner confirm or
   commit the current `fleet-ops` edit, inspect the Codex/Claude differences,
   and update only from the confirmed source.
2. Obtain explicit WebDAV publication approval for `deploy-standard`, then
   publish only that package and verify its central hash.
3. Implement and dry-run the narrow NAS Harness apply path described above;
   execute it only for the approved `deploy-standard` package.
4. From an environment with local and NAS network access, verify the Mac
   executor health endpoint and NAS Gateway state. Treat them as unverified
   until then, not as failed.
5. Before any NAS deployment, validate current commit, Gateway health, agent
   logs, and `dsh-personal` uptime. Do not touch OpenClaw while working on NAS
   dashboard or Harness integration.

## Safety Rules

- Do not replace OpenClaw's system Python or restart its gateway for sidecar
  work.
- Do not convert OpenClaw from pull-only unattended sync to push-pull.
- Do not delete central skills to clear dashboard warnings.
- Do not treat Harness plugins as syncable skills.
- Preserve user edits in the `fleet-ops` source repository; no commit has been
  made there by this session. The source working tree is intentionally still
  dirty (`SKILL.md` plus the sidecar `manifest.json`).
