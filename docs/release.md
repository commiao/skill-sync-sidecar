# Skill Sync Sidecar Release Checklist

Use this checklist before handing a build to another device or packaging a tagged release.

## Preflight

Run the full release gate:

```bash
scripts/verify-release.sh
```

For non-Mac or CI environments that do not have the current validation node paths, skip the operational gate:

```bash
SKILL_SYNC_SKIP_OPS_STATUS=1 scripts/verify-release.sh
```

The full gate expands to:

```bash
PYTHONPATH=src PYTHONPYCACHEPREFIX=/private/tmp/skill-sync-pycache \
  python3 -m unittest discover -s tests

PYTHONPATH=src PYTHONPYCACHEPREFIX=/private/tmp/skill-sync-pycache \
  python3 -m compileall -q src tests
```

## Package Smoke

```bash
scripts/package-smoke.sh
```

The smoke script builds a wheel without network build isolation, installs it into a clean venv under `/private/tmp`, then verifies:

- `skill-sync --version`
- `skill-sync status`
- `skill-sync snapshot`
- `skill-sync remote-status` against a file remote

## Operational Gate

```bash
scripts/status-current.sh
```

The gate must be green before a release is promoted from the current Mac validation node.

## GitHub Publish

Prerequisites:

- The destination repository exists.
- The local machine has Git SSH push permission, or `origin` already points at an authenticated HTTPS remote.
- The worktree is clean.
- The release tag exists locally.

Default publish target:

```bash
scripts/publish-github.sh
```

The default remote is:

```text
git@github.com:commiao/skill-sync-sidecar.git
```

Override it when publishing to another repository:

```bash
scripts/publish-github.sh git@github.com:<owner>/<repo>.git
```

The script pushes `main` and the tag selected by `SKILL_SYNC_RELEASE_TAG`
(`v0.1.1` by default).

## GitHub Release

The repository includes `.github/workflows/release.yml` for publishing release assets.

For new tags, pushing `v*` triggers the release workflow automatically. For an existing tag,
run the `release` workflow manually from GitHub Actions and enter the tag.

The workflow builds and uploads:

- `skill_sync_sidecar-<version>-py3-none-any.whl`
- `skill-sync-sidecar-<version>-source.tar.gz`
- `skill-sync-sidecar-<version>.bundle`

If a release already exists, the workflow re-uploads the assets with `--clobber`.

If GitHub CLI is preferred, login/create the repository first:

```bash
gh auth login
gh repo create commiao/skill-sync-sidecar --private --source=. --remote=origin --push
git push origin "$SKILL_SYNC_RELEASE_TAG"
```

## Versioning

Version numbers are currently duplicated in:

- `pyproject.toml`
- `setup.cfg`
- `src/skill_sync_sidecar/__init__.py`

Update all three together before tagging a release.

## Current Safety Boundary

Release packaging must not:

- write to OpenClaw skill roots
- use the official `cc-switch-sync` prefix
- include WebDAV credentials in artifacts
- include local runtime caches, snapshots, or staging outputs
