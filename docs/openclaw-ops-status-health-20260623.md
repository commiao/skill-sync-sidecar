# OpenClaw Ops Status Health Validation - 2026-06-23

## Goal

Make the sidecar observable enough that a user can distinguish healthy sync from review-required sync without reading raw daemon state files.

## Release

Initial status health code:

```text
commit=bb51726
title=Add ops status health and blocked queue
```

Local runtime override code:

```text
commit=707e006
title=Add local runtime overrides for sync status
```

Packaging signal code:

```text
commit=976c069
title=Flag missing referenced package files
```

Release `976c069` was unpacked to:

```text
/opt/skill-sync-sidecar/releases/976c069
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

OpenClaw status command after local override and packaging-signal support:

```bash
PYTHONPATH=/opt/skill-sync-sidecar/releases/976c069/src \
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
daemon_state: running cycles_run=1
last_cycle: blocked snapshot=tianjin-sidecar-doc-20260622 blocked=1 summary={'blocked': 1, 'noop': 94}
blocked_report: total=1 writer_policy=pull-only
blocked_item: disk-cleanup category=writer_policy status=local_new plan=blocked reason=writer policy pull-only blocks push_new
sync_plan: safe_to_apply=False blocked=1 allowed=94
status_summary: {'already_converged': 1, 'local_new': 1, 'local_override': 1, 'unchanged': 92}
local_overrides: {'total': 1, 'skills': ['lark-cli-adapter']}
overall_ok: False
```

Interpretation: OpenClaw is healthy but has one review-required local new skill. Nothing was uploaded or applied automatically.

The sidecar services were updated to release `976c069`:

```text
openclaw-skill-sync-sidecar-pullonly.service: active
openclaw-skill-sync-sidecar-dryrun.service: active
PYTHONPATH=/opt/skill-sync-sidecar/releases/976c069/src
```

OpenClaw gateway stayed online:

```text
2966537 Ssl openclaw-gateway
```

## Blocked Queue Decision

Do not publish `disk-cleanup` yet:

- `disk-cleanup`: local new skill, but it references `/home/admin/clawd/scripts/disk-cleanup.sh`; package is not portable until the script is bundled or declared as an external dependency.
- Scanner validation now reports `missing_referenced_package_file`: `SKILL.md references scripts/disk-cleanup.sh, but that file is not included in the skill package.`

`lark-cli-adapter` is no longer a blocked publish candidate. It is acknowledged by `/home/admin/clawd/skills/.skill-sync-local-overrides.json`:

```json
{
  "version": 0,
  "skills": {
    "lark-cli-adapter": {
      "ignore_paths": ["bin/lark_send_text.py"],
      "reason": "OpenClaw runtime launcher uses Linuxbrew Python"
    }
  }
}
```

## Next Work

1. Done: add and deploy a local override mechanism for peer-specific runtime patches such as shebangs.
2. Done: add a scanner/doctor warning for local absolute path references that make packages non-portable.
3. Wire `ops-status` into a scheduled or UI-facing status surface so the current state is visible as green/yellow/red.
