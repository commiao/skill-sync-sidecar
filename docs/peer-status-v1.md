# Peer Status v1

## Purpose

Peer status is the device-to-gateway status contract. Each device-side Agent publishes one JSON document to WebDAV. The Gateway reads those documents and renders actual per-device state without scanning or guessing remote tool directories.

Gateway remains read-only. Agent owns local observation and sync actions.

## Record Shape

```json
{
  "record_type": "skill-sync-peer-status",
  "peer_status_version": 1,
  "peer_id": "mac",
  "published_at": "2026-06-29T00:00:00+00:00",
  "device": {
    "id": "mac",
    "name": "Mac 本机",
    "kind": "agent",
    "measured_at": "2026-06-29T00:00:00+00:00"
  },
  "capabilities": {
    "tool_status": true,
    "sync_status": true,
    "blocked_report": true
  },
  "tools": [
    {
      "id": "cc-switch",
      "name": "cc-switch",
      "roots": ["/Users/mac/.cc-switch/skills"],
      "path": "/Users/mac/.cc-switch/skills",
      "role": "主同步目录",
      "installed": true,
      "state": "detected",
      "skills": 94,
      "risk": {"ok": 94, "warning": 0, "error": 0},
      "measured_at": "2026-06-29T00:00:00+00:00",
      "note": "已检测到目录"
    }
  ],
  "remote_snapshot": {},
  "sync_plan": {},
  "blocked_report": {},
  "daemon_state": {}
}
```

## Tool States

- `detected`: the Agent can read at least one configured root for this tool.
- `not_found`: no configured root exists on that device.
- `unsupported`: the device is not configured or cannot report tool state yet.
- `error`: a configured root exists, but scanning failed.
- `unknown`: compatibility fallback used by Gateway when an older peer status has no `tools[]`.

`unknown` is not emitted by v1 Agents during normal scanning. It is a Gateway compatibility state for legacy peer files.

## Compatibility

Older peer status documents remain valid. When Gateway reads a peer status without `peer_status_version` or without `tools[]`, it keeps device sync health visible and renders device tool state as `unknown`.

When `skill-sync publish-peer-status --status-file <file>` is used, the file is published without injecting local tool scans. This preserves OpenClaw handoff files and avoids making the Mac publisher pretend to know OpenClaw's local tool roots.

## Ownership Boundaries

- Agent scans local tool roots and publishes `tools[]`.
- Gateway reads WebDAV canonical snapshot and peer status JSON.
- Dashboard displays both layers:
  - `dashboard.tools`: canonical target projection from WebDAV.
  - `dashboard.device_tools`: actual per-device tool state reported by Agents.
