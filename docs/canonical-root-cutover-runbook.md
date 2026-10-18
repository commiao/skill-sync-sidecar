# 本机正本切换与回滚 Runbook（cc-switch → sidecar 自有正本）

2026-10-15 在 Mac 上执行过一次（T-0260），本文按那次的实际步骤、检查点和踩过的坑整理。
适用于任何仍以 `~/.cc-switch/skills` 为 sync-daemon `--local-root` 的设备。

## 结果形态

| | 切换前 | 切换后 |
|---|---|---|
| 本机正本（daemon 同步的目录） | `~/.cc-switch/skills` | `~/Library/Application Support/skill-sync-sidecar/skills`（Linux：`$XDG_DATA_HOME/skill-sync-sidecar/skills`） |
| 各工具里的技能 | 实体目录 / 指向 skillshub 或 cc-switch 的软链 | 指向正本的软链（Cursor 为复制品），由 `fanout` 维护 |
| `~/.cc-switch/skills` | 正本 | fanout 的一个目标：全部是指向正本的软链 |
| 正本权限 | 可写 | **只读**。在工具目录里就地改技能会报 `Permission denied`，而不是被 daemon 推到中央 |
| WebDAV 凭据 | `~/.cc-switch/settings.json` | sidecar 配置 + Keychain（`config-init --from-cc-switch`） |

## 前置条件

- sidecar 发布版本含 `local-root-migrate`、`fanout --yes`、`fanout-rollback`、`sync-daemon --fanout`（≥ #19），以及 #26/#29/#30 的三处修复。
- `config-init --from-cc-switch --yes` 已跑过，`remote-status --webdav-config` 能访问中央。
- `ops-status` 为 green、同步状态全部 `unchanged`：**有待推送或冲突时不要切换**。
- 决定 Cursor 用复制还是软链（未验证 Cursor 能加载软链技能前用复制，默认即复制）。

## 步骤

每一步都有检查点；不符合就停下回滚，不要带着疑问继续。

### 0. 备份

```sh
B="$HOME/Library/Application Support/skill-sync-sidecar/cutover-$(date +%Y%m%dT%H%M%S)"
mkdir -p "$B/LaunchAgents"
cp -p ~/Library/LaunchAgents/com.skill-sync-sidecar*.plist "$B/LaunchAgents/"
cp -p "$HOME/Library/Application Support/skill-sync-sidecar"/{config.json,base-record.json,state.json} "$B/"
```

### 1. 停服务（等 daemon 空闲）

```sh
cd ~/.local/share/skill-sync-sidecar/current
until /usr/bin/python3 scripts/sync-daemon-idle.py; do sleep 10; done
for l in com.skill-sync-sidecar com.skill-sync-sidecar.dashboard com.skill-sync-sidecar.operator-executor; do
  launchctl bootout gui/$(id -u)/$l
done
```

检查点：三个进程都已退出。

### 2. 迁移正本

```sh
PYTHONPATH=src python3 -m skill_sync_sidecar local-root-migrate --dry-run   # 全部 unchanged 才继续
PYTHONPATH=src python3 -m skill_sync_sidecar local-root-migrate --yes
```

`--yes` 复制旧根，校验逐技能哈希一致、对同一份中央快照的同步动作一致，都过才写 `config.json` 的 `local_root`；任何不一致会删掉复制品、配置不动。

检查点：`config.json` 出现 `local_root`；新目录里技能数等于旧根。

### 3. 改 daemon 与 dashboard 的 plist

两个 plist 的 `--local-root` 后那一项改成新目录。

> ⚠️ `plutil -replace ProgramArguments.N` 对**数组**是插入，不是替换，而且回读第 N 位看到的是新值，单点核对会误判成功。
> 要先 `-remove` 再 `-insert`，改完用 plistlib 把整组参数与备份逐项比对（个数、每个位置、其余键）。

