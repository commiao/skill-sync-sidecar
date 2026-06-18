# Skill Sync Sidecar

Skill Sync Sidecar is a WebDAV-backed tool for scanning, validating, staging, and syncing agent skills across local tools such as cc-switch, skillshub, Codex, and OpenClaw.

The MVP keeps writes behind explicit confirmation flags:

- `scan`: discover local `SKILL.md` packages and emit normalized records.
- `status`: summarize counts, sources, risks, and duplicate skill ids.
- `ops-status`: summarize daemon state, base record, remote snapshot, sync plan, and optional OpenClaw reconcile state.
- `openclaw-gate`: evaluate the latest read-only OpenClaw reconcile report before any peer-writer apply.
- `doctor`: validate skill metadata, size, file count, symlinks, and risky shell patterns.
- `snapshot`: write a local WebDAV-ready snapshot directory with `index.json` and per-skill zip archives.
- `remote-status`: read remote snapshot metadata.
- `push`: upload a local snapshot to a remote; dry-run unless `--yes` is provided.
- `pull-cache`: download a remote snapshot into a local cache without applying it.
- `diff`: compare two local snapshot directories.
- `stage`: safely extract a snapshot/cache into a staging directory without installing it.
- `apply --dry-run`: plan installation targets, backup paths, and scope-based skips without writing files.
- `apply --yes`: install allowed staged skills into an explicit target root and write rollback metadata.
- `rollback --yes`: restore a previous apply from its `.apply-record.json`.
- `sync-status`: compare local, remote, and last-applied hashes before deciding push, pull, or conflict.
- `sync-plan`: convert sync status into a dry-run plan that blocks conflicts by default.
- `sync-apply`: execute safe pull and push actions from the plan; dry-run unless `--yes` is provided.
- `conflict-package`: materialize local, remote, and base metadata for conflicts without applying changes.
- `tombstone`: materialize non-destructive delete records for one-sided deletes without deleting files.
- `sync-cycle`: run one safe remote download, status, plan, review-material, and optional apply cycle.
- `sync-daemon`: run repeated `sync-cycle` passes with explicit dry-run/yes mode and blocked-state stop behavior.

It does not modify tool databases. WebDAV uploads require `push --yes`, and local installs require `apply --yes`, `sync-apply --yes`, or `sync-cycle --yes`.

## Install

From a checked-out repository:

```bash
python3 -m pip install .
skill-sync --version
```

From the GitHub repository:

```bash
python3 -m pip install "git+https://github.com/commiao/skill-sync-sidecar.git"
```

For release artifacts, use the wheel attached to the GitHub Release when available.

## Quick Start

```bash
python3 -m skill_sync_sidecar status
python3 -m skill_sync_sidecar ops-status --allow-new
python3 -m skill_sync_sidecar openclaw-gate --fail-on-blocked
python3 -m skill_sync_sidecar openclaw-gate --require-complete --fail-on-blocked
python3 -m skill_sync_sidecar scan --json
python3 -m skill_sync_sidecar doctor
python3 -m skill_sync_sidecar snapshot --out ./snapshot-preview
python3 -m skill_sync_sidecar push --snapshot-dir ./snapshot-preview --remote file:///tmp/skill-sync-remote
python3 -m skill_sync_sidecar push --snapshot-dir ./snapshot-preview --remote file:///tmp/skill-sync-remote --yes
python3 -m skill_sync_sidecar pull-cache --remote file:///tmp/skill-sync-remote --out ./cache-preview
python3 -m skill_sync_sidecar diff --left ./cache-preview --right ./snapshot-preview
python3 -m skill_sync_sidecar stage --snapshot-dir ./cache-preview --out ./staging-preview --clean
python3 -m skill_sync_sidecar apply --staged-dir ./staging-preview/<snapshot-id> --target cc-switch-global --dry-run
python3 -m skill_sync_sidecar apply --staged-dir ./staging-preview/<snapshot-id> --target cc-switch-global --target-root /tmp/skill-sync-target --yes
python3 -m skill_sync_sidecar sync-status --local-root /tmp/skill-sync-target --remote-snapshot ./cache-preview --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json
python3 -m skill_sync_sidecar sync-plan --local-root /tmp/skill-sync-target --remote-snapshot ./cache-preview --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json --fail-on-blocked
python3 -m skill_sync_sidecar conflict-package --local-root /tmp/skill-sync-target --remote-snapshot ./cache-preview --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json --out ./conflicts
python3 -m skill_sync_sidecar tombstone --local-root /tmp/skill-sync-target --remote-snapshot ./cache-preview --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json --out ./tombstones
python3 -m skill_sync_sidecar sync-apply --local-root /tmp/skill-sync-target --remote-snapshot ./cache-preview --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json --dry-run
python3 -m skill_sync_sidecar sync-apply --local-root /tmp/skill-sync-target --remote-snapshot ./cache-preview --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json --yes
python3 -m skill_sync_sidecar sync-cycle --local-root /tmp/skill-sync-target --remote file:///tmp/skill-sync-remote --prefix snapshots/current --cache-dir ./cache-preview --work-dir ./sync-work --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json --dry-run
python3 -m skill_sync_sidecar sync-daemon --local-root /tmp/skill-sync-target --remote file:///tmp/skill-sync-remote --prefix snapshots/current --cache-dir ./cache-preview --work-dir ./sync-work --state-file ./sync-work/state.json --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json --dry-run --max-cycles 1
python3 -m skill_sync_sidecar rollback --record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json --yes
```

