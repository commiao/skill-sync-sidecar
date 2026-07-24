# OpenClaw Autosync Canary - 2026-06-19

## Goal

Validate that the enabled OpenClaw `pull-only` automatic sync service can apply a real WebDAV-to-OpenClaw skill change, not just run no-op cycles.

## Canary

- Skill id: `sync-probe-autosync`
- Mac source path: `/Users/mac/.cc-switch/skills/sync-probe-autosync/SKILL.md`
- Description: `Harmless canary skill for validating skill-sync-sidecar pull-only automatic sync from WebDAV to OpenClaw.`
- Contents: one `SKILL.md`, no scripts, no credentials, no side effects.

## Mac Save To Shared Library

The Mac snapshot was rebuilt from `/Users/mac/.cc-switch/skills` and pushed to:

```text
prefix=skill-sync-sidecar-dev/current-mac
snapshot_id=autosync-canary
snapshot_total=93
has_canary=True
uploaded_files=2
uploaded_bytes=102244
```

Only the new canary archive and `index.json` were uploaded; the existing 92 archives were not re-uploaded.

## OpenClaw Automatic Pull

The OpenClaw pull-only service was restarted to trigger an immediate cycle:

```text
unit=openclaw-skill-sync-sidecar-pullonly.service
writer_policy=pull-only
mode=--yes
summary={'noop': 92, 'pull_new': 1}
blocked=0
conflicts=0
applied=1
uploaded=0
```

The canary appeared at:

```text
/home/admin/clawd/skills/sync-probe-autosync/SKILL.md
```

## OpenClaw Discovery

OpenClaw-side scanner result:

```text
scan_total=93
canary_count=1
canary_name=sync-probe-autosync
canary_risk=ok
canary_file_count=1
```

## Base Records

After the canary apply, both peers were normalized to a full 93-skill base:

Mac:

```text
base_record=/Users/mac/Library/Application Support/skill-sync-sidecar/base-record.json
status_summary={'unchanged': 93}
overall_ok=True
```

OpenClaw:

```text
base_record=/opt/skill-sync-sidecar/state/openclaw-base-record.json
status_summary={'unchanged': 93}
base_applied=93
base_has_canary=True
has_conflicts=False
```

## Service Safety

```text
openclaw-skill-sync-sidecar-pullonly.service=enabled active
pullonly_NRestarts=0
openclaw-skill-sync-sidecar-dryrun.service=active
dryrun_NRestarts=0
openclaw-gateway pid=2966537
openclaw-gateway uptime=7-04:25:18
```

OpenClaw gateway was not restarted. OpenClaw did not upload anything back to WebDAV.

## Read-Only Reconcile

After the canary and base adoption, a fresh read-only reconcile was generated:

```text
report=/private/tmp/openclaw-skill-sync-validate/reconcile-after-autosync-canary-20260619/reconcile/reconcile-report.json
local=93
remote=93
safe_to_auto_apply=True
summary={'same_without_base': 93}
openclaw_gate=ok
```

## Rollback

To stop automatic pull-only sync:

```bash
systemctl disable --now openclaw-skill-sync-sidecar-pullonly.service
```

The canary is now part of the shared 93-skill baseline. Removing it should be handled as an explicit delete/tombstone workflow rather than ad hoc file deletion.
