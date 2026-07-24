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

### 一次性处理当前待审批 OpenClaw 写盘项（可选）

有时会出现 5~10 条都是本次 review 里的 `writer_policy`，手工逐条点太慢。你可以一次性提取并提交当前待审批页里的可发布 ID。

```bash
# 1) 只做本地预览（不发指令）
scripts/openclaw-approved-push-batch-all.sh --source "oc-vps / OpenClaw" --print-ids-only

# 2) 确认结果后，直接一次性发布（默认会刷新 peer-status；如不需要刷新可加 --no-refresh）
scripts/openclaw-approved-push-batch-all.sh --source "oc-vps / OpenClaw" --yes

# 需要限制数量时可加 --max，例如 --max 5
```

脚本会始终输出 `openclaw_pending_count=`，同时输出 `openclaw_pending_ids=`；通常行为如下：

```text
openclaw_pending_ids=finance-auto-bookkeeping local-writer
openclaw_pending_count=2
```

无可发布项时输出：

```text
openclaw_pending_ids=
openclaw_pending_count=0
no actionable openclaw writer_policy entries found
```

`openclaw_pending_count=0` 表示当前筛选条件下没有可直接发布的 `writer_policy` 条目，不代表链路故障。

注意：

- 这会把所有当前筛出的 `writer_policy` 项一并发布；`delete` / `conflict` 项仍不会被 `approved-push` 写入 WebDAV。
- 如果发现列表不全，先点一次 dashboard 刷新，确认卡片状态无刷新卡住（`blocked` 是否仍未清空）。

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

## 持续巡检（推荐）

如果你要持续观察“是否需要处理”但不想手工反复点击，可以启动只读巡检：

```bash
bash scripts/watch-sync-health-30m.sh
```

建议：

- `1800` 表示每 30 分钟检查一次（默认）。
- `--max-iterations 1` 表示只跑 1 次，适合本地快速验明。
- 不会改文件、不发命令，只做只读检查。
- 输出行前有 `*` 表示相对于上次有状态变化；若出现 `degraded`，脚本会补充 `blocked-queue.sh` 与侧边监控快照，便于快速判断。

单次验证命令：

```bash
bash scripts/watch-sync-health-once.sh
```

健康绿态关键输出应是：

```text
health=green
blocked=0
alerts=0
warnings=0
```

## Recovery Notes

如果当前机器无法访问 NAS 接口（例如本机网络限制），可先做一次本机演练验证脚本：

```bash
tmp_dir=$(mktemp -d)
cat > "$tmp_dir/api/overview" <<'JSON'
{"dashboard":{"health":"green","blocked":0,"alerts":0,"warnings":0}}
JSON
SKILL_SYNC_MONITOR_SUMMARY_FILE="$tmp_dir/api/overview" bash scripts/watch-sync-health-once.sh
```

or

```bash
bash scripts/watch-sync-health-once.sh --monitor-summary-file "$tmp_dir/api/overview"
```

- If SSH to OpenClaw times out, retry once with a higher `OPENCLAW_CONNECT_TIMEOUT`.
- If dry-run refuses stale hashes, do not force it. Rebuild the blocked report after the producer has stopped.
- If a skill should stay private, add or keep it as an explicit local override instead of approved-pushing it.
- Keep OpenClaw gateway untouched; this workflow uses sidecar state and WebDAV only.
