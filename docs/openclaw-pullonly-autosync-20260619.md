# OpenClaw Pull-Only Auto Sync - 2026-06-19

## Goal

Promote OpenClaw from observation-only dry-run sync to automatic `pull-only` sync, while keeping OpenClaw from automatically saving local edits back to WebDAV.

## Service

- Unit: `openclaw-skill-sync-sidecar-pullonly.service`
- Status: `enabled`, `active`
- Main PID: `139334`
- Restart count: `0`
- Runtime: `/opt/skill-sync-sidecar/venv-0.1.3/bin/python`
- Source release: `/opt/skill-sync-sidecar/releases/54cf057`
- Local root: `/home/admin/clawd/skills`
- WebDAV prefix: `skill-sync-sidecar-dev/current-mac`
- Cache: `/opt/skill-sync-sidecar/cache/current-mac-pullonly`
- Work dir: `/opt/skill-sync-sidecar/work/current-mac-pullonly`
- State file: `/opt/skill-sync-sidecar/state/openclaw-daemon-pullonly-state.json`
- Base record: `/opt/skill-sync-sidecar/state/openclaw-base-record.json`
- Writer policy: `pull-only`
- Mode: `--yes`
- Interval: `300` seconds

The service command includes:

```text
--allow-new
--writer-policy pull-only
--yes
--base-record-file /opt/skill-sync-sidecar/state/openclaw-base-record.json
--last-applied-record /opt/skill-sync-sidecar/state/openclaw-base-record.json
```

## First Cycle Result

```text
daemon_status=running
writer_policy=pull-only
cycles_run=1
last_summary={'noop': 92}
blocked=0
applied=0
uploaded=0
status_summary={'unchanged': 92}
has_conflicts=False
```

This means OpenClaw is now automatically polling WebDAV and is ready to apply allowed WebDAV-to-local changes. The first cycle found OpenClaw already aligned with the current 92-skill snapshot.

## Safety Boundaries

- OpenClaw-to-WebDAV saving is blocked by `--writer-policy pull-only`.
- OpenClaw local edits will become blocked review items instead of automatic uploads.
- Deletes are still not automatically executed.
- The existing dry-run observer service remains active as a parallel monitor:
  - Unit: `openclaw-skill-sync-sidecar-dryrun.service`
  - Status: `active`
  - Restart count: `0`
- OpenClaw gateway was not restarted:
  - Process: `openclaw-gateway`
  - PID: `2966537`
  - Uptime at validation: `7-04:07:26`

## Rollback

To stop automatic pull-only sync without touching OpenClaw gateway:

```bash
systemctl disable --now openclaw-skill-sync-sidecar-pullonly.service
```

The dry-run observer can remain active for visibility.
