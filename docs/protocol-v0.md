# Skill Sync Protocol v0

## Goal

Use WebDAV as the storage and exchange layer for private agent skills. The protocol adds skill semantics, validation, conflict handling, and tool adapters above generic file storage.

## Non-Goals

- Do not become a generic file sync system.
- Do not sync API keys, auth tokens, local provider settings, or tool account state.
- Do not require cc-switch, skillshub, or Codex to change their internal sync logic.
- Do not auto-install risky skills.

## Remote Layout

```text
cc-skill-sync/
  protocol.json
  devices/
    <device-id>.json
  skills/
    <skill-id>/
      manifest.json
      latest.json
      revisions/
        <revision-id>.zip
  conflicts/
    <skill-id>/
      <timestamp>-<device-id>.zip
  snapshots/
    <snapshot-id>/
      index.json
      skills/
        <source>/
          <skill-id>/
            <content-hash>.zip
      audit.json
```

## Canonical Skill Manifest

See `docs/manifest-v0.md` for the full field contract. The sync index embeds the same canonical fields so remote consumers can reason about project scope and adapter targets without extracting every archive.

```json
{
  "protocol_version": 0,
  "skill_id": "pua",
  "name": "pua",
  "description": "Forces high-agency exhaustive problem-solving...",
  "scope": "global",
  "targets": ["cc-switch", "skillshub", "codex", "openclaw"],
  "exclude": ["__pycache__", "*.pyc"],
  "content_hash": "sha256:...",
  "revision_id": "20260610T080000Z-<hash>",
  "base_revision_id": "20260610T070000Z-<hash>",
  "source": {
    "device_id": "macbook-pro",
    "tool": "cc-switch",
    "path_hint": "~/.cc-switch/skills/pua"
  },
  "targets": ["cc-switch", "skillshub", "codex", "openclaw"],
  "risk": {
    "level": "ok",
    "issues": []
  },
  "files": [
    {
      "path": "SKILL.md",
      "size": 1024,
      "sha256": "..."
    }
  ]
}
```

## Conflict Rules

The protocol is multi-writer. Mac, Windows, and OpenClaw are peers.

1. Local unchanged, remote changed: pull is safe.
2. Local changed, remote unchanged: push is safe.
3. Local and remote changed from the same base revision: create a conflict package and stop.
4. Unknown base revision: stop and require explicit reconciliation.
5. Any high-risk validation issue: store in quarantine/conflict state; do not auto-apply.

## Validation Gates

Every push and apply must run validation first:

- `SKILL.md` exists and is UTF-8.
- Front matter or `manifest.json` should include `name` and `description`.
- `manifest.json.scope`, when present, must be `global` or `project`.
- Generated and bulky directories are excluded.
- Secret-like files are excluded by default, including `.encryption-key`, `.env*`, private key files, and common package-manager credential files.
- Absolute symlink paths are normalized or rejected.
- Risky shell patterns are flagged.
- Large skills or very high file counts require confirmation.

## Local Apply Rules

Apply is the only command that writes into tool directories.

1. Stage extracted files in a sidecar-owned staging directory.
2. Verify archive member paths, per-file sha256 values, and content hash.
3. Create a full backup of the target skill directory or database state.
4. Install through a tool adapter.
5. Verify the target tool can discover the skill.
6. Record the applied remote revision locally.

## WebDAV Semantics

WebDAV is treated as object storage. Clients should use atomic-ish writes where possible:

1. Upload to a temporary path.
2. Verify size and hash.
3. Move/copy into the final revision path.
4. Update `latest.json` last.

When WebDAV servers do not provide strong atomic operations, clients must prefer append-only revision objects and treat `latest.json` as a pointer that can be retried.

## MVP Commands

```bash
skill-sync snapshot --out ./snapshot-preview
skill-sync remote-status --remote https://example.com/dav/cc-skill-sync
skill-sync push --snapshot-dir ./snapshot-preview --remote https://example.com/dav/cc-skill-sync
skill-sync push --snapshot-dir ./snapshot-preview --remote https://example.com/dav/cc-skill-sync --yes
skill-sync pull-cache --remote https://example.com/dav/cc-skill-sync --out ./cache-preview
skill-sync diff --left ./cache-preview --right ./snapshot-preview
skill-sync stage --snapshot-dir ./cache-preview --out ./staging-preview --clean
skill-sync apply --staged-dir ./staging-preview/<snapshot-id> --target cc-switch-global --dry-run
skill-sync apply --staged-dir ./staging-preview/<snapshot-id> --target cc-switch-global --target-root /tmp/skill-sync-target --yes
skill-sync sync-status --local-root /tmp/skill-sync-target --remote-snapshot ./cache-preview --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json --fail-on-conflict
skill-sync sync-plan --local-root /tmp/skill-sync-target --remote-snapshot ./cache-preview --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json --fail-on-blocked
skill-sync conflict-package --local-root /tmp/skill-sync-target --remote-snapshot ./cache-preview --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json --out ./conflicts
skill-sync tombstone --local-root /tmp/skill-sync-target --remote-snapshot ./cache-preview --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json --out ./tombstones
skill-sync blocked-report --local-root /tmp/skill-sync-target --remote-snapshot ./cache-preview --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json --writer-policy pull-only --out ./blocked-report
skill-sync sync-apply --local-root /tmp/skill-sync-target --remote-snapshot ./cache-preview --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json --dry-run
skill-sync sync-apply --local-root /tmp/skill-sync-target --remote-snapshot ./cache-preview --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json --yes
skill-sync sync-cycle --local-root /tmp/skill-sync-target --remote https://example.com/dav/cc-skill-sync --prefix snapshots/current --cache-dir ./cache-preview --work-dir ./sync-work --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json --dry-run
skill-sync sync-daemon --local-root /tmp/skill-sync-target --remote https://example.com/dav/cc-skill-sync --prefix snapshots/current --cache-dir ./cache-preview --work-dir ./sync-work --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json --dry-run --max-cycles 1
skill-sync rollback --record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json --yes
```