Current validation node status:

```bash
scripts/status-current.sh
```

Release gate:

```bash
scripts/verify-release.sh
```

Run against explicit roots:

```bash
python3 -m skill_sync_sidecar scan --root cc-switch=~/.cc-switch/skills --root skillshub=~/.skillshub
```

## Default Roots

The sidecar scans existing local roots when they exist:

- `~/.cc-switch/skills`
- `~/.skillshub`
- `~/.codex/skills`

## Sync Boundary

The sync unit is a skill package, not an entire application state. API keys, accounts, local provider settings, and tool-specific runtime caches are out of scope.

Skill packages can be global or project-scoped:

- Global skills are portable across tools and devices.
- Project skills live under a repository, usually `skills/<skill-id>/`, and depend on that project's code context.

See [docs/manifest-v0.md](docs/manifest-v0.md) for the canonical metadata contract.
See [docs/operations.md](docs/operations.md) for WebDAV smoke tests, daemon rollout, and launchd/systemd templates.
See [docs/acceptance-report.md](docs/acceptance-report.md) for the current MVP validation evidence and safety boundary.
See [docs/release.md](docs/release.md) for the package smoke test and release checklist.

Generated or bulky directories are excluded from hashing and future packaging:

- `.cache`, `.git`, `.hg`, `.next`, `.pytest_cache`, `.ruff_cache`, `.svn`, `.tox`, `.venv`
- `__pycache__`, `build`, `dist`, `logs`, `node_modules`, `target`, `tmp`, `venv`

## WebDAV Configuration

HTTP(S) remotes use Basic Auth from environment variables:

```bash
export SKILL_SYNC_WEBDAV_USER="..."
export SKILL_SYNC_WEBDAV_PASSWORD="..."
python3 -m skill_sync_sidecar remote-status --remote "https://example.com/dav/cc-skill-sync"
```

Credentials are never printed. Use `--username-env` and `--password-env` to point at different environment variables.

You can also reuse the existing cc-switch WebDAV configuration without putting credentials in the command line:

```bash
python3 -m skill_sync_sidecar remote-status \
  --cc-switch-webdav \
  --prefix "skill-sync-sidecar-dev/test"
```

`push` is safe by default:

```bash
# Dry-run only
python3 -m skill_sync_sidecar push \
  --snapshot-dir ./snapshot-preview \
  --cc-switch-webdav \
  --prefix "skill-sync-sidecar-dev/test"

# Actually uploads
python3 -m skill_sync_sidecar push \
  --snapshot-dir ./snapshot-preview \
  --cc-switch-webdav \
  --prefix "skill-sync-sidecar-dev/test" \
  --yes
```

`pull-cache` only downloads to a cache directory. It does not install or overwrite local tool directories.

