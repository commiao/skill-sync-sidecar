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
SKILL_SYNC_GATEWAY_REMOTE_PEER_STATUS_OPENCLAW=oc-vps=skill-sync-sidecar-peer-status/oc-vps.json
```

The container listens on:

```text
http://<nas-ip>:8765
```

Health check:

```bash
curl -sS http://<nas-ip>:8765/healthz
curl -sS http://<nas-ip>:8765/api/summary
curl -sS http://<nas-ip>:8765/api/status
```

The browser dashboard refreshes from `/api/summary`, which keeps only the fields
needed for the UI and avoids returning the full projection/debug payload on every
poll. Use `/api/status` when you need the complete diagnostic JSON.

`/healthz` is intentionally lightweight: it reports process health and summary
cache metadata without reading WebDAV. Docker health checks should use
`/healthz`, not `/api/summary`.

`/api/summary` is stale-safe. It has a short refresh budget and returns the last
successful summary when the live WebDAV/peer-status aggregation is slow. Check
`summary_cache.state`:

- `fresh`: live or recently refreshed data.
- `stale`: returned from the last successful cache while a refresh is in flight.
- `miss`: no successful cache exists yet; inspect WebDAV and gateway logs.

Expected healthy fields:

```text
mode=gateway
health=green
dashboard.health=green
dashboard.blocked=0
summary_cache.state=fresh
```

Operator monitor:

```bash
scripts/monitor-nas-summary.sh
SKILL_SYNC_MONITOR_FAIL_ON_ALERT=1 scripts/monitor-nas-summary.sh
```

The monitor reads `/api/summary` and turns it into an operator report. It alerts
on non-green dashboard health, blocked sync items, stale Mac/OpenClaw peer
status, snapshot drift, missing device tool reports, and abnormal canonical
snapshot totals.

In Docker Compose, `skill-sync-monitor` runs the same check on an interval and
writes:

```text
/cache/monitor/last-report.json
/cache/monitor/last-report.txt
/cache/monitor/events.jsonl
```

Inspect it on NAS with:

```bash
docker exec skill-sync-monitor cat /cache/monitor/last-report.txt
docker logs --tail 50 skill-sync-monitor
```

Day-2 operator checks from the Mac repo:

```bash
scripts/blocked-queue.sh
scripts/ops-watch.sh
```

`blocked-queue.sh` is the fastest "do I need to act?" view. It reads the NAS
gateway summary and prints the blocked approval queue with the relevant local,
remote, and base hashes when present.

`ops-watch.sh` is the wider read-only view. It checks the NAS monitor summary,
Mac launchd peer-status job, OpenClaw peer-status job, and recent local logs.
It does not run `approved-push` or mutate any skill tree.

When OpenClaw is yellow only because reviewed local skill changes are waiting
behind the pull-only writer policy, use the explicit approval runbook:

```bash
scripts/openclaw-approved-push-batch.sh skill-id-1 skill-id-2
scripts/openclaw-approved-push-batch.sh --yes skill-id-1 skill-id-2
```

Keep the first command as dry-run review. Use `--yes` only after the producer
work has settled and the queue hashes still match. See
[approved-push-runbook.md](approved-push-runbook.md) for the full checklist.

To show real device state instead of only the canonical snapshot, publish peer status JSON to WebDAV and let the gateway read it with `--remote-peer-status`.

Peer status v1 separates responsibilities:

- Device Agent scans actual local tool roots and publishes `tools[]`.
- Gateway only reads WebDAV and aggregates status.
- Dashboard shows `dashboard.tools` as canonical projection and `dashboard.device_tools` as per-device measured state.

See [peer-status-v1.md](peer-status-v1.md) for the JSON contract.

On Mac:

```bash
scripts/publish-mac-peer-status.sh
scripts/install-mac-peer-status-launchd.sh
scripts/publish-openclaw-peer-status.sh
scripts/install-openclaw-peer-status-launchd.sh
```

The default published paths are:

```text
skill-sync-sidecar-peer-status/mac.json
skill-sync-sidecar-peer-status/oc-vps.json
```

The NAS compose file reads those paths by default through:

```text
--remote-peer-status mac=skill-sync-sidecar-peer-status/mac.json
--remote-peer-status oc-vps=skill-sync-sidecar-peer-status/oc-vps.json
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
  --remote-peer-status mac=skill-sync-sidecar-peer-status/mac.json \
  --remote-peer-status oc-vps=skill-sync-sidecar-peer-status/oc-vps.json
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
