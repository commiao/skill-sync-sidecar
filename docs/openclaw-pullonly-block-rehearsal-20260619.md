# OpenClaw Pull-Only Block Rehearsal - 2026-06-19

## Goal

Validate that OpenClaw can run as a peer with automatic pull-only sync enabled, while local OpenClaw edits are blocked from being uploaded to WebDAV by the unattended service.

## Preflight

OpenClaw and WebDAV were aligned before the rehearsal:

```text
openclaw_status_summary={'unchanged': 93}
pre_cycles_run=2
pre_last_summary={'noop': 93}
pre_blocked=0
pre_applied=0
pre_uploaded=0
writer_policy=pull-only
```

The canary skill was backed up before the local edit:

```text
skill_id=sync-probe-autosync
source=/home/admin/clawd/skills/sync-probe-autosync/SKILL.md
backup=/opt/skill-sync-sidecar/state/sync-probe-autosync-before-block-test.SKILL.md
```

## Local Edit

Only the OpenClaw local canary file was edited. No Mac file or WebDAV snapshot was changed.

```text
local_root=/home/admin/clawd/skills
edited_file=/home/admin/clawd/skills/sync-probe-autosync/SKILL.md
base_hash=9ee9fde0c6d97362e23043991665588f3dc190712a914c5078f49b0391949b64
local_hash=d2afe19de07dbf622d9413d9bcdd60e14c2cc08b010f9d267839d99a65c20db0
remote_hash=9ee9fde0c6d97362e23043991665588f3dc190712a914c5078f49b0391949b64
```

The pull-only service was restarted only to trigger an immediate sidecar cycle. The OpenClaw gateway was not restarted.

## Block Result

The sidecar refused to upload the OpenClaw local edit:

```text
unit=openclaw-skill-sync-sidecar-pullonly.service
final_status=complete
final_last_status=blocked
final_last_reason=sync plan has blocked items
final_summary={'blocked': 1, 'noop': 92}
blocked=1
conflicts=0
applied=0
uploaded=0
```

The generated review report is the approval queue for this blocked local edit:

```text
json=/opt/skill-sync-sidecar/work/current-mac-pullonly/blocked-report/blocked-report.json
markdown=/opt/skill-sync-sidecar/work/current-mac-pullonly/blocked-report/blocked-report.md
total=1
summary={'writer_policy': 1}
skill_id=sync-probe-autosync
status_action=push
plan_action=blocked
reason=writer policy pull-only blocks push
recommendation=Review the local change. If it should publish upstream, run an explicit approved push path instead of changing the unattended OpenClaw policy.
```

This proves the unattended OpenClaw service does not silently publish OpenClaw-local edits back to WebDAV while running with `--writer-policy pull-only`.

## Restore

The canary file was restored from the preflight backup, and only the pull-only sidecar service was restarted.

```text
restore_source=/opt/skill-sync-sidecar/state/sync-probe-autosync-before-block-test.SKILL.md
restore_target=/home/admin/clawd/skills/sync-probe-autosync/SKILL.md
service=openclaw-skill-sync-sidecar-pullonly.service
active=true
NRestarts=0
```

Post-restore OpenClaw status:

```text
status_summary={'unchanged': 93}
has_conflicts=false
daemon_cycle_summary={'noop': 93}
daemon_blocked=0
daemon_applied=0
daemon_uploaded=0
```

Mac/WebDAV status remained aligned:

```text
remote_snapshot=autosync-canary
remote_total=93
mac_status_summary={'unchanged': 93}
mac_last_cycle_summary={'noop': 93}
mac_blocked=0
mac_overall_ok=True
```

## Service Safety

```text
openclaw-skill-sync-sidecar-pullonly.service=active running
openclaw-skill-sync-sidecar-dryrun.service=active running
openclaw-gateway pid=2966537
openclaw-gateway uptime=7-04:34:56
```

OpenClaw gateway connectivity was preserved. The only restarted unit during the rehearsal was the sidecar pull-only service.

## Operating Rule Confirmed

OpenClaw can receive WebDAV updates automatically, but OpenClaw-local edits must not be published by the unattended service. When a local edit appears, `pull-only` blocks it and writes a `blocked-report`; promotion of that local edit requires a separate reviewed push path.
