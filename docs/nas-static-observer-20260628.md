# NAS Static Observer Rollout - 2026-06-28

## Goal

Expose a read-only Skill Sync observer through the WebDAV/NAS sync folder without making the NAS a writer.

## Published Bundle

- Output directory: `/Users/mac/public-sync/skill-sync-sidecar-dashboard`
- HTML entry: `/Users/mac/public-sync/skill-sync-sidecar-dashboard/index.html`
- Full status: `/Users/mac/public-sync/skill-sync-sidecar-dashboard/status.json`
- Split status files:
  - `devices.json`
  - `blocked-items.json`
  - `tools.json`
  - `access.json`
  - `generated-at.txt`

The directory is synced to NAS through the existing WebDAV/local sync path.

## Installed Automation

- Label: `com.skill-sync-sidecar.nas-dashboard-export`
- Interval: 300 seconds
- Program: `scripts/export-nas-dashboard.sh`
- Logs:
  - `/Users/mac/Library/Logs/skill-sync-nas-dashboard-export.out.log`
  - `/Users/mac/Library/Logs/skill-sync-nas-dashboard-export.err.log`

Related observer agents:

- `com.skill-sync-sidecar`: Mac sync daemon.
- `com.skill-sync-sidecar.openclaw-peer-status`: OpenClaw peer status refresh.
- `com.skill-sync-sidecar.dashboard`: Mac local dashboard at `http://127.0.0.1:8765`.

## Sync Governance During Rollout

The first NAS export correctly showed OpenClaw as yellow because a new OpenClaw-local `finance-auto-bookkeeping` update was blocked by the pull-only writer policy.

Review result:

- Skill: `finance-auto-bookkeeping`
- Change type: finance dashboard read-only review queue improvements.
- Changed files:
  - `SKILL.md`
  - `scripts/finance-dashboard-data.js`
  - `scripts/finance-dashboard-server.js`
- Decision: portable skill content, safe to publish through explicit approval.

Approved-push path:

- Blocked report: `/opt/skill-sync-sidecar/work/current-mac-pullonly/blocked-report-finance-dashboard-20260628`
- Approved push: `/opt/skill-sync-sidecar/work/current-mac-pullonly/approved-push-finance-dashboard-20260628`
- New shared snapshot: `approved-push-20260628T090055.644720Z`

OpenClaw remains `pull-only`; the unattended policy was not changed.

## Final Validation

NAS exported status:

```json
{
  "health": "green",
  "dashboard_health": "green",
  "snapshot": "approved-push-20260628T090055.644720Z",
  "blocked": 0,
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
      "policy": "pull-only"
    },
    {
      "id": "win",
      "health": "not_configured",
      "policy": "未接入"
    }
  ]
}
```

Remote WebDAV check:

```text
webdav_ok=true
webdav_path=skill-sync-sidecar-dashboard/status.json
dashboard_health=green
blocked=0
snapshot=approved-push-20260628T090055.644720Z
```

NAS HTTP check:

```text
http_static_ok=false
```

The NAS HTTP service currently returns a generic 544-byte nginx page for candidate paths such as `/skill-sync-sidecar-dashboard/index.html`, so the dashboard is confirmed present in WebDAV but not yet mapped as a static web directory. This is an exposure/configuration step on NAS, not a sync failure.

Current usable access path is the WebDAV URL recorded in:

```text
/Users/mac/public-sync/skill-sync-sidecar-dashboard/access.json
```

That URL requires the configured WebDAV account. It does not contain credentials.

## Next Step

Map the NAS-synced `skill-sync-sidecar-dashboard` directory into a Synology static site or web-accessible shared-folder route, then rerun:

```bash
scripts/check-nas-dashboard-remote.sh
```

When it reports `http_static_ok=true`, the next infrastructure step is Windows onboarding as a third peer.
