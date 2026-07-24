# Approved Push Runbook

Use this runbook when OpenClaw is `yellow` because pull-only sync found local skill changes that may need to be saved to WebDAV.

## Goal

Save only reviewed OpenClaw-local skill changes while keeping the unattended OpenClaw sidecar policy at `pull-only`.

The CLI and script names keep the historical `approved-push` terminology. The dashboard presents the same operator action as `保存到共享库`.

Do not switch OpenClaw to `push-pull` just to clear a queue. The queue is the safety mechanism.

## When To Wait

Wait before approving when any of these are true:

- A related OpenClaw validation process is still running.
- The same skill hash keeps changing between blocked reports.
- The blocked item is a conflict or delete review, not a writer-policy push.
- The skill is OpenClaw-private, such as `disk-cleanup`.

Useful checks:

```bash
scripts/blocked-queue.sh
scripts/ops-watch.sh
ssh -o ConnectTimeout=20 -o BatchMode=yes root@100.79.177.102 \
  'ps -eo pid,ppid,lstart,cmd | grep -E "recruitment|job-monitor|job-cron|skill_sync_sidecar" | grep -v grep || true'
```

## Dry-Run Approval

Run the helper from the repo on Mac. It refreshes the OpenClaw cache, regenerates the blocked report, and runs `approved-push --dry-run`.

```bash
scripts/openclaw-approved-push-batch.sh \
  beijing-recruitment \
  hebei-recruitment
```

For new OpenClaw-local skills, the helper allows `push_new` by default. Use `--no-allow-new` if the batch must contain only updates.

Dry-run must show `safe_to_push: true`. If it reports stale hashes, regenerate the queue after the producer stops writing and try again.

## Save To Shared Library

After reviewing the dry-run output, rerun with `--yes`.

```bash
scripts/openclaw-approved-push-batch.sh --yes \
  beijing-recruitment \
  hebei-recruitment
```

This writes an approved-push audit record under:

```text
/opt/skill-sync-sidecar/work/current-mac-pullonly/
```

It also updates:

```text
/opt/skill-sync-sidecar/state/openclaw-base-record.json
```

## Verify OpenClaw

Refresh and publish OpenClaw peer status:

```bash
scripts/publish-openclaw-peer-status.sh
```

Expected output:

```text
health=green
blocked=0
```

If OpenClaw is still yellow, run:

```bash
scripts/blocked-queue.sh
```

A new yellow item means OpenClaw produced another local change after the approved push. Treat it as a new approval cycle.

## Sync Mac

Pull the approved snapshot into the Mac cc-switch tree:

```bash
PYTHONPATH=src python3 -m skill_sync_sidecar sync-cycle \
  --local-root /Users/mac/.cc-switch/skills \
  --target mixed-scope-root \
  --last-applied-record "$HOME/Library/Application Support/skill-sync-sidecar/base-record.json" \
  --cache-dir "$HOME/Library/Caches/skill-sync-sidecar/cache" \
  --work-dir "$HOME/Library/Application Support/skill-sync-sidecar/work" \
  --allow-new \
  --writer-policy push-pull \
  --cc-switch-webdav \
  --prefix skill-sync-sidecar-dev/current-mac \
  --yes \
  --json
```

Then adopt the shared base:

```bash
PYTHONPATH=src python3 -m skill_sync_sidecar adopt-base \
  --local-root /Users/mac/.cc-switch/skills \
  --remote-snapshot "$HOME/Library/Caches/skill-sync-sidecar/cache" \
  --out "$HOME/Library/Application Support/skill-sync-sidecar/base-record.json" \
  --yes \
  --json
```

Publish Mac peer status:

```bash
scripts/publish-mac-peer-status.sh
```

## Final Check

```bash
PYTHONPATH=src python3 -m skill_sync_sidecar monitor-summary \
  --url http://100.123.208.32:8765/api/overview \
  --timeout-seconds 60

scripts/blocked-queue.sh
```

Expected final state:

```text
health: green
blocked: 0
alerts: 0
warnings: 0
```

## Recovery Notes

- If SSH to OpenClaw times out, retry once with a higher `OPENCLAW_CONNECT_TIMEOUT`.
- If dry-run refuses stale hashes, do not force it. Rebuild the blocked report after the producer has stopped.
- If a skill should stay private, add or keep it as an explicit local override instead of approved-pushing it.
- Keep OpenClaw gateway untouched; this workflow uses sidecar state and WebDAV only.
