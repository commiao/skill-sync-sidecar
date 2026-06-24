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
