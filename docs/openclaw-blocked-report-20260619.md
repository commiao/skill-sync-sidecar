# OpenClaw Blocked Report Deployment - 2026-06-19

## Goal

Deploy and verify the blocked sync review report capability on OpenClaw without changing long-running services.

This gives `pull-only` OpenClaw a review queue for local changes that are intentionally blocked from automatic WebDAV upload.

## Inputs

- Source commit: `0e67181`
- Source release path: `/opt/skill-sync-sidecar/releases/0e67181`
- Python runtime: `/opt/skill-sync-sidecar/venv-0.1.3/bin/python`
- Local skill root: `/home/admin/clawd/skills`
- Remote cache: `/opt/skill-sync-sidecar/cache/openclaw-writable-rehearsal`
- Base record: `/opt/skill-sync-sidecar/state/openclaw-base-record.json`
- Rehearsal state: `/opt/skill-sync-sidecar/state/openclaw-blocked-report-rehearsal-0e67181.json`

## Command Verification

```text
skill-sync blocked-report --help
```

OpenClaw successfully loaded the new release and exposed:

```text
--writer-policy {push-pull,pull-only,push-only,no-writes}
--out OUT
--fail-on-empty
--json
```

## One-Cycle Rehearsal

```text
current_base_record=/opt/skill-sync-sidecar/state/openclaw-base-record.json
writer_policy=pull-only
cycles_run=1
summary={'noop': 92}
blocked=0
conflicts=0
applied=0
uploaded=0
```

The 92/92 aligned path remains a no-op. Because there were no blocked items, no blocked review report was needed in this rehearsal.

## Service Safety Check

```text
openclaw-skill-sync-sidecar-dryrun.service=active
openclaw-skill-sync-sidecar-dryrun MainPID=4056815
openclaw-skill-sync-sidecar-dryrun NRestarts=0
openclaw-gateway pid=2966537
openclaw-gateway uptime=7-03:45:35
```

No systemd unit was modified. No long-running writable service was installed. OpenClaw gateway was not stopped or restarted.

## Operator Path

When a future `pull-only` cycle blocks because OpenClaw has a local change, run:

```bash
sudo -iu admin \
  PYTHONPATH=/opt/skill-sync-sidecar/releases/0e67181/src \
  /opt/skill-sync-sidecar/venv-0.1.3/bin/python -m skill_sync_sidecar blocked-report \
    --local-root /home/admin/clawd/skills \
    --remote-snapshot /opt/skill-sync-sidecar/cache/openclaw-writable-rehearsal \
    --last-applied-record /opt/skill-sync-sidecar/state/openclaw-base-record.json \
    --writer-policy pull-only \
    --out /opt/skill-sync-sidecar/work/openclaw-blocked-review
```

Treat the generated `blocked-report.md` as the approval queue before any OpenClaw-to-WebDAV publish.
