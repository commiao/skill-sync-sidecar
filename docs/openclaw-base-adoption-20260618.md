# OpenClaw Base Adoption - 2026-06-18

## Goal

Convert OpenClaw from a fully aligned but base-less state (`same_without_base=92`) into a durable peer-writer baseline (`unchanged=92`) without installing, deleting, uploading, or replacing any skill content.

## Inputs

- Source commit: `b014ec0`
- Source release path: `/opt/skill-sync-sidecar/releases/b014ec0`
- Python runtime: `/opt/skill-sync-sidecar/venv-0.1.3/bin/python`
- Local skill root: `/home/admin/clawd/skills`
- Remote cache: `/opt/skill-sync-sidecar/cache/openclaw-writable-rehearsal`
- WebDAV prefix: `skill-sync-sidecar-dev/current-mac`
- Base record: `/opt/skill-sync-sidecar/state/openclaw-base-record.json`

## Dry Run

```text
safe_to_adopt=True
total=92
summary={'same_without_base': 92}
blocked=0
```

## Adoption Result

```text
status=complete
record_path=/opt/skill-sync-sidecar/state/openclaw-base-record.json
applied_count=92
record_type=skill-sync-base
snapshot_id=20260615T113322.799109Z
adoption_summary={'same_without_base': 92}
```

The adoption wrote only the stable base record. It did not change the OpenClaw skill tree or WebDAV snapshot.

## Post-Adoption Sync Status

```text
total=92
summary={'unchanged': 92}
has_conflicts=False
```

## Post-Adoption Writable Rehearsal

```text
current_base_record=/opt/skill-sync-sidecar/state/openclaw-base-record.json
cycles_run=1
summary={'noop': 92}
blocked=0
conflicts=0
tombstones=0
applied=0
uploaded=0
```

## Service Safety Check

```text
openclaw-skill-sync-sidecar-dryrun.service=active
openclaw-skill-sync-sidecar-dryrun NRestarts=0
openclaw-gateway process uptime=7-03:16:21
post_adopt_scan_total=92
post_adopt_by_risk={'ok': 80, 'warning': 12, 'error': 0}
```

The dry-run sidecar stayed active. OpenClaw gateway was not restarted. The OpenClaw skill tree remained at 92 packages.

## Next Gate

Long-running writable service rollout is still not automatic. The next decision is policy, not mechanics:

- whether OpenClaw may push local edits to WebDAV automatically,
- whether Mac/Win/OpenClaw all run writable daemons or only selected writers do,
- how conflict packages are routed for review,
- and how tombstones/deletes are approved.
