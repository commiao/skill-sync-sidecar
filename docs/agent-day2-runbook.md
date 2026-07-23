# Agent Day-2 Runbook

This runbook is for operating the current Mac, OpenClaw, WebDAV, and NAS Gateway chain after the initial rollout.

Current scope:

- Mac is the normal `push-pull` peer for the canonical WebDAV skill set.
- OpenClaw is a peer, but unattended automation stays `pull-only`.
- NAS Gateway is read-only and aggregates WebDAV snapshot plus peer-status files.
- Windows is intentionally deferred and should appear under `dashboard.planned_devices`, not the active device health list.

## Health Model

There are two different counts in the dashboard:

- Canonical projection: what the WebDAV snapshot targets for each tool.
- Device measured state: what each device Agent actually sees in local tool roots.

These counts are allowed to differ. For example, OpenClaw can report one extra local-only private skill such as `disk-cleanup` while the canonical snapshot stays clean. Treat this as expected when the device card is green and the skill is covered by local policy.

Healthy NAS summary:

```text
health=green
dashboard.health=green
dashboard.blocked=0
remote_snapshot.total=<expected canonical total>
dashboard.devices.mac.health=green
dashboard.devices.oc-vps.health=green
dashboard.planned_devices includes win with policy="本阶段跳过"
```

## Mac Agent

Mac publishes two things:

- Local peer status for Mac itself.
- Approved canonical snapshot changes when explicitly allowed.

One-shot status publish:

```bash
scripts/publish-mac-peer-status.sh
```

Install or refresh the LaunchAgent:

```bash
scripts/install-mac-peer-status-launchd.sh
launchctl print gui/$(id -u)/com.skill-sync-sidecar.mac-peer-status
```

When the WebDAV snapshot was just pulled to the app cache instead of the local WebDAV mirror, pass the cache explicitly:

```bash
SKILL_SYNC_REMOTE_SNAPSHOT="$HOME/Library/Application Support/skill-sync-sidecar/cache/current-mac" \
  scripts/publish-mac-peer-status.sh
```

Confirm Mac base alignment:

```bash
PYTHONPATH=src python3 -m skill_sync_sidecar sync-status \
  --local-root "$HOME/.cc-switch/skills" \
  --remote-snapshot "$HOME/Library/Application Support/skill-sync-sidecar/cache/current-mac" \
  --last-applied-record "$HOME/Library/Application Support/skill-sync-sidecar/base-record.json" \
  --json
```

Expected after a clean adoption:

```text
summary.unchanged=<canonical total>
has_conflicts=false
local_overrides.total=0
```

If local and remote hashes match but base records are missing, adopt the base instead of pushing:

```bash
PYTHONPATH=src python3 -m skill_sync_sidecar adopt-base \
  --local-root "$HOME/.cc-switch/skills" \
  --remote-snapshot "$HOME/Library/Application Support/skill-sync-sidecar/cache/current-mac" \
  --out "$HOME/Library/Application Support/skill-sync-sidecar/base-record.json" \
  --last-applied-record "$HOME/Library/Application Support/skill-sync-sidecar/base-record.json" \
  --prefix skill-sync-sidecar-dev/current-mac \
  --dry-run \
  --json
```

Use `--yes` only when `safe_to_adopt=true` and `blocked=0`.

## OpenClaw Agent

OpenClaw publishes its own peer status from the OpenClaw host. It must use the isolated Python 3.9+ runtime and must not replace system Python.

Runtime defaults:

```text
release=/opt/skill-sync-sidecar/releases/peer-status-v1
python=/opt/skill-sync-sidecar/venv-0.1.3/bin/python
skill_root=/home/admin/clawd/skills
service=openclaw-skill-sync-peer-status.service
timer=openclaw-skill-sync-peer-status.timer
```

Run once on OpenClaw:

