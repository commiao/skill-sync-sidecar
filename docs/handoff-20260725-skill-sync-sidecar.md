# Skill Sync Sidecar Handoff - 2026-07-25

This handoff is for continuing the `skill-sync-sidecar` work in a new Codex
session.

## Current Objective

Keep advancing the Skill Sync Sidecar toward a simple, reliable private-skill
management and sync product:

- WebDAV is the canonical storage layer.
- NAS Gateway is the always-on read-only dashboard/aggregator.
- Each device has an Agent/client that reports real local tool state.
- Users should operate local skills from a simple UI, with explicit publish to
  the central shared library.
- Windows Agent is intentionally skipped for now.

## Repository And Deployment

- Local repo: `/Users/mac/workspace_codex/skill-sync-sidecar`
- Remote repo: `git@github.com:commiao/skill-sync-sidecar.git`
- Branch: `main`
- Latest local/remote commit verified before handoff: `d65bf09`
- Latest commit message: `Add quick openclaw approved-push workflow helper`
- NAS deploy dir: `/volume1/docker/skill-sync-gateway`
- NAS gateway: `http://100.123.208.32:8765`
- Report portal: `http://100.123.208.32:17172/portal`
- NAS docker binary:
  `/var/packages/ContainerManager/target/usr/bin/docker`

## Latest Verified Status

Verified with:

```bash
SKILL_SYNC_EXPECTED_COMMIT=d65bf09 scripts/validate-nas-sidecar.sh
```

Result:

- NAS reachable: yes
- Deployed commit: `d65bf09`
- Commit match: true
- `skill-sync-gateway`: running and healthy
- `skill-sync-monitor`: running
- Portal: `HTTP/1.1 200 OK`
- WebDAV dashboard artifact exists
- Monitor report: `monitor_health=green`, `alerts=0`, `warnings=0`, `info=1`

Important latest nuance:

- Gateway deployed version is correct.
- Current dashboard status is `yellow`, not green.
- Current blocked count is `1`.
- Operator message says the remaining issue is a version difference for
  `finance-auto-bookkeeping`.
- The new quick approved-push helper reports:

```text
openclaw_pending_count=0
没有检测到可发布的 writer_policy 项。
```

Interpretation: the remaining `finance-auto-bookkeeping` item is not a normal
OpenClaw `writer_policy` push candidate. Do not keep clicking the one-click
publish path. The next session should inspect this one version-difference item
and decide whether to restore shared-library version, save OpenClaw version, or
handle manually.

## Completed Work

### Infrastructure And Sync Mechanism

- WebDAV remains the storage base.
- NAS Gateway is deployed as the read-only aggregation/dashboard layer.
- Gateway no longer guesses remote tool installation from the NAS container.
- Peer status v1 exists and distinguishes device-reported actual tool state from
  remote canonical projection.
- Mac and OpenClaw are connected peers.
- OpenClaw remains pull-only for unattended sync. Local OpenClaw changes require
  explicit approval before writing to WebDAV.
- Windows is deferred and should not block this phase.

### Dashboard/Product Direction

The intended product direction is now clear:

- First screen should be simple and operator-oriented.
- Detailed diagnostics should live in secondary areas.
- UI should distinguish:
  - Current device local skill workspace.
  - Central shared library state.
  - Other device reported state.
  - Tool installation state per skill.
- Operations should apply to the local client/device first, or explicitly
  publish to central storage.

### Recent Code Changes

Added:

- `scripts/openclaw-approved-push-quick.sh`

Updated:

- `docs/approved-push-runbook.md`

The quick helper provides:

```bash
# Read current NAS queue and run dry-run only.
scripts/openclaw-approved-push-quick.sh

# Publish all current actionable OpenClaw writer_policy candidates.
scripts/openclaw-approved-push-quick.sh --yes

# Limit blast radius.
scripts/openclaw-approved-push-quick.sh --max 3
```

Known behavior:

- It only handles actionable OpenClaw `writer_policy` items.
- It does not handle conflict/delete/version-difference review items.
- It defaults to refreshing peer status after `--yes`.
- It correctly returns 0 when no actionable writer-policy candidate exists.

## Deployment Actions Already Done

The latest `d65bf09` was pushed to GitHub and deployed to NAS by archive upload:

1. Built local archive from `d65bf09`.
2. Uploaded to NAS using legacy scp mode because NAS SFTP scp failed:

```bash
scp -O /tmp/skill-sync-sidecar-d65bf09.tar.gz \
  commiao@100.123.208.32:/tmp/skill-sync-sidecar-d65bf09.tar.gz
```

3. Unpacked into `/volume1/docker/skill-sync-gateway`.
4. Wrote `deployed-commit.txt=d65bf09`.
5. Rebuilt/restarted:

