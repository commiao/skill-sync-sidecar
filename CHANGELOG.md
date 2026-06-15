# Changelog

All notable changes to Skill Sync Sidecar are documented here.

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
