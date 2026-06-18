# OpenClaw Approved Push Recruitment Validation - 2026-06-19

## Goal

Validate the real blocked-report to approved-push governance path with actual OpenClaw skill optimization output.

## Source Changes

The OpenClaw skill optimization session produced three local OpenClaw changes:

```text
beijing-recruitment  push
lark-cli-adapter     push
tianjin-recruitment  push_new
```

OpenClaw `pull-only` blocked these local-to-WebDAV changes as intended:

```text
blocked_report=/opt/skill-sync-sidecar/work/current-mac-pullonly/blocked-report/blocked-report.json
writer_policy=pull-only
summary={'writer_policy': 3}
sync_status={'push': 2, 'local_new': 1, 'unchanged': 91}
has_conflicts=false
```

The pull-only service was `inactive/dead` because it stops on blocked cycles by design. The dry-run observer and OpenClaw gateway remained running.

## Approved Push

Dry-run:

```text
approved_skill_ids=beijing-recruitment,lark-cli-adapter,tianjin-recruitment
current_plan_summary={'noop': 91, 'push': 2, 'push_new': 1}
upload_preview_total=94
upload_preview_files=4
deferred_pushes=[]
safe_to_push=true
```

Execution:

```text
command=skill-sync approved-push --yes
release=/opt/skill-sync-sidecar/releases/c2401cf
uploaded_files=4
uploaded_bytes=121902
base_record_path=/opt/skill-sync-sidecar/state/openclaw-base-record.json
approved_record=/opt/skill-sync-sidecar/work/current-mac-pullonly/approved-push-recruitment-20260619/approved-push-record.json
snapshot_id=approved-push-20260618T172643.235262Z
remote_total=94
```

Only the three approved skill archives and final merged `index.json` were uploaded.

## OpenClaw Result

The OpenClaw WebDAV cache was refreshed and the pull-only service was restarted after approval:

```text
openclaw_sync_status={'unchanged': 94}
pullonly_service=active running
pullonly_cycle_summary={'noop': 94}
blocked=0
applied=0
uploaded=0
openclaw-gateway pid=2966537
gateway_restarted=false
```

## Mac Result

The Mac WebDAV sync folder received the approved 94-skill snapshot:

```text
remote_snapshot=approved-push-20260618T172643.235262Z
remote_total=94
has_beijing-recruitment=true
has_lark-cli-adapter=true
has_tianjin-recruitment=true
```

The three approved skills are present in `/Users/mac/.cc-switch/skills`:

```text
beijing-recruitment/SKILL.md exists
lark-cli-adapter/SKILL.md exists
tianjin-recruitment/SKILL.md exists
```

Mac status after the one-shot pull:

```text
last_cycle_summary={'noop': 91, 'pull': 2, 'pull_new': 1}
applied=3
uploaded=0
sync_summary={'noop': 94}
overall_ok=true
```

## Scope Note

The approved recruitment skills are `scope=project` packages. A normal `cc-switch-global` sync target refuses to install project-scoped packages, so the Mac one-shot pull used:

```text
--target codex-project
--project-root /Users/mac/.cc-switch
--local-root /Users/mac/.cc-switch/skills
```

This kept the scope guard intact while applying the reviewed project-scoped OpenClaw skills into the existing local skill root. It was a one-shot transition path, not the long-term daemon target.

Future unattended Mac/OpenClaw sync for governed roots that intentionally contain both global and project-scoped packages should use:

```text
--target mixed-scope-root
--local-root /Users/mac/.cc-switch/skills
```

or the equivalent OpenClaw root:

```text
--target mixed-scope-root
--local-root /home/admin/clawd/skills
```

This keeps scope mismatch protection for pure targets while making the mixed private-skill store explicit.

## Outcome

The real governance loop passed:

```text
OpenClaw local optimization
-> pull-only blocked-report
-> approved-push dry-run
-> approved-push yes
-> WebDAV 94 snapshot
-> OpenClaw unchanged 94
-> Mac pulled approved changes
```

This validates the mechanism for continuing business skill optimization while keeping OpenClaw local edits from being silently published.