For HTTP(S) uploads, `--prefix` is required and uploads to `cc-switch-sync` are refused by default.

Run the repeatable WebDAV smoke test after changing remote, sync-cycle, or sync-daemon behavior:

```bash
scripts/webdav-smoke.sh "skill-sync-sidecar-dev/smoke-$(date +%Y%m%d%H%M%S)"
```

The smoke test uploads only synthetic data under the provided dev prefix. It checks a zero-skill canary, a synthetic `sync-cycle` install, and a synthetic `sync-daemon --max-cycles 1` install into `/private/tmp`.

Run the real local root through a non-network dry run before uploading private skill content anywhere:

```bash
scripts/local-real-dryrun.sh "$HOME/.cc-switch/skills"
```

This packages the real local root, pushes it to a file-backed remote under `/private/tmp`, and runs `sync-daemon --dry-run --max-cycles 1` with a state file. It does not upload private skills to WebDAV.

After explicit approval to upload private skill content, run the real WebDAV dry-run:

```bash
SKILL_SYNC_ALLOW_PRIVATE_WEBDAV_UPLOAD=1 \
  scripts/real-webdav-dryrun.sh "$HOME/.cc-switch/skills" "skill-sync-sidecar-dev/real-$(hostname -s)-$(date +%Y%m%d%H%M%S)"
```

This uploads the real local root to the provided WebDAV prefix, reads it back, and runs `sync-daemon --dry-run --max-cycles 1` with a state file. It still does not install a daemon or write changes into the source skill root.

Install the current macOS device as a launchd service:

```bash
SKILL_SYNC_ALLOW_PRIVATE_WEBDAV_UPLOAD=1 \
SKILL_SYNC_DAEMON_MODE=yes \
SKILL_SYNC_PREFIX=skill-sync-sidecar-dev/current-mac \
  scripts/install-current-launchd.sh
```

The current installed service writes status to:

```text
~/Library/Application Support/skill-sync-sidecar/state.json
```

## Apply and Rollback

`apply --dry-run` is the normal inspection step:

```bash
python3 -m skill_sync_sidecar apply \
  --staged-dir ./staging-preview/<snapshot-id> \
  --target cc-switch-global \
  --target-root /tmp/skill-sync-target \
  --dry-run
```

`apply --yes` writes only to an explicit target root. For `cc-switch-global`, omitting `--target-root` is refused so the sidecar cannot accidentally overwrite `~/.cc-switch/skills`.

```bash
python3 -m skill_sync_sidecar apply \
  --staged-dir ./staging-preview/<snapshot-id> \
  --target cc-switch-global \
  --target-root /tmp/skill-sync-target \
  --yes
```

Every real apply writes a rollback record under:

```text
<target-root>/.skill-sync-backups/<apply-id>/.apply-record.json
```

Rollback restores replaced skills from backup and removes newly installed skills:

```bash
python3 -m skill_sync_sidecar rollback \
  --record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json \
  --yes
```

## Conflict Detection

Before automatic sync, compare three states:

- `local`: the installed target root currently on this machine.
- `remote`: the downloaded WebDAV snapshot/cache.
- `last-applied`: the `.apply-record.json` from the last successful apply.

```bash
python3 -m skill_sync_sidecar sync-status \
  --local-root /tmp/skill-sync-target \
  --remote-snapshot ./cache-preview \
  --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json \
  --fail-on-conflict
```

Actions mean:

- `unchanged`: local and remote both match the last-applied base.
- `pull`: remote changed, local did not.
- `push`: local changed, remote did not.
- `conflict`: local and remote both changed differently.
- `local_new` / `remote_new`: no base exists for that skill yet.
- `local_deleted` / `remote_deleted`: one side deleted a skill while the other still matches base.

Old apply records without `content_hash` are refused as a sync base; re-run `apply --yes` with the current sidecar to create a usable base record.

When conflicts exist, materialize them before resolving:

```bash
python3 -m skill_sync_sidecar conflict-package \
  --local-root /tmp/skill-sync-target \
  --remote-snapshot ./cache-preview \
  --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json \
  --out ./conflicts
```

Each conflict package contains:

- `metadata.json`: conflict reason, local/remote/base hashes, and source paths.
- `local/`: the current local skill files, when present.
- `remote/`: the staged and hash-verified remote skill files, when present.
- `base.json`: common ancestor hash and last-applied record reference. Full base files are copied only when the old source path is still available.

The command also writes `conflict-index.json` under the output directory.

One-sided deletes are not executed directly. Create tombstone records first:

```bash
python3 -m skill_sync_sidecar tombstone \
  --local-root /tmp/skill-sync-target \
  --remote-snapshot ./cache-preview \
  --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json \
  --out ./tombstones
```

Each tombstone contains `tombstone.json`, `base.json`, and whichever side still has material (`local/` or `remote/`). Tombstones are non-destructive markers; they do not delete local files or remove remote archives. Later delete execution should consume these records with an explicit retention and rollback gate.

`sync-plan` turns the status into a dry-run execution plan:

```bash
python3 -m skill_sync_sidecar sync-plan \
  --local-root /tmp/skill-sync-target \
  --remote-snapshot ./cache-preview \
  --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json \
  --fail-on-blocked
```

By default it allows one-sided `pull` and `push`, but blocks conflicts, new skills, and deletions. Use `--allow-new` or `--allow-delete` only after reviewing the plan.

`sync-apply` is the first execution layer above `sync-plan`:

```bash
python3 -m skill_sync_sidecar sync-apply \
  --local-root /tmp/skill-sync-target \
  --remote-snapshot ./cache-preview \
  --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json \
  --dry-run
```

Real execution requires `--yes`. Pull actions stage and validate the remote snapshot, write through the same `apply` backup/rollback path, and emit a fresh `.apply-record.json`. Push actions require `--remote` or `--cc-switch-webdav`; before uploading, the sidecar verifies that the remote's current `index.json` still matches the local cache so a stale cache cannot overwrite another device's newer change. A successful push writes a `.skill-sync-bases/<sync-id>.json` base record that should be used as the next `--last-applied-record`.

```bash
python3 -m skill_sync_sidecar sync-apply \
  --local-root /tmp/skill-sync-target \
  --remote-snapshot ./cache-preview \
  --last-applied-record /tmp/skill-sync-target/.skill-sync-bases/<sync-id>.json \
  --remote file:///tmp/skill-sync-remote \
  --prefix snapshots/current \
  --yes
```

Project-scoped skills can be applied into a repository without treating them as global cc-switch skills:

```bash
python3 -m skill_sync_sidecar sync-apply \
  --target codex-project \
  --project-root /path/to/repo \
  --remote-snapshot ./cache-preview \
  --allow-new \
  --yes
```

For `codex-project`, `--local-root` defaults to `<project-root>/skills` and rollback metadata is written under `<project-root>/.skill-sync-backups`.

`sync-cycle` is the one-shot automation layer. It always downloads the remote snapshot into `--cache-dir`, builds `sync-status` and `sync-plan`, and writes conflict packages or tombstones under `--work-dir` when needed. With `--dry-run` it stops there. With `--yes`, it executes only the same safe `sync-apply` subset: one-sided pull/push actions with no blocked items, no conflicts, and no delete propagation.

```bash
python3 -m skill_sync_sidecar sync-cycle \
  --local-root /tmp/skill-sync-target \
  --remote file:///tmp/skill-sync-remote \
  --prefix snapshots/current \
  --cache-dir ./cache-preview \
  --work-dir ./sync-work \
  --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json \
  --dry-run

python3 -m skill_sync_sidecar sync-cycle \
  --local-root /tmp/skill-sync-target \
  --remote file:///tmp/skill-sync-remote \
  --prefix snapshots/current \
  --cache-dir ./cache-preview \
  --work-dir ./sync-work \
  --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json \
  --yes
```

`sync-daemon` repeats the same cycle on an interval. It defaults to stopping when a cycle is blocked, which prevents a conflict or pending delete from being retried forever without review. Use `--max-cycles` for finite tests or launch checks; omit it only when running under a process supervisor.

For OpenClaw, run the stricter gate before converting a dry-run service into a writable service:

```bash
python3 -m skill_sync_sidecar openclaw-gate --require-complete --fail-on-blocked
```

