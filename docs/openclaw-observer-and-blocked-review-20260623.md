# OpenClaw Observer And Blocked Review - 2026-06-23

## Context

After the mixed-scope rollout, Mac continued to report a clean state:

```text
target=mixed-scope-root
summary={"noop": 94}
blocked=0
unsupported=0
```

OpenClaw services were healthy, but the observer and pull-only service had different plan wording:

```text
pullonly: blocked={"blocked": 2, "noop": 93}, conflicts=0
dryrun: blocked={"blocked": 1, "noop": 93, "push_new": 1}, conflicts=1
```

The mismatch came from the dry-run observer not using the same base record and writer policy as the pull-only service.

## Observer Fix

Updated OpenClaw:

```text
/etc/systemd/system/openclaw-skill-sync-sidecar-dryrun.service
```

Added:

```text
--last-applied-record /opt/skill-sync-sidecar/state/openclaw-base-record.json
--writer-policy pull-only
```

Backup:

```text
/etc/systemd/system/openclaw-skill-sync-sidecar-dryrun.service.bak-20260623-observer-policy
```

Post-restart state:

```text
dryrun:
daemon_status=running
writer_policy=pull-only
summary={"blocked": 2, "noop": 93}
blocked=2
conflicts=0
applied=0
uploaded=0

pullonly:
daemon_status=running
writer_policy=pull-only
summary={"blocked": 2, "noop": 93}
blocked=2
conflicts=0
applied=0
uploaded=0
```

The local systemd template was also updated so future installs keep the same observation policy:

```text
examples/systemd/openclaw-skill-sync-sidecar-dryrun.service
```

## Blocked Queue

Current blocked report:

```text
/opt/skill-sync-sidecar/work/current-mac-pullonly/blocked-report/blocked-report.json
summary={"writer_policy": 2}
```

### disk-cleanup

Status:

```text
status_action=local_new
plan_action=blocked
reason=writer policy pull-only blocks push_new
```

Review result:

```text
do_not_publish_yet
```

Reason:

The skill currently contains only `SKILL.md` and references an external OpenClaw script:

```text
/home/admin/clawd/scripts/disk-cleanup.sh
```

Publishing only the skill directory would make the package incomplete on Mac/Windows/other peers. It should either bundle its executable helper under the skill directory or declare itself as OpenClaw-local/project-scoped with a clear dependency policy.

### lark-cli-adapter

Status:

```text
status_action=push
plan_action=blocked
reason=writer policy pull-only blocks push
```

Diff result:

```text
bin/lark_send_text.py shebang only
remote: #!/usr/bin/env python3
local:  #!/home/linuxbrew/.linuxbrew/bin/python3
```

Review result:

```text
do_not_publish
```

Reason:

OpenClaw system Python is `3.6.8`, which cannot run the adapter because the adapter uses modern Python features. The local shebang points direct execution at the isolated Linuxbrew Python `3.14.2`, so the local change is useful for OpenClaw runtime compatibility.

However, publishing that hard-coded OpenClaw path to WebDAV would make the package less portable for Mac/Windows/other peers. Treat it as an OpenClaw-local runtime override until the sidecar has a first-class local override mechanism or the adapter scripts use a portable launcher.

## Decision

Do not run `approved-push` for the current blocked queue.

The blocked state is expected and safe:

```text
blocked=2
conflicts=0
applied=0
uploaded=0
services=active
gateway_process=still_running
```

## Next Plan

1. Done: add an explicit `ops-status` view that reports Mac/OpenClaw health and the current blocked queue. Validation is recorded in `docs/openclaw-ops-status-health-20260623.md`.
2. Add a local override design for peer-specific runtime patches such as shebangs.
3. Define a packaging rule for skills that depend on external scripts, using `disk-cleanup` as the first test case.
4. Only use `approved-push` when a blocked item is reviewed as portable and complete.
