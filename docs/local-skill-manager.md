# Local Skill Manager

Local Skill Manager is the user-facing path for managing one skill across the current device's tool roots. It exists so users do not need to hand-write `manifest.json`, copy directories between tools, or understand low-level sync flags.

## Goal

Given a local skill directory or `SKILL.md`, sidecar should:

1. Detect the skill package.
2. Infer scope, targets, metadata, and risk.
3. Generate sidecar manifest metadata when missing.
4. Install the skill into selected local tool roots with backups.
5. Preview publishing the installed skill to the WebDAV central snapshot.
6. Publish only the selected skill after explicit central publish authorization.

The sync unit is the skill package. Sidecar does not modify tool databases, API keys, accounts, provider settings, or runtime caches.

## Dashboard Flow

Open the local dashboard:

```bash
http://127.0.0.1:8765
```

Use:

```text
本地 Skill 工作区 -> 导入本地 Skill
```

The normal user flow is:

1. Paste a skill directory or `SKILL.md` path.
2. Click `分析`.
3. Review the summary and target tools.
4. Click `安装到本机工具`.
5. Click `预检发布`.
6. Click `发布中央` only when central publishing is authorized.

The user should not need to edit manifest metadata or manually copy package files.

The secondary `Skill 清单` view is the per-skill management surface for the current client:

- Search and filters are view-only helpers. Users can filter by skill name/description, central lifecycle state, scope, local tool, or pending sync state without changing files.
- The unpublished triage chips split local-only skills into `可发布公用`, `项目级`, `设备私有`, and `缺本机路径`. Clicking a chip only changes the current view.
- Checking a local tool box installs a central published skill into that Mac tool root after dry-run and `INSTALL` confirmation.
- Unchecking an installed local tool box moves the skill out of that Mac tool root into `.skill-sync-removed/<timestamp>/` after dry-run and `REMOVE` confirmation; it does not delete the backup or change the central snapshot.
- `发布中央仓库` appears for unpublished public skills that have a current Mac source path. It runs a dry-run first, then requires `PUBLISH`; it does not install the skill onto other devices.
- Project-scoped skills are shown in the inventory but are not one-click published from the global list yet. They need a project-level policy before distribution.
- `标记废弃` updates the central snapshot lifecycle to `deprecated` after dry-run and `DEPRECATE` confirmation. It uploads `index.json` only and keeps existing archives.
- `恢复发布` updates the central snapshot lifecycle from `deprecated` back to `published` after dry-run and `REACTIVATE` confirmation. It uploads `index.json` only and does not auto-install the skill into any tool.

The first dashboard screen should remain a simple "what should I do now" view. Tool matrices, search/filter controls, install/remove buttons, and lifecycle actions belong in the secondary inventory/details view.

When OpenClaw is actively changing a skill, the first screen may show `暂时搁置`.
This is a browser-local UI deferral only:

- It is stored in `localStorage` for the current browser.
- It does not write WebDAV, OpenClaw, Mac tool roots, or central metadata.
- It is tied to the current source hash, so a new OpenClaw edit shows up again.
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

Allows the executor to upload the selected skill and merged `index.json` to the WebDAV central snapshot after the dashboard confirmation prompt.

The launchd helper enables local installs by default and keeps central publishing disabled unless explicitly requested:

```bash
scripts/install-operator-executor-launchd.sh

SKILL_SYNC_EXECUTOR_ALLOW_PUBLISH=1 \
  scripts/install-operator-executor-launchd.sh
```

When publishing is disabled, the dashboard still allows `预检发布` and disables `发布中央` with an explanation.

## CLI Equivalents

Analyze:

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

Preview central publish:

```bash
python3 -m skill_sync_sidecar local-skill-publish \
  --path /Users/mac/.codex/skills/read-wechat-article \
  --cc-switch-webdav \
  --prefix skill-sync-sidecar-dev/current-mac \
  --dry-run
```

Real central publish requires `--yes` and explicit authorization to upload private skill content to WebDAV.

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

Central publish result:

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

After explicit data-export approval, `read-wechat-article` was published to the WebDAV central snapshot. A follow-up publish dry-run now returns `noop`, which means the central snapshot already matches the local skill content hash.

## Safety Rules

- Generated files such as `__pycache__`, `*.pyc`, and `.DS_Store` are excluded.
- Secret-like files are detected and block automatic handling.
- Existing target skills are backed up before replacement.
- Local removals are moved to `.skill-sync-removed/<timestamp>/`, not permanently deleted.
- Existing identical skills with missing metadata only receive `manifest.json`; they are not replaced.
- Selective publish merges only the selected skill into the central snapshot and leaves unrelated conflicts untouched.
- Central deprecation changes lifecycle metadata only; it does not delete WebDAV archives or uninstall devices.
- Central reactivation changes lifecycle metadata only; it does not rewrite WebDAV archives or install devices.
