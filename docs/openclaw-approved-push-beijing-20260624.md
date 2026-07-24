# OpenClaw Approved Push - Beijing Recruitment - 2026-06-24

## Goal

Save one reviewed OpenClaw-local skill change, `beijing-recruitment`, to the shared WebDAV snapshot without changing OpenClaw's unattended `pull-only` policy.

## Starting State

OpenClaw dashboard peer status reported:

```text
OpenClaw health=yellow
sync_plan_summary={"blocked": 1, "noop": 94}
blocked_skill=beijing-recruitment
reason=writer policy pull-only blocks push
```

Expected local-only and peer-local exceptions were not published:

```text
disk-cleanup=local_only
lark-cli-adapter=local_override
```

## Approved Push

The blocked report was regenerated immediately before approval:

```text
blocked_report=/opt/skill-sync-sidecar/work/current-mac-pullonly/blocked-report/blocked-report.json
total=1
skill_id=beijing-recruitment
status_action=push
base_hash=e9cbddfd09aff09db3124604157452ebb063f505774d4b5455f835942a68e2d4
local_hash=9c08d1ab19872db7654eed5819b7810e7b25528928f37b7ed576a1edbc2e0156
remote_hash=e9cbddfd09aff09db3124604157452ebb063f505774d4b5455f835942a68e2d4
```

Dry-run:

```text
safe_to_push=true
approved_skill_ids=beijing-recruitment
current_plan_summary={"noop": 94, "push": 1}
upload_preview_total=94
upload_preview_files=2
archive=skills/cc-switch/beijing-recruitment/9c08d1ab19872db7654eed5819b7810e7b25528928f37b7ed576a1edbc2e0156.zip
```

Execution:

```text
command=skill-sync approved-push --yes
run_user=admin
release=/opt/skill-sync-sidecar/releases/2e4499f
uploaded_files=2
uploaded_bytes=132607
remote_prefix=skill-sync-sidecar-dev/current-mac
base_record_path=/opt/skill-sync-sidecar/state/openclaw-base-record.json
approved_record=/opt/skill-sync-sidecar/work/current-mac-pullonly/approved-push-beijing-20260624/approved-push-record.json
snapshot_id=approved-push-20260624T081058.172945Z
```

The first `--yes` attempt as `root` was refused because root had no cc-switch WebDAV base URL configured. The command was rerun as `admin`, which owns the configured `/home/admin/.cc-switch/settings.json`.

## OpenClaw Result

After refreshing the OpenClaw WebDAV cache:

```text
cache_snapshot_id=approved-push-20260624T081058.172945Z
blocked_report_total=0
sync_status_summary={"local_only": 1, "local_override": 1, "unchanged": 93}
```

Service and gateway checks:

```text
openclaw-gateway pid=2966537
openclaw-skill-sync-sidecar-pullonly.service=active
openclaw-skill-sync-sidecar-dryrun.service=active
gateway_restarted=false
```

## Mac Result

The Mac WebDAV sync folder received:

```text
snapshot_id=approved-push-20260624T081058.172945Z
remote_total=94
beijing-recruitment_hash=9c08d1ab19872db7654eed5819b7810e7b25528928f37b7ed576a1edbc2e0156
```

Mac then applied the one allowed pull into `/Users/mac/.cc-switch/skills` with `target=mixed-scope-root`:

```text
sync_apply_summary={"noop": 93, "pull": 1}
applied=1
uploaded=0
applied_skill=beijing-recruitment
backup=/Users/mac/.cc-switch/skills/.skill-sync-backups/20260624T081341.005331Z/beijing-recruitment
```

Mac base adoption:

```text
safe_to_adopt=true
adoptable=94
blocked=0
base_record=/Users/mac/Library/Application Support/skill-sync-sidecar/base-record.json
base_snapshot_id=approved-push-20260624T081058.172945Z
```

Final status:

```text
mac_sync_status={"unchanged": 94}
dashboard_health=green
dashboard_blocked=0
mac_device=green skills=94 blocked=0
openclaw_device=green skills=94 blocked=0
```

## Decision

Keep OpenClaw unattended sync on `pull-only`. Use `approved-push` for reviewed OpenClaw-local changes that should be saved to the shared library. Use `mixed-scope-root` when applying the shared snapshot into governed roots that intentionally contain both global and project-scoped skills.