The default OpenClaw gate is compatible with supervised allowlist pulls. `--require-complete` additionally blocks when `remote_new` remains, so unattended writes only start after every canonical package has been reviewed or explicitly deferred elsewhere.

Before changing any OpenClaw service unit, run the finite writable rehearsal:

```bash
scripts/openclaw-writable-rehearsal.sh
```

The rehearsal first enforces the strict gate, then runs `sync-daemon --yes --max-cycles 1 --interval-seconds 0`. It is intentionally a one-shot command and does not edit systemd units or install a long-running writable daemon.

When a peer root and the remote snapshot already match but no common base exists, adopt that state before long-running multi-writer sync:

```bash
python3 -m skill_sync_sidecar adopt-base \
  --local-root /home/admin/clawd/skills \
  --remote-snapshot /opt/skill-sync-sidecar/cache/openclaw-writable-rehearsal \
  --out /opt/skill-sync-sidecar/state/openclaw-base-record.json \
  --prefix skill-sync-sidecar-dev/current-mac \
  --dry-run
```

Use `--yes` only when the dry run reports `safe_to_adopt=true` and every item is `same_without_base`, `unchanged`, or `already_converged`. The resulting base record makes follow-up `sync-status --last-applied-record ...` report `unchanged` instead of `same_without_base`.

```bash
python3 -m skill_sync_sidecar sync-daemon \
  --local-root /tmp/skill-sync-target \
  --remote file:///tmp/skill-sync-remote \
  --prefix snapshots/current \
  --cache-dir ./cache-preview \
  --work-dir ./sync-work \
  --state-file ./sync-work/state.json \
  --last-applied-record /tmp/skill-sync-target/.skill-sync-backups/<apply-id>/.apply-record.json \
  --dry-run \
  --interval-seconds 300 \
  --max-cycles 1
```

`sync-apply` refuses conflicts, deletions, project-to-global installs, global-to-project installs, and push attempts where the remote has drifted since `pull-cache`.

### Canary Check

When policy or security review blocks uploading real skills, validate WebDAV write/read with a zero-skill canary snapshot first:

```bash
python3 -m skill_sync_sidecar snapshot \
  --root canary=/private/tmp/skill-sync-empty-root-does-not-exist \
  --out webdav-canary-preview \
  --label webdav-canary

python3 -m skill_sync_sidecar push \
  --snapshot-dir webdav-canary-preview \
  --cc-switch-webdav \
  --prefix "skill-sync-sidecar-dev/canary-<timestamp>" \
  --yes

python3 -m skill_sync_sidecar remote-status \
  --cc-switch-webdav \
  --prefix "skill-sync-sidecar-dev/canary-<timestamp>"

python3 -m skill_sync_sidecar pull-cache \
  --cc-switch-webdav \
  --prefix "skill-sync-sidecar-dev/canary-<timestamp>" \
  --out webdav-canary-cache
```

This only uploads `index.json` with `total: 0`. It proves WebDAV credentials and path handling without transferring local skill content.

### Real Skill Upload Handoff

Uploading a real snapshot transfers local skill content to WebDAV. If an automated agent is blocked by data-transfer policy, run the command manually from a trusted terminal:

```bash
python3 -m skill_sync_sidecar push \
  --snapshot-dir snapshot-preview \
  --cc-switch-webdav \
  --prefix "skill-sync-sidecar-dev/<timestamp>" \
  --yes
```

## Roadmap

1. Read-only scanner and doctor.
2. Local WebDAV-ready snapshot export.
3. WebDAV remote inventory, dry-run push, and pull-cache.
4. Remote diff against local snapshot.
5. `push` with conflict detection and risk gate.
6. Real `apply --yes` with backup and rollback.
7. Tool adapters for skillshub, Codex project roots, and OpenClaw packaging details.

## Acceptance Cases

- `cc-switch` global snapshot: 91 discoverable `SKILL.md` packages, 91 remote archives, clean round-trip diff.
- `libtv-m-forward` project skill: `libtv-m/skills/libtv-m-forward/`, `scope=project`, scripts included, generated files excluded, no global install by default.