```bash
systemctl start openclaw-skill-sync-peer-status.service
systemctl --no-pager --full status openclaw-skill-sync-peer-status.service
systemctl --no-pager --full list-timers openclaw-skill-sync-peer-status.timer
```

Expected service interpretation:

- `Active: inactive (dead)` is normal for this oneshot service after it exits.
- Success is `code=exited, status=0/SUCCESS`.
- Logs should show `health=green`, `blocked=0`, `peer_status_version=1`, and `tools=1`.

Install or refresh the systemd timer on OpenClaw:

```bash
/opt/skill-sync-sidecar/releases/peer-status-v1/scripts/install-openclaw-peer-status-systemd.sh --dry-run
/opt/skill-sync-sidecar/releases/peer-status-v1/scripts/install-openclaw-peer-status-systemd.sh --yes
```

Safety boundaries:

- Do not replace OpenClaw system Python.
- Do not restart OpenClaw gateway for peer-status changes.
- Do not switch unattended OpenClaw sync to push mode.
- Keep OpenClaw local-only private skills out of the canonical shared snapshot unless they are explicitly reviewed for reuse.

## NAS Gateway

NAS Gateway is read-only. It reads:

- `skill-sync-sidecar-dev/current-mac` canonical snapshot.
- `skill-sync-sidecar-peer-status/mac.json`.
- `skill-sync-sidecar-peer-status/oc-vps.json`.

Check from any machine that can reach Tailscale/NAS:

```bash
curl -sS http://100.123.208.32:8765/healthz
curl -sS http://100.123.208.32:8765/api/overview
```

Operator summary from the repo:

```bash
scripts/operator-status.sh
scripts/monitor-nas-summary.sh
scripts/blocked-queue.sh
scripts/ops-watch.sh
```

`operator-status.sh` is the shortest "do I need to do anything right now?" view. `/healthz` checks process health and cache freshness. `/api/overview` is the operator source of truth for device health, blocked queue, and planned devices.

## Recovery Checklist

Use this order when the dashboard is yellow or stale:

1. Read the NAS summary first:

   ```bash
   scripts/operator-status.sh
   scripts/monitor-nas-summary.sh
   scripts/blocked-queue.sh
   ```

2. If Mac is stale, publish Mac status:

   ```bash
   scripts/publish-mac-peer-status.sh
   ```

3. If OpenClaw is stale, trigger only the peer-status oneshot:

   ```bash
   ssh root@100.79.177.102 'systemctl start openclaw-skill-sync-peer-status.service'
   ```

4. If OpenClaw has pull-only blocked items, use approved push only after review:

   ```bash
   scripts/openclaw-approved-push-batch.sh skill-id
   scripts/openclaw-approved-push-batch.sh --yes skill-id
   ```

5. Pull the latest snapshot to Mac and align base records:

   ```bash
   PYTHONPATH=src python3 -m skill_sync_sidecar pull-cache \
     --cc-switch-webdav \
     --prefix skill-sync-sidecar-dev/current-mac \
     --out "$HOME/Library/Application Support/skill-sync-sidecar/cache/current-mac" \
     --json
   ```

6. Wait one Gateway refresh interval or re-read `/api/overview`.

Stop and review instead of applying when:

- `blocked > 0` and the skill was not explicitly reviewed.
- `has_conflicts=true`.
- A package contains credentials, generated caches, or host-specific hard-coded runtime paths.
- OpenClaw local edits are still actively changing.

## Current Known Local Policy

OpenClaw local-only/private handling:

- `disk-cleanup` is OpenClaw internal private usage and is not shared.
- Local-only private skills can make OpenClaw measured counts differ from canonical counts.
- The dashboard should show that distinction through `dashboard.device_tools`, not by treating Gateway as a tool scanner.

Windows:

- Windows Agent is skipped in the current rollout.
- Gateway should keep Windows under `dashboard.planned_devices`.
- Do not treat missing Windows status as a health issue during the Mac/OpenClaw phase.
