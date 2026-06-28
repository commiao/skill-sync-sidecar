# Gateway Observer Rollout - 2026-06-28

## Goal

Replace the Mac-exported static dashboard as the preferred shared observer path.

The gateway serves the dashboard UI from any host that can reach WebDAV. It reads the canonical snapshot directly and keeps only a runtime cache, so it does not depend on:

- `/Users/mac/public-sync/skill-sync-sidecar-dashboard`
- Mac launchd export jobs
- NAS Web Station static folder mapping

## Command

Smoke-tested command:

```bash
PYTHONPATH=src python3 -m skill_sync_sidecar gateway \
  --cc-switch-webdav \
  --prefix skill-sync-sidecar-dev/current-mac \
  --cache-dir /private/tmp/skill-sync-gateway-smoke \
  --refresh-interval-seconds 0 \
  --host 127.0.0.1 \
  --port 8877
```

For NAS/Linux deployment, adapt:

```text
examples/systemd/skill-sync-gateway.service
```

## Validation

The gateway successfully pulled the real WebDAV snapshot into its own cache and served `/api/status`:

```text
ok=True
health=green
mode=gateway
snapshot=approved-push-20260628T090055.644720Z
total=94
dashboard_health=green
blocked=0
devices=gateway,mac,oc-vps,win
cache_path=/private/tmp/skill-sync-gateway-smoke
```

The gateway cache contained its own `index.json`, proving the page was not reading the Mac-exported static dashboard directory.

## Current Recommendation

Use `gateway` as the long-term shared observer. Keep the static NAS observer as a fallback for environments that cannot run Python or cannot access WebDAV directly.

Next infrastructure step: deploy the gateway service on NAS or another always-on host, then point users at that gateway URL instead of the Mac-local dashboard.
