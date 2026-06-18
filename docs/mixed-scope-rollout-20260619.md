# Mixed Scope Root Rollout - 2026-06-19

## Scope

This rollout enabled the explicit `mixed-scope-root` target for governed peer skill roots that intentionally contain both `scope=global` and `scope=project` private skills.

The goal was to remove the previous one-shot workaround where project-scoped OpenClaw skills were pulled into `~/.cc-switch/skills` through `--target codex-project`.

## Code Release

- Commit: `350e800 Add mixed scope sync target`
- Branch: `main`
- Remote: `commiao/skill-sync-sidecar`

Validated locally:

```text
PYTHONPATH=src python3 -m unittest discover -s tests
Ran 79 tests: OK

PYTHONPYCACHEPREFIX=/private/tmp/skill-sync-pycache PYTHONPATH=src python3 -m compileall -q src tests
OK

Mac real dry-run:
total=94
executable=0
blocked=0
unsupported=0
```

## Mac Rollout

Updated:

```text
/Users/mac/Library/LaunchAgents/com.skill-sync-sidecar.plist
```

Added:

```text
--target mixed-scope-root
```

Current LaunchAgent command includes:

```text
python3 -m skill_sync_sidecar sync-daemon
--target mixed-scope-root
--local-root /Users/mac/.cc-switch/skills
--remote file:///Users/mac/public-sync
--prefix skill-sync-sidecar-dev/current-mac
--yes
--interval-seconds 300
```

Rollback backup:

```text
/Users/mac/Library/LaunchAgents/com.skill-sync-sidecar.plist.bak-20260619-mixed-scope
```

Post-restart validation:

```text
LaunchAgent state=running
state summary={"noop": 94}
blocked=0
conflicts=0
applied=0
uploaded=0
```

## OpenClaw Rollout

OpenClaw was reached through Tailscale IP:

```text
root@100.79.177.102
```

Deployed release directory:

```text
/opt/skill-sync-sidecar/releases/350e800
```

Backups:

```text
/etc/systemd/system/openclaw-skill-sync-sidecar-dryrun.service.bak-20260619-mixed-scope
/etc/systemd/system/openclaw-skill-sync-sidecar-pullonly.service.bak-20260619-mixed-scope
```

Updated services:

```text
openclaw-skill-sync-sidecar-dryrun.service
openclaw-skill-sync-sidecar-pullonly.service
```

Both services now use:

```text
Environment=PYTHONPATH=/opt/skill-sync-sidecar/releases/350e800/src
--target mixed-scope-root
--local-root /home/admin/clawd/skills
```

The pull-only service also uses:

```text
--writer-policy pull-only
--continue-on-blocked
```

`--continue-on-blocked` keeps the daemon polling while OpenClaw skill optimization is happening. It does not upload blocked OpenClaw-local edits; it only prevents the service from exiting when pull-only detects unapproved local changes.

Post-restart validation:

```text
openclaw-skill-sync-sidecar-dryrun.service=active
openclaw-skill-sync-sidecar-pullonly.service=active

dryrun:
daemon_status=running
summary={"noop": 94}
blocked=0
conflicts=0
applied=0
uploaded=0

pullonly:
daemon_status=running
stop_on_blocked=false
summary={"noop": 94}
blocked=0
conflicts=0
applied=0
uploaded=0

openclaw-gateway process still running:
pid=2966537
```

## Current Interpretation

The sidecar can now replace cc-switch for WebDAV-backed private skill synchronization across mixed global/project skill roots.

The remaining governance rule still holds: OpenClaw is `pull-only` for unattended sync. If OpenClaw-local edits need to publish upstream, generate a blocked report and run `approved-push` for explicit skill IDs.
