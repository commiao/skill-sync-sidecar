# OpenClaw Writer Policy Rehearsal - 2026-06-19

## Goal

Validate OpenClaw as a peer device that may receive WebDAV skill updates without automatically saving OpenClaw-local edits back upstream.

This closes the gap after base adoption: OpenClaw has a durable base record, and the sidecar now has an explicit `pull-only` writer policy for safe downstream sync.

## Inputs

- Source commit: `149dcd6`
- Source release path: `/opt/skill-sync-sidecar/releases/149dcd6`
- Python runtime: `/opt/skill-sync-sidecar/venv-0.1.3/bin/python`
- Local skill root: `/home/admin/clawd/skills`
- Remote cache: `/opt/skill-sync-sidecar/cache/openclaw-writable-rehearsal`
- WebDAV prefix: `skill-sync-sidecar-dev/current-mac`
- Base record: `/opt/skill-sync-sidecar/state/openclaw-base-record.json`
- Plan report: `/opt/skill-sync-sidecar/state/openclaw-writer-policy-plan-149dcd6.json`
- Rehearsal state: `/opt/skill-sync-sidecar/state/openclaw-writer-policy-rehearsal-149dcd6.json`

## Policy

OpenClaw should run writable sync with:

```text
--writer-policy pull-only
```

This permits WebDAV-to-OpenClaw pulls and no-op cycles, but blocks OpenClaw-to-WebDAV pushes. Local OpenClaw edits are surfaced as blocked plan items for review instead of being uploaded automatically.

## Pull-Only Plan Check

```text
safe_to_apply=True
writer_policy=pull-only
summary={'noop': 92}
```

The existing base record made all 92 packages resolve as no-op. The policy did not introduce false blocks in the aligned state.

## One-Cycle Rehearsal

```text
current_base_record=/opt/skill-sync-sidecar/state/openclaw-base-record.json
writer_policy=pull-only
cycles_run=1
summary={'noop': 92}
blocked=0
conflicts=0
tombstones=0
applied=0
uploaded=0
```

The rehearsal ran the strict OpenClaw gate first, then a finite `sync-daemon --yes --max-cycles 1 --interval-seconds 0 --writer-policy pull-only` cycle. No skill files were changed and nothing was uploaded.

## Service Safety Check

```text
openclaw-skill-sync-sidecar-dryrun.service=active
openclaw-skill-sync-sidecar-dryrun MainPID=4056815
openclaw-skill-sync-sidecar-dryrun NRestarts=0
openclaw-gateway pid=2966537
openclaw-gateway uptime=7-03:38:14
```

The existing dry-run sidecar stayed active. OpenClaw gateway was not stopped or restarted. No long-running writable service was installed.

## Next Gate

Do not promote OpenClaw to an unattended writable service until the operating policy is explicit:

- keep OpenClaw on `pull-only` by default,
- define who may approve OpenClaw-to-WebDAV publishing,
- route blocked local edits to a review report instead of auto-uploading them,
- require manual approval for deletes or tombstones,
- and keep gateway connectivity checks independent from sidecar rollout.
