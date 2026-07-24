# Local Skill Manager

Local Skill Manager is the user-facing path for managing one skill across the current device's tool roots. It exists so users do not need to hand-write metadata files, copy directories between tools, or understand low-level sync flags.

## Goal

Given a local skill directory or `SKILL.md`, sidecar should:

1. Let the user paste a skill folder or `SKILL.md` path.
2. Detect the package and decide whether it is public, project-level, or device-private.
3. Show the local tools where the skill can be installed.
4. Install it into selected local tool roots with backups after confirmation.
5. Check whether it can be shared without writing WebDAV.
6. Save only the selected skill to the shared library after explicit central save authorization.

The sync unit is the skill package. Sidecar does not modify tool databases, API keys, accounts, provider settings, or runtime caches.

## Dashboard Flow

Open the local dashboard:

```bash
http://127.0.0.1:8765
```

On the first screen, use the top recommendation card first. If it says `现在不用做任何事`, there is no required sync action.

To add or share one local skill, open:

```text
管理本机 skill -> 其他操作和详情 -> 添加或同步 skill
```

Clicking `管理本机 skill` on the top card should open this drawer, scroll to the path input, and focus it. The user should be able to paste a path immediately without hunting through the inventory.

The normal user flow is:

1. Paste a skill directory or `SKILL.md` path.
2. Click `开始`.
3. If the result looks right, click `安装到本机工具`.
4. Optional: click `检查共享` to verify what would be saved.
5. Click `保存到共享库` only when central saving is authorized.

The user should not need to edit metadata, understand technical check output, or manually copy package files. The dashboard should phrase the action in plain language; detailed paths, versions, generated metadata, and raw queues belong in the secondary details views.

The secondary `Skill 清单` view is the per-skill management surface for the current client. It must derive the current device id/name from the local executor (`SKILL_SYNC_DEVICE_ID` / `SKILL_SYNC_DEVICE_NAME`) instead of assuming the operator is always on Mac:

- The top `推荐操作` strip chooses one plain next action from the current state: high-risk sync review, installable local tools, savable local skills, installed local skills, ordinary OpenClaw review, or the full list. Ordinary OpenClaw `source_changed` review must not outrank local install/save work.
- After a recommended action opens the list, the `正在看` strip explains the current view and the next safe operation, such as checking a tool box, saving one skill to the shared library, or leaving project skills in the project repository.
- Search and filters are view-only helpers. Users can filter by skill name/description, shared-library state, scope, local tool, or pending sync state without changing files.
- Each skill row should show a compact current-client tool coverage summary, such as installed tools and installable tools, before the checkbox matrix. Users should not need to decode the full matrix just to know where a skill is already usable.
- Each skill row should expose a plain `下一步` sentence before buttons or checkboxes. The sentence should name the next safe operation, such as ticking a local tool, keeping an installed tool checked, restoring a deprecated skill, or saving to the shared library after `PUBLISH`.
- After a row-level install, remove, or shared-library save is cancelled, completed, or fails, the same row should show a short recent status. This row status is browser-local feedback and should not replace the refreshed shared-library or device state.
- The secondary inventory should also show a compact per-tool coverage overview for the current client, such as `Codex: installed / installable`, before the full list. Clicking a tool overview should filter the list to that tool; it should not perform install/remove by itself.
- The unpublished triage chips split local-only skills into `可保存公用`, `项目级`, `设备私有`, and `缺本机路径`. Clicking a chip only changes the current view.
- Checking a local tool box installs a shared-library skill into that current client's tool root after a read-only check and `INSTALL` confirmation.
- Unchecking an installed local tool box moves the skill out of that current client's tool root into `.skill-sync-removed/<timestamp>/` after a read-only check and `REMOVE` confirmation; it does not delete the backup or change the central snapshot.
- `保存到共享库` appears for unpublished public skills that have a current client source path. It checks first, then requires `PUBLISH`; it does not directly install the skill onto other devices.
- Project-scoped skills are shown in the inventory but are not one-click saved from the global list yet. They need a project-level policy before distribution.
- `标记废弃` updates the shared-library lifecycle to `deprecated` after a read-only check and `DEPRECATE` confirmation. It uploads `index.json` only and keeps existing archives.
- `恢复可用` updates the shared-library lifecycle from `deprecated` back to `published` after a read-only check and `REACTIVATE` confirmation. It uploads `index.json` only and does not auto-install the skill into any tool.

