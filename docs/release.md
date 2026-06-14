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
