# OpenClaw Ops Status Health Validation - 2026-06-23

## Goal

Make the sidecar observable enough that a user can distinguish healthy sync from review-required sync without reading raw daemon state files.

## Release

Validated code:

```text
commit=bb51726
title=Add ops status health and blocked queue
```

The release was unpacked to:

```text
/opt/skill-sync-sidecar/releases/bb51726
```

The OpenClaw gateway was not restarted.

## Mac Result

The Mac sidecar status is green:

```text
health=green
snapshot=tianjin-sidecar-doc-20260622
total=94
last_cycle={'noop': 94}
blocked_report=null
```

Interpretation: Mac, WebDAV cache, and last applied state are converged.

## OpenClaw Result

OpenClaw status command:

```bash
PYTHONPATH=/opt/skill-sync-sidecar/releases/bb51726/src \
  /opt/skill-sync-sidecar/venv-0.1.3/bin/python -m skill_sync_sidecar ops-status \
    --local-root /home/admin/clawd/skills \
    --remote-snapshot /opt/skill-sync-sidecar/cache/current-mac-pullonly \
    --base-record /opt/skill-sync-sidecar/state/openclaw-base-record.json \
    --state-file /opt/skill-sync-sidecar/state/openclaw-daemon-pullonly-state.json \
    --blocked-report /opt/skill-sync-sidecar/work/current-mac-pullonly/blocked-report/blocked-report.json \
    --allow-new \
    --writer-policy pull-only
```

Result:

```text
health: yellow
local_root: /home/admin/clawd/skills
remote_snapshot: tianjin-sidecar-doc-20260622 total=94
base_record: approved-push-base-20260622T115617.827181Z applied=94
daemon_state: running cycles_run=1358
last_cycle: blocked snapshot=tianjin-sidecar-doc-20260622 blocked=2 summary={'blocked': 2, 'noop': 93}
blocked_report: total=2 writer_policy=pull-only
blocked_item: disk-cleanup category=writer_policy status=local_new plan=blocked reason=writer policy pull-only blocks push_new
blocked_item: lark-cli-adapter category=writer_policy status=push plan=blocked reason=writer policy pull-only blocks push
sync_plan: safe_to_apply=False blocked=2 allowed=93
status_summary: {'already_converged': 1, 'local_new': 1, 'push': 1, 'unchanged': 92}
overall_ok: False
```

Interpretation: OpenClaw is healthy but has review-required local changes. Nothing was uploaded or applied automatically.

## Blocked Queue Decision

Do not publish these two items yet:

- `disk-cleanup`: local new skill, but it references `/home/admin/clawd/scripts/disk-cleanup.sh`; package is not portable until the script is bundled or declared as an external dependency.
- `lark-cli-adapter`: local shebang points at OpenClaw Linuxbrew Python for runtime compatibility; publishing that absolute path would pollute other peers.

## Next Work

1. Add a local override mechanism for peer-specific runtime patches such as shebangs.
2. Add a packaging rule for skills that depend on external scripts.
3. Wire `ops-status` into a scheduled or UI-facing status surface so the current state is visible as green/yellow/red.
