# Mac Autopull and Recruitment Approved Push - 2026-06-24

## Goal

Close the Mac-side automation gap after OpenClaw approved-push events: Mac should not require manual `sync-apply` and base adoption after a reviewed OpenClaw-to-WebDAV publish.

## Mac Daemon Fix

The Mac LaunchAgent already used the governed mixed root:

```text
plist=/Users/mac/Library/LaunchAgents/com.skill-sync-sidecar.plist
target=mixed-scope-root
local_root=/Users/mac/.cc-switch/skills
remote=file:///Users/mac/public-sync
prefix=skill-sync-sidecar-dev/current-mac
base_record=/Users/mac/Library/Application Support/skill-sync-sidecar/base-record.json
```

The stale dashboard yellow state came from an old stopped daemon state:

```text
last_cycle={"blocked": 1, "noop": 93}
daemon_status=complete
```

The LaunchAgent was updated to include:

```text
--writer-policy push-pull
--continue-on-blocked
```

After restart:

```text
daemon_status=running
target=mixed-scope-root
writer_policy=push-pull
stop_on_blocked=false
last_cycle={"noop": 94}
```

This lets the daemon keep polling after a blocked cycle. Once an approved-push or local resolution clears the blocked condition, a later daemon cycle can return the Mac status to green without a manual `sync-apply`.

## New OpenClaw Blocked Items

While validating the Mac daemon, OpenClaw had two new reviewed local recruitment skill changes:

```text
beijing-recruitment   push
tianjin-recruitment   push
reason=writer policy pull-only blocks push
```

Blocked report:

```text
blocked_report=/opt/skill-sync-sidecar/work/current-mac-pullonly/blocked-report/blocked-report.json
total=2
summary={"writer_policy": 2}
```

Hashes:

```text
beijing-recruitment base=9c08d1ab19872db7654eed5819b7810e7b25528928f37b7ed576a1edbc2e0156
beijing-recruitment local=6ebeddcef85c3ab335a3e598da3a3d157a56c95190116114dd62175b543dafd9
tianjin-recruitment base=d45da5b55b24711de32331050d1b8e609460875870603d8dcaa51579af80bd2f
tianjin-recruitment local=1dfdb2e7364449de36faba752810c8b545133400e55ca9b29a4356d985550f49
```

## Approved Push

Dry-run:

```text
safe_to_push=true
approved_skill_ids=beijing-recruitment,tianjin-recruitment
current_plan_summary={"noop": 93, "push": 2}
upload_preview_total=94
upload_preview_files=3
```

Execution:

```text
command=skill-sync approved-push --yes
run_user=admin
release=/opt/skill-sync-sidecar/releases/2e4499f
uploaded_files=3
uploaded_bytes=170371
remote_prefix=skill-sync-sidecar-dev/current-mac
base_record_path=/opt/skill-sync-sidecar/state/openclaw-base-record.json
approved_record=/opt/skill-sync-sidecar/work/current-mac-pullonly/approved-push-recruitment-20260624b/approved-push-record.json
snapshot_id=approved-push-20260624T085723.001434Z
```

OpenClaw cache refresh after approval:

```text
cache_snapshot_id=approved-push-20260624T085723.001434Z
blocked_report_total=0
```

## Mac Autopull Result

The WebDAV local sync folder received:

```text
snapshot_id=approved-push-20260624T085723.001434Z
remote_total=94
beijing-recruitment_hash=6ebeddcef85c3ab335a3e598da3a3d157a56c95190116114dd62175b543dafd9
tianjin-recruitment_hash=1dfdb2e7364449de36faba752810c8b545133400e55ca9b29a4356d985550f49
```

The Mac daemon then applied the downstream changes:

```text
last_cycle_snapshot=approved-push-20260624T085723.001434Z
last_cycle_summary={"noop": 92, "pull": 2}
applied=2
uploaded=0
blocked=0
conflicts=0
base_snapshot_id=approved-push-20260624T085723.001434Z
```

Final dashboard:

```text
dashboard_health=green
dashboard_blocked=0
mac_device=green skills=94 blocked=0
openclaw_device=green skills=94 blocked=0
sync_summary={"noop": 94}
```

## Additional Hardening

`scripts/refresh-openclaw-peer-status.sh` now uses a unique `mktemp` output file and cleanup trap instead of a fixed `<out>.tmp` path. This avoids a race when the background OpenClaw peer refresh LaunchAgent and an operator-triggered refresh run at the same time.

## Decision

Keep:

- OpenClaw unattended sync as `pull-only`.
- Mac governed root as `mixed-scope-root`.
- Mac daemon running with `--continue-on-blocked`.

Use approved-push for reviewed OpenClaw-local changes, and let the Mac daemon pull approved WebDAV updates into `/Users/mac/.cc-switch/skills`.
