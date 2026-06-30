# Mac Dashboard LaunchAgent Rollout - 2026-06-28

## Goal

Make the local skill-sync dashboard and OpenClaw peer status refresh independent of the current Codex shell session.

## Installed Agents

### Main Sync Daemon

- Label: `com.skill-sync-sidecar`
- State: running
- Target: `mixed-scope-root`
- Local root: `/Users/mac/.cc-switch/skills`
- Remote: `file:///Users/mac/public-sync`
- Prefix: `skill-sync-sidecar-dev/current-mac`
- Writer policy: `push-pull`
- Interval: 300 seconds

### OpenClaw Peer Status Refresh (Legacy)

- Label: `com.skill-sync-sidecar.openclaw-peer-status`
- State: disabled on 2026-06-30
- Interval: 300 seconds
- Output: `/Users/mac/Library/Application Support/skill-sync-sidecar/peers/openclaw-status.json`
- Logs:
  - `/Users/mac/Library/Logs/skill-sync-openclaw-peer-status.out.log`
  - `/Users/mac/Library/Logs/skill-sync-openclaw-peer-status.err.log`

This Mac-side SSH refresher was used before OpenClaw could publish its own
`peer-status v1`. It is now superseded by the OpenClaw systemd timer documented
in `docs/openclaw-peer-status-systemd.md`; keep this LaunchAgent disabled to
avoid two writers updating `skill-sync-sidecar-peer-status/oc-vps.json`.

### Dashboard

- Label: `com.skill-sync-sidecar.dashboard`
- State: running
- URL: `http://127.0.0.1:8765`
- Peer file: `/Users/mac/Library/Application Support/skill-sync-sidecar/peers/openclaw-status.json`
- Logs:
  - `/Users/mac/Library/Logs/skill-sync-dashboard.out.log`
  - `/Users/mac/Library/Logs/skill-sync-dashboard.err.log`

## Validation Snapshot

Dashboard API:

```json
{
  "ok": true,
  "health": "green",
  "dashboard_health": "green",
  "snapshot": "approved-push-20260627T155837.064783Z",
  "local_summary": {
    "noop": 94
  },
  "local_blocked": 0,
  "devices": [
    {
      "id": "mac",
      "health": "green",
      "skills": 94,
      "blocked": 0,
      "policy": "push-pull"
    },
    {
      "id": "oc-vps",
      "health": "green",
      "skills": 94,
      "blocked": 0,
      "policy": "pull-only",
      "local_policy": ["disk-cleanup", "lark-cli-adapter"]
    },
    {
      "id": "win",
      "health": "not_configured",
      "policy": "未接入"
    }
  ]
}
```

Mac `ops-status`:

```json
{
  "ok": true,
  "health": "green",
  "snapshot": "approved-push-20260627T155837.064783Z",
  "base_snapshot": "approved-push-20260627T155837.064783Z",
  "daemon": "running",
  "summary": {
    "noop": 94
  },
  "blocked": 0
}
```

## Notes

- The dashboard is read-only. It does not trigger sync, upload to WebDAV, or apply packages.
- OpenClaw remains `pull-only`; explicit `approved-push` is still required for reviewed OpenClaw-originated changes.
- OpenClaw-private skills such as `disk-cleanup` remain local policy items and are not published to the shared WebDAV snapshot.
- Older stderr log lines may contain historical SSH timeouts from before NAS/OpenClaw connectivity stabilized. New OpenClaw status should come from `openclaw-skill-sync-peer-status.timer` on OpenClaw, not from this Mac job.

## Next Step

Use this Mac observer setup as the baseline before moving the read-only dashboard UI to NAS or onboarding Windows as a third peer.
