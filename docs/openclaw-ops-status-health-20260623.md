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

Peer-local private skill code:

```text
commit=2e4499f
title=Refresh blocked report when sync plan clears
```

Release `2e4499f` was unpacked to:

```text
/opt/skill-sync-sidecar/releases/2e4499f
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

OpenClaw status command after local override, local-only, and packaging-signal support:

```bash
PYTHONPATH=/opt/skill-sync-sidecar/releases/2e4499f/src \
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
health: green
local_root: /home/admin/clawd/skills
remote_snapshot: tianjin-sidecar-doc-20260622 total=94
base_record: approved-push-base-20260622T115617.827181Z applied=94
daemon_state: running cycles_run=1
last_cycle: complete snapshot=tianjin-sidecar-doc-20260622 blocked=0 summary={'noop': 95}
blocked_report: total=0 writer_policy=pull-only
sync_plan: safe_to_apply=True blocked=0 allowed=95
status_summary: {'already_converged': 1, 'local_only': 1, 'local_override': 1, 'unchanged': 92}
local_overrides: {'total': 2, 'skills': ['disk-cleanup', 'lark-cli-adapter']}
overall_ok: True
```

Interpretation: OpenClaw is healthy. Its server-private operational skill is kept local, the runtime shebang patch is kept local, and nothing was uploaded or applied automatically.

The sidecar services were updated to release `2e4499f`:

```text
openclaw-skill-sync-sidecar-pullonly.service: active
openclaw-skill-sync-sidecar-dryrun.service: active
PYTHONPATH=/opt/skill-sync-sidecar/releases/2e4499f/src
```

OpenClaw gateway stayed online:

```text
2966537 Ssl openclaw-gateway
```

## Local-Only Decision

Do not publish `disk-cleanup`:

- `disk-cleanup`: OpenClaw internal private operational skill; useful on the server but not portable or intended for Mac/Windows peers.
- Scanner validation now reports `missing_referenced_package_file`: `SKILL.md references scripts/disk-cleanup.sh, but that file is not included in the skill package.`

`disk-cleanup` and `lark-cli-adapter` are acknowledged by `/home/admin/clawd/skills/.skill-sync-local-overrides.json`:

```json
{
  "version": 0,
  "skills": {
    "lark-cli-adapter": {
      "ignore_paths": ["bin/lark_send_text.py"],
      "reason": "OpenClaw runtime launcher uses Linuxbrew Python"
    },
    "disk-cleanup": {
      "local_only": true,
      "reason": "OpenClaw internal private operational skill"
    }
  }
}
```

## Next Work

1. Done: add and deploy a local override mechanism for peer-specific runtime patches such as shebangs.
2. Done: add a scanner/doctor warning for local absolute path references that make packages non-portable.
3. Done: add and deploy local-only semantics for peer-private skills.
4. Wire `ops-status` into a scheduled or UI-facing status surface so the current state is visible as green/yellow/red.
