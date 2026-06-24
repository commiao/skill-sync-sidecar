# OpenClaw Conflict Local-Wins Resolution 2026-06-24

## Scope

Closed the remaining OpenClaw sidecar sync blockers after the dashboard showed two yellow conflict items.

## Releases

- `e861bdc`: fixed local override acknowledgement when no base record exists.
- `1a7c5f1`: added explicit `approved-push --allow-conflict-local-wins` for audited conflict resolution.

Both OpenClaw sidecar units now run:

```text
PYTHONPATH=/opt/skill-sync-sidecar/releases/1a7c5f1/src
```

Only these sidecar services were restarted:

```text
openclaw-skill-sync-sidecar-pullonly.service
openclaw-skill-sync-sidecar-dryrun.service
```

OpenClaw gateway stayed online throughout.

## Resolution

- `lark-cli-adapter`: no longer a conflict; acknowledged as `local_override` for OpenClaw Linuxbrew Python shebangs.
- `disk-cleanup`: remains `local_only` for OpenClaw private operational use.
- `beijing-recruitment`: pulled from the remote snapshot into `/home/admin/clawd/skills`; backup written under `.skill-sync-backups/20260624-143816-025739`.
- `tianjin-recruitment`: resolved with explicit local-wins approved push and uploaded to WebDAV.

Approved push record:

```text
/opt/skill-sync-sidecar/work/current-mac-pullonly/approved-push-tianjin-local-wins/approved-push-record.json
```

Updated base record:

```text
/opt/skill-sync-sidecar/state/openclaw-base-record.json
```

## Final Verification

Final OpenClaw dry-run summary:

```json
{
  "summary": {
    "local_only": 1,
    "local_override": 1,
    "unchanged": 93
  },
  "plan": {
    "noop": 95
  },
  "blocked": 0,
  "conflicts": 0
}
```

Local dashboard peer status:

```json
{
  "health": "green",
  "sync_summary": {
    "noop": 95
  },
  "blocked": 0
}
```

Dashboard API:

```json
{
  "dashboard_health": "green",
  "dashboard_blocked": 0
}
```
