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

## Why "save" may look like no change

Common causes:

- `approved=0` after save: usually means this item is no longer a valid publish candidate in the latest report (stale/changed after pre-check).
- The list contains only conflict/delete items: those are not saved by the one-click button.
- Publish permission is not enabled: executor is in check-only mode (`allow_publish=0`).
- The item is a normal OpenClaw pull-only writer-policy block. It is intentional ordinary deferral, not an error.

How to verify quickly:

1. Do `scripts/openclaw-approved-push-batch.sh <skill-id>` first (dry-run).
2. If output is clear, do `scripts/openclaw-approved-push-batch.sh --yes <skill-id>`.
3. Then run `scripts/publish-openclaw-peer-status.sh` and check `blocked=0`.

Useful checks:

```bash
bash scripts/blocked-queue.sh
scripts/ops-watch.sh
ssh -o ConnectTimeout=20 -o BatchMode=yes root@100.79.177.102 \
  'ps -eo pid,ppid,lstart,cmd | grep -E "recruitment|job-monitor|job-cron|skill_sync_sidecar" | grep -v grep || true'
```

> Note: `blocked-queue.sh` is a Bash script. Use `bash` (or execute it directly) instead of `python3 scripts/blocked-queue.sh`.

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

If you want peer status to refresh automatically, add `--refresh-peer-status`
when you run `openclaw-approved-push-batch.sh --yes`:

```bash
scripts/openclaw-approved-push-batch.sh --yes --refresh-peer-status finance-auto-bookkeeping
```

Otherwise, do the two-step verification manually:

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
bash scripts/blocked-queue.sh
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

bash scripts/blocked-queue.sh
```

Expected final state:

```text
health: green
blocked: 0
alerts: 0
warnings: 0
```

## When Save Shows No-Change

If the dashboard says `approved=0` after save or no skill was written, use this sequence:

```bash
bash scripts/openclaw-approved-push-batch.sh <skill-id>

# if still shows 0/clear candidate, regenerate after writer stops changing
bash scripts/openclaw-approved-push-batch.sh <skill-id>

# refresh peer status and confirm blocked list
bash scripts/publish-openclaw-peer-status.sh
bash scripts/blocked-queue.sh
```

Interpretation:

- `reason=none of the requested skills are currently blocked publish candidates`: the item is already handled or changed out of date.
- `reason=writer policy pull-only blocks push`: waiting until manual review; this is normal protection in pull-only mode.
- Other reasons with `approved=0`: treat as changing inputs and rerun dry-run before save.

If status shows `blocked` again, re-open the queue and only save skill ids that are currently `status_action=push` / `plan_action=blocked` and keep `category=writer_policy`.

### 一次命令排障（推荐）

在遇到“点了保存后显示 `approved=0` / 看不到变化”时，先用诊断脚本：

```bash
bash scripts/openclaw-approved-push-diagnose.sh <skill-id>
```

它会做 3 件事：

1. 运行 `openclaw-approved-push-batch.sh` 预检；
2. 输出 `safe_to_push / approved / reason`；
3. 刷新并读取 OpenClaw peer-status + blocked queue。

确认可写后可一键发布：

```bash
bash scripts/openclaw-approved-push-diagnose.sh --yes <skill-id>
```

若 `reason` 为 `none of the requested skills are currently blocked publish candidates`，一般表示当前时刻没有待发布候选：

- 可能该项已经在上一次提交中已处理；
- 本地变更尚未稳定，队列刚更新；
- 你点了非当前待审项。

这时请等待生产端稳定后再跑一次诊断。

## Recovery Notes

- If SSH to OpenClaw times out, retry once with a higher `OPENCLAW_CONNECT_TIMEOUT`.
- If dry-run refuses stale hashes, do not force it. Rebuild the blocked report after the producer has stopped.
- If a skill should stay private, add or keep it as an explicit local override instead of approved-pushing it.
- Keep OpenClaw gateway untouched; this workflow uses sidecar state and WebDAV only.
