# NAS Gateway Deployment

## Goal

Run Skill Sync Gateway on NAS or another always-on host, so the shared dashboard no longer depends on Mac exporting static files.

Gateway is read-only:

- It reads the WebDAV canonical snapshot.
- It writes only its own runtime cache.
- It does not run sync-cycle.
- It does not upload skills.
- It does not write into tool skill roots.

## Synology Container Manager

Use the repository Dockerfile and compose example:

```text
Dockerfile
examples/docker-compose.gateway.yml
```

Required environment variables:

```bash
SKILL_SYNC_WEBDAV_URL=https://example.com/path/to/public-sync
SKILL_SYNC_WEBDAV_USER=your-webdav-user
SKILL_SYNC_WEBDAV_PASSWORD=your-webdav-password
SKILL_SYNC_GATEWAY_PREFIX=skill-sync-sidecar-dev/current-mac
SKILL_SYNC_GATEWAY_REMOTE_PEER_STATUS_MAC=mac=skill-sync-sidecar-peer-status/mac.json
```

The container listens on:

```text
http://<nas-ip>:8765
```

Health check:

```bash
curl -sS http://<nas-ip>:8765/healthz
curl -sS http://<nas-ip>:8765/api/status
```

Expected healthy fields:

```text
mode=gateway
health=green
dashboard.health=green
dashboard.blocked=0
```

To show real device state instead of only the canonical snapshot, publish peer status JSON to WebDAV and let the gateway read it with `--remote-peer-status`.

On Mac:

```bash
scripts/publish-mac-peer-status.sh
scripts/install-mac-peer-status-launchd.sh
```

The default published path is:

```text
skill-sync-sidecar-peer-status/mac.json
```

The NAS compose file reads that path by default through:

```text
--remote-peer-status mac=skill-sync-sidecar-peer-status/mac.json
```

## Docker CLI

From the repository root:

```bash
docker build -t skill-sync-sidecar:local .

docker run -d \
  --name skill-sync-gateway \
  --restart unless-stopped \
  -p 8765:8765 \
  -e SKILL_SYNC_WEBDAV_URL="$SKILL_SYNC_WEBDAV_URL" \
  -e SKILL_SYNC_WEBDAV_USER="$SKILL_SYNC_WEBDAV_USER" \
  -e SKILL_SYNC_WEBDAV_PASSWORD="$SKILL_SYNC_WEBDAV_PASSWORD" \
  -v skill-sync-gateway-cache:/cache \
  skill-sync-sidecar:local \
  gateway \
  --remote "$SKILL_SYNC_WEBDAV_URL" \
  --prefix "${SKILL_SYNC_GATEWAY_PREFIX:-skill-sync-sidecar-dev/current-mac}" \
  --cache-dir /cache/current \
  --refresh-interval-seconds 60 \
  --host 0.0.0.0 \
  --port 8765 \
  --remote-peer-status mac=skill-sync-sidecar-peer-status/mac.json
```

## Local Docker Smoke

The Docker image and named-volume cache path were validated locally on 2026-06-28:

```text
image=skill-sync-sidecar:gateway-smoke
mode=gateway
health=green
snapshot=approved-push-20260628T090055.644720Z
total=94
blocked=0
cache=/cache/current
```

The container wrote `index.json` and `skills/` under `/cache/current` as the non-root `skill-sync` user. This validates the same cache mount used by `examples/docker-compose.gateway.yml`.

## Current Mac Validation

Mac already runs the same gateway through launchd at:

```text
http://127.0.0.1:8877
```

This validates the gateway behavior, but NAS remains the preferred always-on host.

## Current NAS Deployment

NAS deployment was completed on 2026-06-28:

```text
http://100.123.208.32:8765
```

See [nas-gateway-rollout-20260628.md](nas-gateway-rollout-20260628.md) for the root cause, deployment command, and validation record.
