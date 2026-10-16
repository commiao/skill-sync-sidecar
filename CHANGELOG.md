# Changelog

All notable changes to Skill Sync Sidecar are documented here.

## Unreleased

### Added

- Explicit writer policy controls for `sync-plan`, `sync-apply`, `sync-cycle`, `sync-daemon`, and ops status.
- `pull-only`, `push-only`, and `no-writes` modes for peers that must not publish or consume changes automatically.
- `blocked-report` command and automatic `sync-cycle` blocked review reports for policy blocks, conflicts, new-skill review, and delete review.
- OpenClaw base-adoption, writer-policy, and blocked-report deployment validation documents.
- Sidecar-owned WebDAV config (`config-init --from-cc-switch`, `--webdav-config`): URL/username in the sidecar config, password from Keychain, env or a mode-600 file. Falls back to cc-switch while no config exists (T-0260 step 1).
- Configurable canonical local root (`local_root` in the sidecar config) and `local-root-migrate --dry-run|--yes`, which copies `~/.cc-switch/skills` to the sidecar's own root and switches the config only after per-skill hashes and sync actions match (T-0260 step 2).
- `remote_snapshot`, `remote_prefix` and `base_record` in the sidecar config, used by the operator executor instead of hardcoded `~/public-sync/...`, prefix and base-record paths; defaults are unchanged (T-0260 step 3).
- `fanout --dry-run`: read-only plan for placing canonical skills into each tool (symlinks; copies for Cursor), adopting same-content entries and never touching foreign or diverged ones (T-0260 step 4, PR A).
- `fanout --yes` and `fanout-rollback`: per-tool lock, backups and run records in the sidecar state directory, idempotent re-runs, and a read-only canonical root once tools link to it; sidecar deletes/moves lift the protection first (T-0260 step 4, PR B).
- `sync-daemon --fanout` / `--fanout-interval-seconds`: run fanout after consistent cycles when the canonical fingerprint changes, hourly, or after failures; failures are recorded per cycle and never fail the sync. Fanout content comparisons now include nested skills (T-0260 step 4, PR C).
- `local-skill-install` leaves tool entries that are symlinks into the canonical root alone (`noop`, fanout keeps them current) instead of replacing them with copies (T-0260).
- Fanout adoption ignores `manifest.json` and dotfiles when deciding whether a tool entry is the same skill (resolves 10 false conflicts on the current Mac); fanout run records are capped at the latest 200 (T-0260).
- Everything copied out of the read-only canonical root is made writable (`copy_tree_writable`): fanout copies, `local-skill-publish` / `local-skill-install` temporary trees and backups, conflict packages and tombstones; `sync-cycle` resets report directories even when they hold read-only copies; a failed fanout copy action removes its temporary directory (T-0260).
- skillshub is retired as a destination and cc-switch is no longer written directly: `local-skill-install` targets the canonical root (`sidecar`, always first) plus the tool roots; skillshub left the default manifest/scanner targets, the executor's install targets and the projection adapters; `apply` into a tool skips entries that are symlinks into the canonical root; tool status labels the canonical root, cc-switch (fanout target) and skillshub (retired) (T-0260 step 6).

### Changed

- OpenClaw rehearsals and service templates now use explicit `--writer-policy pull-only`.
- Desktop launchd/systemd examples make their writer policy explicit instead of relying on CLI defaults.

## v0.1.3 - 2026-06-16

Linux/OpenClaw runtime compatibility patch.

### Fixed

- Removed macOS-only `/private/tmp` assumptions from core sync staging paths.
- `sync-apply`, conflict packaging, and tombstone packaging now use the platform default temporary directory.
- Package smoke now rejects stale wheel artifacts whose filename version does not match the current package version.

## v0.1.2 - 2026-06-15

Release workflow hardening.

### Added

- Reusable `scripts/build-release-assets.sh` shared by local release checks and GitHub Actions.

### Changed

- GitHub Release workflow installs build dependencies explicitly before building assets.

## v0.1.1 - 2026-06-15

Release infrastructure and metadata polish.

### Added

- GitHub Release workflow with wheel, source archive, and git bundle assets.
- MIT license, security policy, changelog, and README install instructions.

### Changed

- Package smoke test no longer depends on the old pip `in-tree-build` feature flag.

## v0.1.0 - 2026-06-15

Initial MVP release.

### Added

- Skill scanner, doctor, status, and normalized snapshot protocol.
- WebDAV/file remote push and pull-cache commands.
- Staging, dry-run apply, explicit apply, and rollback flows.
- Three-way sync status, sync plan, sync apply, sync cycle, and sync daemon.
- Conflict package and tombstone materialization.
- OpenClaw read-only reconcile gate and current-node ops status.
- Release verification, package smoke test, GitHub publish script, CI, and release workflow.

### Safety

- Remote uploads and local installs require explicit confirmation flags.
- Official `cc-switch-sync` writes are refused by default.
- Credentials, tool databases, and runtime caches are outside the sync boundary.
