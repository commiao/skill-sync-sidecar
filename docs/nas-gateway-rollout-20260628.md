# NAS Gateway Rollout - 2026-06-28

## Root Cause

The gateway was not running on NAS because it had not been deployed there yet.

Earlier checks used `admin@100.123.208.32`, which failed with `Permission denied`. The working SSH account is:

```text
commiao@100.123.208.32
```

The `commiao` account is in the NAS `administrators` group and can run Container Manager Docker through:

```text
/var/packages/ContainerManager/target/usr/bin/docker
```

DSM API login with the configured WebDAV account returned error code `402`, so the WebDAV account should not be treated as a DSM management account.

## Deployment

Deployed under:

```text
/volume1/docker/skill-sync-gateway
```

Started with:

```bash
sudo -n /var/packages/ContainerManager/target/usr/bin/docker compose \
  --env-file .env \
  -f examples/docker-compose.gateway.yml \
  up -d --build
```

The `.env` file contains the WebDAV URL/user/password and is stored only on NAS with mode `600`.

## Running Service

Container:

```text
skill-sync-gateway
```

Image:

```text
skill-sync-sidecar:local
```

URL:

```text
http://100.123.208.32:8765
```

Validation:

```text
mode=gateway
health=green
snapshot=approved-push-20260628T090055.644720Z
total=94
blocked=0
devices=gateway,mac,oc-vps,win
```

The runtime cache was written under `/cache/current` inside the container and contains `index.json` plus `skills/`.

## Safety Check

Existing NAS services were not stopped or reconfigured:

```text
report-portal             Up
kg-hub-ingester           Up
kg-hub-watchdog           Up
kg-hub-server             Up
openclaw-tj-proxy-18888   Up
kg-hub-nas-probe          Up
kg-hub-falkordb           Up
```

The OpenClaw Tianjin proxy still responds on port `18888`.

## Operations

Check status from Mac:

```bash
curl -sS http://100.123.208.32:8765/healthz
curl -sS http://100.123.208.32:8765/api/status
```

Check status on NAS:

```bash
ssh commiao@100.123.208.32 \
  'sudo -n /var/packages/ContainerManager/target/usr/bin/docker ps --filter name=skill-sync-gateway'
```

Tail logs:

```bash
ssh commiao@100.123.208.32 \
  'sudo -n /var/packages/ContainerManager/target/usr/bin/docker logs --tail 80 skill-sync-gateway'
```

Restart:

```bash
ssh commiao@100.123.208.32 \
  'cd /volume1/docker/skill-sync-gateway && sudo -n /var/packages/ContainerManager/target/usr/bin/docker compose --env-file .env -f examples/docker-compose.gateway.yml up -d'
```
