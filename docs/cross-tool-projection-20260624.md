# Cross-Tool Projection Baseline - 2026-06-24

## Goal

Explain and measure why local tool skill counts differ after the Mac/OpenClaw/WebDAV canonical sync has converged.

The canonical snapshot is the WebDAV-backed sidecar store:

- Snapshot: `approved-push-20260624T092058.434127Z`
- Canonical skills: `94`
- Mac/OpenClaw device sync: green, blocked `0`

Tool directories are projections, not the canonical store. A tool can have fewer or more visible skills because each tool has its own discovery root, target metadata, scope support, and local-only skills.

## Added Capability

`skill-sync tool-projection` is a read-only preflight. It compares a canonical snapshot against local tool roots and classifies each canonical skill per tool:

- `installed`: tool root has the canonical content hash
- `missing`: manifest targets the tool and scope is supported, but the tool root lacks the skill
- `drift`: tool root has the skill id with a different content hash
- `unsupported_scope`: manifest targets the tool, but the tool root cannot safely install that scope
- `not_targeted`: manifest does not target the tool
- `blocked_error`: the canonical skill has validation errors

This does not write to any tool directory.

`skill-sync apply` now also supports explicit global tool adapters:

- `skillshub-global`
- `codex-global`
- `cursor-global`
- `claude-code-global`

These adapters only install `scope=global` packages whose manifest `targets` include the target tool. Real writes to those tool roots require both an explicit `--target-root` and at least one `--skill-id` allowlist entry.

## Initial Read-Only Projection Result

```text
cc-switch: installed=94 targeted=92 missing=0 drift=0 not_targeted=2 unsupported_scope=0 blocked_error=0 extra_local=0
skillshub: installed=87 targeted=92 missing=33 drift=4 not_targeted=2 unsupported_scope=0 blocked_error=0 extra_local=28
Codex: installed=28 targeted=94 missing=92 drift=0 not_targeted=0 unsupported_scope=2 blocked_error=0 extra_local=28
Cursor: installed=18 targeted=2 missing=0 drift=0 not_targeted=92 unsupported_scope=2 blocked_error=0 extra_local=17
Claude Code: installed=1 targeted=0 missing=0 drift=0 not_targeted=94 unsupported_scope=0 blocked_error=0 extra_local=1
```

After the first Codex allowlist apply:

```text
command=skill-sync apply --target codex-global --target-root ~/.codex/skills --skill-id hackernews-frontpage --yes
applied=1
record=~/.codex/skills/.skill-sync-backups/20260624-095756-114277/.apply-record.json
Codex: installed=29 targeted=94 missing=91 drift=0 not_targeted=0 unsupported_scope=2 blocked_error=0 extra_local=28
```

The first skillshub allowlist attempt included `smart-reporter` and `task-complete-summary`, but local scanning showed portability warnings after install:

```text
smart-reporter: external OpenClaw absolute paths
task-complete-summary: external OpenClaw adapter path and missing scripts/task-archive.sh
```

That batch was rolled back with its apply record. The retained skillshub batch installed only staged packages that scanned as `ok`:

```text
command=skill-sync apply --target skillshub-global --target-root ~/.skillshub \
  --skill-id calendar \
  --skill-id searxng \
  --skill-id skill-finder-cn \
  --skill-id task \
  --skill-id trigger-manager \
  --yes
applied=5
record=~/.skillshub/.skill-sync-backups/20260624-115822-958474/.apply-record.json
skillshub: installed=92 targeted=92 missing=28 drift=4 not_targeted=2 unsupported_scope=0 blocked_error=0 extra_local=28
```

## Interpretation

- `cc-switch` is the governed mixed-scope root and is aligned with the canonical snapshot.
- `skillshub` is partially overlapping: it has many local extras plus canonical misses/drift.
- `Codex` is heavily missing canonical global skills, but also has local extras.
- `Cursor` and `Claude Code` are mostly not targeted by manifest metadata yet, so copying all skills there would be unsafe.
- Project-scoped skills must not be blindly installed into global tool roots.

## Next Safe Step

Do not bulk-copy the `94` canonical skills into every tool. The safe sequence is:

1. Review manifest `targets` for high-value global skills.
2. Add tool-specific apply adapters that consume the projection plan.
3. Start with dry-run only, then apply a small allowlist.
4. Keep project-scoped skills out of global roots unless the target is an explicit project adapter.

## Same-Session Sync Governance Check

During this projection work, OpenClaw produced one new pull-only blocked item:

```text
tianjin-recruitment push
reason=writer policy pull-only blocks push
```

The standard approved-push path was used without changing the unattended OpenClaw policy:

```text
dry_run=True approved=1 skill=tianjin-recruitment
dry_run=False approved=1 skill=tianjin-recruitment uploaded_files=2
snapshot=approved-push-20260624T092058.434127Z
```

After refreshing OpenClaw cache and rebuilding the blocked report:

```text
dashboard_health=green
dashboard_blocked=0
mac_snapshot=approved-push-20260624T092058.434127Z
openclaw_snapshot=approved-push-20260624T092058.434127Z
```

## Skillshub Import Diagnosis

The `skillshub` UI can report external skills as import candidates while the
actual import path rejects them as already present in the Hub. The sidecar now
has a read-only diagnosis command for that mismatch:

```text
command=skill-sync hub-import-diagnosis
hub=/Users/mac/.skillshub
hub_total=94
source_total=162
already_in_hub=125
update_available=7
importable=30
```

The `lark-*` series is not missing from Hub on this Mac. The diagnosis classifies
those entries as:

```text
agents/lark-contact already_in_hub
reason=same skill_id and content_hash already exist in Hub
```

So the root cause is a discovery/import semantic mismatch: discovery sees the
same skill IDs in external roots such as `~/.agents/skills`, while import then
correctly refuses to create a duplicate under `~/.skillshub`.

The dashboard also exposes this under `dashboard.hub_import` and renders a
`skillshub 导入诊断` panel. The panel now uses operator-facing labels and
prioritizes actionable rows first:

- `可导入`: skill ID is not present in Hub.
- `可更新`: same skill ID exists in Hub but the content hash differs.
- `已在 Hub / 无需导入`: same skill already exists in Hub.

The CLI JSON keeps the raw `status` values for automation and also includes
`status_label`, `operator_action`, and `reason_label` for UI or report rendering.

It also includes an `action_plan` in `dry_run` mode. The plan separates:

- `preview_import`: Hub does not contain the skill and a single external source
  can be staged as an import candidate.
- `review_duplicate_import`: Hub does not contain the skill, but multiple
  external roots provide the same skill ID; an operator must choose the source.
- `review_update`: Hub contains the same skill ID with different content; inspect
  the diff before replacing anything.
- `skip_existing`: Hub already has the same skill; no import is needed.

The action plan is intentionally non-writing (`writes_files=false` and
`safe_to_apply_automatically=false`) until an explicit apply flow exists.

For an auditable package, write the same plan to disk:

```text
command=skill-sync hub-import-preview --out /tmp/skillshub-import-preview
```

The command writes:

- `preview.json`: machine-readable dry-run package with non-skip actions,
  source file hashes, target paths, and review flags.
- `preview.md`: operator-readable summary with `SKILL.md` diffs for update
  candidates.

This preview package still does not write to `~/.skillshub`; it is the handoff
artifact before an explicit apply command exists.

The first apply layer is intentionally narrow:

```text
command=skill-sync hub-import-apply --preview /tmp/skillshub-import-preview/preview.json
```

Without `--yes`, it only prints an apply plan. With `--yes`, it imports only
`preview_import` actions whose target path is still absent under the Hub root and
whose source hash has not changed since preview generation:

```text
command=skill-sync hub-import-apply --preview /tmp/skillshub-import-preview/preview.json --yes --out /tmp/skillshub-import-apply
```

`review_update` and `review_duplicate_import` remain blocked by design. This
keeps the first writable path limited to new Hub skills and prevents accidental
overwrites.

The dashboard exposes the safe half of this flow under `skillshub 导入诊断`.
Clicking `生成预览包` calls `POST /api/hub-import-preview`, writes a timestamped
preview package under the sidecar work directory, and displays the corresponding
`hub-import-apply` dry-run result. The dashboard does not expose a writing
`--yes` action yet.
