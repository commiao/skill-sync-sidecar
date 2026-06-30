# OpenClaw Peer Status Publisher

Purpose: let OpenClaw publish its own `peer-status v1` to WebDAV instead of relying on a Mac-side SSH refresh job.

This is a read-only status publisher:

- Reads `/home/admin/clawd/skills`.
- Reads the existing pull-only cache/base/state under `/opt/skill-sync-sidecar`.
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

## Rollback

The publisher is independent of the sync daemon and OpenClaw gateway. To disable only this publisher:

```bash
systemctl disable --now openclaw-skill-sync-peer-status.timer
rm -f /etc/systemd/system/openclaw-skill-sync-peer-status.service
rm -f /etc/systemd/system/openclaw-skill-sync-peer-status.timer
systemctl daemon-reload
```
