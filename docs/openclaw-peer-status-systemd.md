# OpenClaw Peer Status Publisher

Purpose: let OpenClaw publish its own `peer-status v1` to WebDAV instead of relying on a Mac-side SSH refresh job.

This is a read-only status publisher:

- Reads `/home/admin/clawd/skills`.
- Reads the existing pull-only cache/base/state under `/opt/skill-sync-sidecar`.
- Refreshes the pull-only WebDAV cache before publishing, so `snapshot_id` does not lag after an approved push.
- Writes `/opt/skill-sync-sidecar/state/openclaw-peer-status.json`.
- Publishes `skill-sync-sidecar-peer-status/oc-vps.json` to WebDAV.
- Does not run `sync-cycle`, `sync-apply`, or `approved-push`.
- Does not restart OpenClaw gateway.
- Does not modify `openclaw-skill-sync-sidecar-pullonly.service` or `openclaw-skill-sync-sidecar-dryrun.service`.

## Files

- Runtime script: `scripts/publish-openclaw-local-peer-status.sh`
- Installer: `scripts/install-openclaw-peer-status-systemd.sh`
- Service template: `examples/systemd/openclaw-skill-sync-peer-status.service`
- Timer template: `examples/systemd/openclaw-skill-sync-peer-status.timer`

## Install

Copy the release to OpenClaw under `/opt/skill-sync-sidecar/releases/peer-status-v1`, then run on OpenClaw:

```bash
/opt/skill-sync-sidecar/releases/peer-status-v1/scripts/install-openclaw-peer-status-systemd.sh --dry-run
/opt/skill-sync-sidecar/releases/peer-status-v1/scripts/install-openclaw-peer-status-systemd.sh --yes
```

The timer defaults to every 300 seconds:

```bash
systemctl list-timers openclaw-skill-sync-peer-status.timer
systemctl status openclaw-skill-sync-peer-status.service --no-pager
journalctl -u openclaw-skill-sync-peer-status.service -n 50 --no-pager
```

## Verify

From any machine that can read the NAS gateway:

```bash
python3 -m skill_sync_sidecar monitor-summary \
  --url http://100.123.208.32:8765/api/summary \
  --timeout-seconds 60
```

Expected:

- `dashboard_health: green`
- `blocked: 0`
- `oc-vps / OpenClaw` freshness remains fresh without the Mac SSH refresh job
- Dashboard `device_tools` shows `openclaw` with root `/home/admin/clawd/skills`

## 2026-06-30 Rollout Evidence

Commit deployed: `8fad400 Add OpenClaw local peer-status publisher`.

OpenClaw runtime:

- Release path: `/opt/skill-sync-sidecar/releases/peer-status-v1`
- Python: `/opt/skill-sync-sidecar/venv-0.1.3/bin/python`
- Timer: `openclaw-skill-sync-peer-status.timer`
- Existing services preserved:
  - `openclaw-skill-sync-sidecar-pullonly.service`
  - `openclaw-skill-sync-sidecar-dryrun.service`

Final validated snapshot:

- Snapshot: `approved-push-20260630T155739.385071Z`
- Canonical total: `99`
- NAS dashboard health: `green`
- Blocked queue: `0`
- Mac peer: `health=green`, `skills=99`, `blocked=0`, `tools=5`
- OpenClaw peer: `health=green`, `skills=99`, `blocked=0`
- OpenClaw self-published peer status path: `skill-sync-sidecar-peer-status/oc-vps.json`

Important interpretation:

- The OpenClaw peer status is now written by OpenClaw itself, not by the legacy Mac SSH proxy job.
- `openclaw-skill-sync-peer-status.service` is a systemd oneshot. `ActiveState=inactive` after a run is expected when `Result=success` and `ExecMainStatus=0`.
- `disk-cleanup` remains an OpenClaw local-only private skill and must not be treated as canonical shared content.

## Rollback

The publisher is independent of the sync daemon and OpenClaw gateway. To disable only this publisher:

```bash
systemctl disable --now openclaw-skill-sync-peer-status.timer
rm -f /etc/systemd/system/openclaw-skill-sync-peer-status.service
rm -f /etc/systemd/system/openclaw-skill-sync-peer-status.timer
systemctl daemon-reload
```
