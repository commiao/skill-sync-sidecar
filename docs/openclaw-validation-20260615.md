# OpenClaw Validation - 2026-06-15

Validation target: OpenClaw server `root@oc-vps-aliyun-us`.

Sidecar version under validation: `v0.1.2`.

## Scope

This validation was intentionally read-only for OpenClaw:

- No writes to `/home/admin/clawd/skills`.
- No daemon install or service restart.
- No Python/runtime installation.
- No container image pull.
- Local writes were limited to `/private/tmp/openclaw-skill-sync-validate/...`.

This was necessary because OpenClaw skill optimization was happening in parallel.

## OpenClaw Preflight

- SSH reachable over the configured Tailscale route.
- Host: `iZ0xi1l67zzk3qgaail2zvZ`.
- User: `root`.
- OpenClaw skill root exists: `/home/admin/clawd/skills`.
- OpenClaw skill count: `32`.
- cc-switch WebDAV settings exist for admin: `/home/admin/.cc-switch/settings.json`.
- System Python: `Python 3.6.8`.
- No Python `3.9+` binary was found under `/usr`, `/opt`, or `/home`.
- No existing Docker or Podman images were present for an isolated Python `3.9+` smoke test.

## Reconcile Result

Report path:

```text
/private/tmp/openclaw-skill-sync-validate/reconcile-20260615-openclaw-live-v012/reconcile/reconcile-report.json
```

Remote snapshot:

```text
/Users/mac/public-sync/skill-sync-sidecar-dev/current-mac
```

Summary:

- Local OpenClaw skills: `32`
- Remote snapshot skills: `92`
- `safe_to_auto_apply`: `false`
- `conflict`: `8`
- `remote_new`: `60`
- `same_without_base`: `24`
- `changed_since_previous`: `8`

OpenClaw gate blockers:

- `safe_to_auto_apply=false`
- `conflict=8`
- `changed_since_previous=8`

## Active Conflicts

These OpenClaw skills changed since the previous inventory and conflict with the current remote snapshot:

- `beijing-recruitment`: `skill_md_only`, changed `SKILL.md`
- `daily-report`: `mixed_with_code`, changed `DESIGN.md`, `config/report-subscriptions.yaml`
- `lark-cli-adapter`: `code_or_config`, changed `lib/lark_cli_adapter.py`
- `puter-image-gen`: `code_or_config`, changed image/Feishu scripts
- `role-maintainer`: `docs_only`, changed `USAGE.md`
- `session-knowledge-manager`: `code_or_config`, changed `src/main.js`, `src/triggers/keyword-trigger.js`
- `session-lifetime-manager`: `mixed_with_code`, changed docs and executor/dispatcher code
- `smart-reporter`: `mixed_with_code`, changed `SKILL.md`, `main.py`

## Decision

Do not apply or install the sidecar daemon on OpenClaw while this gate is red.

The safe next step is to let the OpenClaw skill optimization finish, then run a new read-only reconcile. If the same 8 skills are intentional, adopt or merge them into the canonical WebDAV snapshot from a controlled writer before enabling OpenClaw pull/apply behavior.

## Next Safe Validation

After OpenClaw skill edits settle:

```bash
REMOTE_CACHE=/Users/mac/public-sync/skill-sync-sidecar-dev/current-mac \
PREVIOUS_INVENTORY=/private/tmp/openclaw-skill-sync-validate/reconcile-20260615-openclaw-live-v012/openclaw-inventory.json \
  scripts/openclaw-reconcile-readonly.sh \
  /private/tmp/openclaw-skill-sync-validate/reconcile-$(date +%Y%m%d%H%M%S)
```

Only consider supervised OpenClaw rollout when:

- `conflict=0`
- `changed_since_previous=0`
- unreviewed `local_new=0`
- OpenClaw has a Python `3.9+` runtime, or an approved isolated runtime strategy exists