`push` without `--yes` is a dry-run. `pull-cache`, `stage`, and `apply --dry-run` never write into cc-switch, skillshub, Codex, or OpenClaw directories. `apply --yes` writes only to an explicit target root for `cc-switch-global` and emits rollback metadata before returning success.

`sync-status` is the gate before automatic sync. It compares the installed local root, downloaded remote snapshot, and the last `.apply-record.json` base. Only `pull` and `push` are one-sided changes. `conflict` means local and remote both changed away from base differently and must not be auto-applied.

`sync-plan` converts that status into a dry-run plan. The default policy allows one-sided `pull` and `push`, blocks `conflict`, and also blocks new skills or deletions unless `--allow-new` or `--allow-delete` is explicitly set.

`conflict-package` is the handoff path for blocked conflicts. For each conflicting skill it writes a package containing `metadata.json`, current `local/` files, staged and hash-verified `remote/` files, and `base.json` with the common ancestor hash and source record. It does not modify local installs or remote storage.

`tombstone` is the handoff path for one-sided deletes. For `local_deleted` it records a pending `delete_remote`; for `remote_deleted` it records a pending `delete_local`. Tombstones copy the surviving side's files plus base metadata, but do not execute deletion. Actual delete propagation requires a later explicit retention/rollback gate.

`blocked-report` is the handoff path for blocked sync-plan items, especially writer-policy blocks. It writes `blocked-report.json` and `blocked-report.md` with the blocked skill id, status action, plan action, local/remote/base hashes, category, and recommended next step. It does not copy skill contents or apply changes.

`sync-apply` executes the safe subset of that plan. It is still dry-run by default. With `--yes`, remote-to-local `pull` and `pull_new` actions stage the remote cache, verify archive hashes, install into the explicit local root, and write rollback metadata. Local-to-remote `push` and `push_new` actions require a remote destination; before upload, the client verifies that the remote's current index still matches the local cache. If the remote drifted, the push is refused and the user must run `pull-cache` and re-plan.

For project-scoped skills, `sync-apply --target codex-project --project-root <repo>` installs only `scope=project` packages into `<repo>/skills/<skill-id>` and writes rollback metadata under `<repo>/.skill-sync-backups`. Global skills are refused for project targets, and project skills are refused for global targets.

After a successful push, the sidecar writes `.skill-sync-bases/<sync-id>.json` under the local root. That record contains the new local/remote hashes and should be passed as the next `--last-applied-record`, preventing stale-base false conflicts after local changes have been published. `sync-apply` still refuses conflicts, deletions, and scope-target mismatches.

`sync-cycle` composes the manual commands into one automation-safe pass:

1. Download remote `index.json` and archives into `--cache-dir`.
2. Build `sync-status` and `sync-plan` against `--last-applied-record`.
3. Materialize conflict packages under `--work-dir/conflicts` when local and remote both changed.
4. Materialize tombstones under `--work-dir/tombstones` for one-sided deletes.
5. Materialize blocked review reports under `--work-dir/blocked-report` when the plan has blocked items.
6. With `--dry-run`, stop before any apply or upload.
7. With `--yes`, execute only non-blocked pull/push actions through `sync-apply`.

`sync-cycle --yes` does not execute deletions. Even with `--allow-delete`, deletes are represented as tombstones and the cycle reports `blocked` until a later explicit delete gate exists. This keeps automatic sync safe for peer devices where Mac, Windows, and OpenClaw can all be writers.

`sync-daemon` is repeated `sync-cycle` with a sleep interval. The safety model is unchanged:

- It requires explicit `--dry-run` or `--yes`.
- It stops on blocked cycles by default.
- It can be bounded with `--max-cycles` for smoke tests and supervisor health checks.
- `--continue-on-blocked` is available only for monitoring scenarios where repeated blocked reports are desired.

When reusing cc-switch WebDAV settings, use:

```bash
skill-sync push --snapshot-dir ./snapshot-preview --cc-switch-webdav --prefix skill-sync-sidecar-dev/test
```

HTTP uploads require a non-empty `--prefix`, and the official `cc-switch-sync` prefix is protected from sidecar writes.

## Canary Workflow

For first contact with a WebDAV endpoint, upload a zero-skill canary snapshot before transferring real skills:

1. Generate a snapshot from a non-existent root so the index contains `total: 0`.
2. Upload it to `skill-sync-sidecar-dev/canary-<timestamp>`.
3. Read it with `remote-status`.
4. Pull it into a cache directory.

This validates auth, MKCOL/PUT/GET behavior, prefix handling, and cache download without exposing local skill content.

## Acceptance Cases

### Case 1: cc-switch Global Snapshot

- Source: `~/.cc-switch/skills`
- Expected scan result on the current Mac acceptance machine: 91 discoverable `SKILL.md` packages
- Scope: mostly `global`
- Remote prefix: `skill-sync-sidecar-dev/20260610-120716`
- Expected round trip: 91 archives, 0 changed after diff

### Case 2: libtv-m-forward Project Skill

- Source layout: `libtv-m/skills/libtv-m-forward/`
- Scope: `project`
- Contents: `SKILL.md` plus Python scripts
- External integration: scripts may call `lark-cli` using caller OAuth
- Required behavior: scan and package as a project skill, preserve scripts, exclude generated files, and do not install globally by default