The first dashboard screen should remain a simple "what should I do now" view. Tool matrices, search/filter controls, install/remove buttons, and lifecycle actions belong in the secondary inventory/details view.
The first screen should also state the operation boundary plainly: the current client is directly operable, the shared library is written only after explicit save confirmation, and other devices are read-only status unless their own Agent acts.
The first screen should prefer one primary action. Optional shortcuts on the first card must be folded under `其他选项`; do not place multiple secondary buttons directly in the first visual path. It should not expose raw queues, tool matrices, hash/version details, long explanations, diagnostic counts, or embedded detail drawers as the normal path. Put those details behind `其他操作和详情`, `查看确认清单`, `Skill 清单`, or advanced sections. A non-technical user should be able to operate the first card without reading this document.
The first card may show compact facts such as current-client skill count, shared-library count, and pending count. It should not put ordinary OpenClaw review dry-run/publish buttons on the first card unless that is the only remaining safe action; ordinary review details belong behind `查看待审详情` or the confirmation list.
`/api/overview` may expose compact inventory counts for portals and status cards, including per-tool and per-device installed counts, but the full per-skill `items[]` list belongs in `/api/status` and the secondary `Skill 清单` view.
Clicking a first-screen `查看确认清单` or `看看要处理什么` action should open every parent drawer needed to make the review queue visible, then scroll directly to that queue.
Clicking permission/setup actions such as `开启保存权限` or `开启本机写入` should open the technical setup area only. It must not focus the local skill path input; that input is reserved for the explicit `管理本机 skill` workflow.

When OpenClaw is actively changing a skill, the first screen may show `暂时搁置`.
The first screen must say this is ordinary review work, not a service failure. The primary action should let the user continue local skill management, and `先不提醒` should be available as a secondary way to hide the reminder while editing is still in progress.
This is a browser-local UI deferral only:

- It is stored in `localStorage` for the current browser.
- It does not write WebDAV, OpenClaw, current-client tool roots, or central metadata.
- It is tied to the current source version, so a new OpenClaw edit shows up again.
- The original item stays visible in the secondary confirmation list.
- Use it when a skill is still being edited and the dashboard should not block other local management work.

## Permission Model

Local actions and central writes are separate permissions:

```text
--allow-local-writes
```

Allows the executor to write into local tool skill roots after the dashboard confirmation prompt.

```text
--allow-publish
```

Allows the executor to upload the selected skill and merged `index.json` to the WebDAV central snapshot after the dashboard confirmation prompt. The internal flag keeps the historical `publish` name, but the dashboard presents this as `保存到共享库`.

The launchd helper enables local installs by default and keeps central shared-library saving disabled unless explicitly requested:

```bash
scripts/install-operator-executor-launchd.sh

SKILL_SYNC_EXECUTOR_ALLOW_PUBLISH=1 \
  scripts/install-operator-executor-launchd.sh
```

For non-default clients, set the displayed device identity before installing the helper:

```bash
SKILL_SYNC_DEVICE_ID=win \
SKILL_SYNC_DEVICE_NAME="Windows 笔记本" \
  scripts/install-operator-executor-launchd.sh
```

When shared-library saving is disabled, the dashboard still allows `检查共享` and disables `保存到共享库` with an explanation.

## CLI Equivalents

Start/check a local skill:

```bash
python3 -m skill_sync_sidecar local-skill-analyze \
  --path /Users/mac/.codex/skills/read-wechat-article
```

Install to local tools:

```bash
python3 -m skill_sync_sidecar local-skill-install \
  --path /Users/mac/.codex/skills/read-wechat-article \
  --dry-run
```

Check shared-library save:

```bash
python3 -m skill_sync_sidecar local-skill-publish \
  --path /Users/mac/.codex/skills/read-wechat-article \
  --cc-switch-webdav \
  --prefix skill-sync-sidecar-dev/current-mac \
  --dry-run
```

Real central save requires `--yes` and explicit authorization to upload private skill content to WebDAV.

## Acceptance Case: read-wechat-article

Current validated local result:

```text
skill_id=read-wechat-article
scope=global
targets=codex, cc-switch, skillshub, cursor, claude-code, openclaw
risk=ok
local tools=noop: 5
will_write=0
```

Installed local roots:

```text
/Users/mac/.codex/skills/read-wechat-article
/Users/mac/.cc-switch/skills/read-wechat-article
/Users/mac/.skillshub/read-wechat-article
/Users/mac/.cursor/skills-cursor/read-wechat-article
/Users/mac/.claude/skills/read-wechat-article
```

Each root contains `SKILL.md` and `manifest.json`.

Central save result:

```text
safe_to_push=true
initial_plan_action=push_new
published_snapshot=local-skill-publish-20260722T132650.736038Z
snapshot_total=104
post_publish_plan_action=noop
uploaded_files_after_publish=0
local_hash=6292e4d420bde89a710a90f13ed15562a4f36c172015f2b6cc4c82efafe17485
remote_hash=6292e4d420bde89a710a90f13ed15562a4f36c172015f2b6cc4c82efafe17485
```

After explicit data-export approval, `read-wechat-article` was saved to the WebDAV central snapshot. A follow-up save dry-run now returns `noop`, which means the central snapshot already matches the local skill content hash.

## Safety Rules

- Generated files such as `__pycache__`, `*.pyc`, and `.DS_Store` are excluded.
- Secret-like files are detected and block automatic handling.
- Existing target skills are backed up before replacement.
- Local removals are moved to `.skill-sync-removed/<timestamp>/`, not permanently deleted.
- Existing identical skills with missing metadata only receive `manifest.json`; they are not replaced.
- Selective save merges only the selected skill into the central snapshot and leaves unrelated conflicts untouched.
- Central deprecation changes lifecycle metadata only; it does not delete WebDAV archives or uninstall devices.
- Central reactivation changes lifecycle metadata only; it does not rewrite WebDAV archives or install devices.