```bash
sudo -n /var/packages/ContainerManager/target/usr/bin/docker compose \
  --env-file .env \
  -f examples/docker-compose.gateway.yml \
  up -d --build
```

6. Validated NAS deployment successfully.

Do not assume `git pull` works on NAS: `git` is not installed there.

## What Is Not Finished

### Immediate

1. Resolve the current dashboard yellow state:
   - blocked count: `1`
   - skill: `finance-auto-bookkeeping`
   - type: version difference, not quick-approved writer-policy candidate

2. Capture a concrete report for the `finance-auto-bookkeeping` difference:
   - what changed locally/centrally
   - which side should win
   - whether it is safe to save OpenClaw version or restore shared version

3. After resolving it, verify:

```bash
SKILL_SYNC_EXPECTED_COMMIT=d65bf09 scripts/validate-nas-sidecar.sh
scripts/openclaw-approved-push-quick.sh
```

Expected healthy end state:

```text
dashboard_health=green
dashboard_blocked=0
openclaw_pending_count=0
```

### Product/UI

The UI is still not the final ordinary-user experience. Remaining product work:

- Add a first-class local skill workspace area.
- Show a skill-centric list:
  - skill name
  - installed tools on this device
  - central published/unpublished state
  - public/shared vs project-only vs private/local policy
  - deprecated/disabled state without hard delete
- Let the user install/uninstall a skill per local tool through checkboxes.
- Let the user explicitly publish a local skill to the central WebDAV library.
- Keep advanced diagnostics out of the first screen unless opened.

### Real-World Verification Still Needed

- The quick helper has been verified for current no-writer-policy queue.
- It still needs one future live test where `approved > 0`:
  - create/observe a normal OpenClaw writer-policy candidate
  - run quick dry-run
  - run quick `--yes`
  - confirm NAS returns green/blocked 0

## Recommended Next Session Steps

Start with these commands:

```bash
cd /Users/mac/workspace_codex/skill-sync-sidecar
git status -sb
git log --oneline -n 3
SKILL_SYNC_EXPECTED_COMMIT=d65bf09 scripts/validate-nas-sidecar.sh
scripts/blocked-queue.sh
```

Then focus on the one remaining yellow item:

```bash
scripts/operator-status.sh
scripts/ops-watch.sh
```

If `finance-auto-bookkeeping` still appears as a version difference:

1. Generate/read the read-only diff package/report for that item.
2. Decide direction:
   - restore shared-library version to OpenClaw/Mac
   - save OpenClaw version to shared library
   - manually merge and then publish
3. Do not use `openclaw-approved-push-quick.sh --yes` unless the item becomes a
   normal `writer_policy` candidate.

After resolution:

```bash
SKILL_SYNC_EXPECTED_COMMIT=d65bf09 scripts/validate-nas-sidecar.sh
scripts/openclaw-approved-push-quick.sh
```

## Safety Boundaries

- Do not touch OpenClaw service/gateway processes for NAS dashboard work.
- Do not replace or remove system Python on OpenClaw.
- Do not switch OpenClaw unattended sync to push-pull.
- Do not delete central WebDAV skills to clear yellow status.
- Do not treat `openclaw_pending_count=0` as full health if dashboard still has
  blocked items; it only means there are no quick writer-policy candidates.
- `disk-cleanup` is OpenClaw-private and should not be made public/shared.
- Keep NAS `.env` private and in place; it contains WebDAV configuration.

## Useful Commands

Validate NAS:

```bash
SKILL_SYNC_EXPECTED_COMMIT=d65bf09 scripts/validate-nas-sidecar.sh
```

Quick OpenClaw writer-policy path:

```bash
scripts/openclaw-approved-push-quick.sh
scripts/openclaw-approved-push-quick.sh --yes
```

Queue/status:

```bash
scripts/blocked-queue.sh
scripts/operator-status.sh
scripts/ops-watch.sh
```

NAS container status:

```bash
ssh commiao@100.123.208.32 \
  'cd /volume1/docker/skill-sync-gateway && cat deployed-commit.txt && sudo -n /var/packages/ContainerManager/target/usr/bin/docker compose --env-file .env -f examples/docker-compose.gateway.yml ps'
```

NAS logs:

```bash
ssh commiao@100.123.208.32 \
  'sudo -n /var/packages/ContainerManager/target/usr/bin/docker logs --tail 80 skill-sync-gateway'
```

## Suggested Next Milestone

Milestone: "Resolve last yellow item and make local skill workspace actionable."

Definition of done:

- Dashboard returns green with `blocked=0`.
- `finance-auto-bookkeeping` version difference is resolved with an audit trail.
- Quick helper still returns `openclaw_pending_count=0`.
- Dashboard first screen has a clearer local skill workspace entry point.
- There is a visible product path toward per-skill tool install/uninstall and
  publish/deprecate actions.