```sh
f=~/Library/LaunchAgents/com.skill-sync-sidecar.plist
i=$(plutil -extract ProgramArguments json -o - "$f" | python3 -c "import json,sys;print(json.load(sys.stdin).index('--local-root')+1)")
plutil -remove ProgramArguments.$i "$f"
plutil -insert ProgramArguments.$i -string "$HOME/Library/Application Support/skill-sync-sidecar/skills" "$f"
plutil -lint "$f"
```

dashboard 同理。**这一步先不加 `--fanout`。**

检查点：与备份比对，只有那一个路径不同。

### 4. 重新拉起

```sh
for l in com.skill-sync-sidecar com.skill-sync-sidecar.dashboard com.skill-sync-sidecar.operator-executor; do
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/$l.plist
done
```

> `bootout` 后立刻 `bootstrap` 可能报 `Bootstrap failed: 5: Input/output error`（卸载尚未完成）。等几秒重试即可，不是配置错误。

检查点：daemon 首轮 `complete`、summary 全部 `noop`、`uploaded 0`；`ops-status` green 且 `local_root` 是新目录。

### 5. 首次扇出

```sh
PYTHONPATH=src python3 -m skill_sync_sidecar fanout --dry-run      # 把计划给人看
PYTHONPATH=src python3 -m skill_sync_sidecar fanout --yes
```

执行前再出一次计划并与过目的那份比对；有变化先停。

检查点：每个工具 `failed 0`；再跑 `--dry-run` 只剩 `ok` 与 `conflict`；抽查几个工具条目可读；正本已只读（在工具目录里 `touch` 一个文件应被拒）；`~/.cc-switch/skills` 全部变成软链。

### 6. daemon 开启 `--fanout`

```sh
plutil -insert ProgramArguments -string --fanout -append ~/Library/LaunchAgents/com.skill-sync-sidecar.plist
```

与备份比对：只多了末尾的 `--fanout` 和第 3 步的路径。等空闲后 `bootout` + `bootstrap`。

检查点：下一轮 `cycles[-1].fanout.status` 是 `ran` 或 `skipped`，`failed 0`。

### 7. 观察 24 小时

复核：各轮 `fanout.failed` 均 0；没有意外的 `uploaded`；`ops-status` 持续 green；各工具软链仍指向正本。

## 回滚

任何一步都可退回：

1. 已执行第 5/6 步：`fanout-rollback --record <fanout/runs/<run_id>.json> --yes`。它逆序撤销，跳过那次运行之后被改过的条目；在真实数据副本上验证过逐字节还原。
2. 停三个服务，把备份的 plist 和 `config.json`（没有 `local_root`）放回，重新拉起。daemon 回到同步 `~/.cc-switch/skills`。期间若新正本拉到过更新，中央也已有，会照常拉回，不丢。
3. 新正本目录留着无害；要删先解除只读（`chmod -R u+w`）。

## 切换后的变化与坑

- **改技能的流程变了**：正本只读，走「改源码（git 正本）→ 发布/安装」。没有 git 源码的技能：复制出来改 → `local-skill-install`。
- **从只读正本复制出的东西必须恢复可写**（#29）：copytree 连权限一起复制，macOS 上只读目录在同一父目录内改名也会被拒。sidecar 内部已统一处理；自己写脚本从正本复制时同样注意。
- **扇出的「冲突」先看是不是元数据差异**（#27）：`manifest.json`、`.gitignore` 之类不算内容不同。
- **`local-skill-install` 与 `apply` 不能把指向正本的软链换成副本**（#26/#30）：否则每次 fleet-ops 发布都会把工具悄悄移出软链方案。
- **未声明 `targets` 的技能只维护它已在的工具**，不会被加到新工具；要进新工具就在 manifest 里写 `targets`。
- **退役 skillshub 前全盘扫软链**，不只工具目录：2026-10-17 在 `~/.agents/skills`（skills CLI 管理）和 `~/.openclaw/skills` 各发现 47/73 个指向 skillshub 的软链，其中 `lark-note` 在 `~/.agents/skills` 里本身就是软链。处理完 `find ~ -type l` 归零后再改名归档，观察一周再删。
- **装进正本 = 5 分钟内推到共享库**。fleet-ops 发布后自动安装 deploy-standard 就是一次发布。
