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

Build the release artifacts locally with the same script used by GitHub Actions:

```bash
RELEASE_TAG=v0.1.3 scripts/build-release-assets.sh
```

CI can opt into build dependency installation:

```bash
SKILL_SYNC_INSTALL_BUILD_DEPS=1 RELEASE_TAG=v0.1.3 scripts/build-release-assets.sh
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

## Mac installed release

The Mac runtime is `~/.local/share/skill-sync-sidecar/current`, a link to
`releases/<sha>/`. Releasing switches the link; it does not touch processes that
are already running. A long-running Python job keeps the code it imported at
startup until it is restarted, so a release that does not restart them is not
live. On 2026-10-13 `com.skill-sync-sidecar` and `.dashboard` were still running
code from 2026-09-30, and `.operator-executor` was still using a release
directory that had already been pruned.

Release a main commit:

```bash
python3 ~/.local/share/fleet-ops/current/bin/mac-release.py skill-sync-sidecar \
  --repo ~/work-ai/skill-sync-sidecar --ref <main-sha>
```

After `current` has been switched, `mac-release.py` restarts (`launchctl kickstart
-k`) every job declared in [`mac-release.json`](../mac-release.json) at the repo
root. The release command used to need four `--reload` flags typed by hand; the
list now travels with the commit. This needs fleet-ops with declared-reload
support (fleet-ops T-0277); an older `mac-release.py` ignores the file and
restarts nothing.

| Label | How it runs | Restart on release |
| --- | --- | --- |
| `com.skill-sync-sidecar` | `sync-daemon`, one cycle every 300 s | yes, between cycles |
| `com.skill-sync-sidecar.operator-executor` | KeepAlive HTTP server, port 18765 | yes |
| `com.skill-sync-sidecar.dashboard` | KeepAlive HTTP server, port 8765 | yes |
| `com.skill-sync-sidecar.gateway` | KeepAlive HTTP server, port 8877 | yes |
| `com.skill-sync-sidecar.mac-peer-status` | `StartInterval` 300 s | no, each run starts fresh |
| `com.skill-sync-sidecar.nas-dashboard-export` | `StartInterval` 300 s | no, each run starts fresh |
| `com.skill-sync-sidecar.openclaw-peer-status` | `StartInterval` | no, each run starts fresh |

When you add a long-running job (an installer whose plist has no
`StartInterval`), add its label to `mac-release.json` and to this table.
`tests/test_mac_release_decl.py` derives the resident jobs from `scripts/` and
fails if the declaration does not match.

`sync-daemon` must not be killed in the middle of a cycle. Its entry carries
`when_idle: scripts/sync-daemon-idle.py`, which `mac-release.py` polls from the
release directory (up to 360 s) and restarts the daemon only once it exits 0:
no `active_cycle` in `state.json` and the next cycle more than 60 s away. If it
never sees a gap, it leaves the daemon running, exits 3 and prints the
`launchctl kickstart -k gui/$(id -u)/com.skill-sync-sidecar` command to run in
the next gap. Exit 3 means `current` was switched but some job is still on old
code; it is not a success.

`--no-reload` skips the declared jobs. Use it only when you will restart them
yourself.

After the release, check the running processes, not the files on disk:

```bash
python3 ~/.local/share/fleet-ops/current/bin/mac-release.py skill-sync-sidecar \
  --repo ~/work-ai/skill-sync-sidecar --check
for l in com.skill-sync-sidecar com.skill-sync-sidecar.operator-executor \
         com.skill-sync-sidecar.dashboard com.skill-sync-sidecar.gateway; do
  pid=$(launchctl list | awk -v l="$l" '$3==l{print $1}')
  echo "$l pid=$pid started=$(ps -o lstart= -p "$pid")"
done
curl -s http://127.0.0.1:18765/healthz   # repo_root must be .../skill-sync-sidecar/current
```

- Every start time must be later than the release.
- The executor must report the `current` path, not a `releases/<sha>` path.
- The first `sync-daemon` cycle after the restart must be `complete` with no
  `blocked` items.

## Read-only NAS dashboard release

The NAS Gateway is a read-only dashboard/aggregator. A release that changes
only its dashboard presentation must not be blocked by unrelated skill-sync
review items, but it may use the exception only through this machine-checked
profile:

```bash
scripts/verify-release.sh nas-dashboard-only
```

The profile belongs only to the `skill-sync-sidecar` project: its validator checks
the repository package identity before applying the service-specific allowlist.
Other projects use their own release contracts and the normal full gate.

The profile always runs the full tests, compile check, and package smoke test.
It rejects a dirty worktree and rejects every changed path outside its explicit
allowlist (the dashboard template, its tests, and the contract implementation
and documentation). It does **not** accept `SKILL_SYNC_SKIP_OPS_STATUS=1` as a
substitute. Changes to sync logic, WebDAV writes, device installs/removals, or
OpenClaw policy must use the normal full gate and a green sync status.

After deployment, verify the actual Gateway HTML contains the contract's new
markers as well as the deployed commit and health endpoints:

```bash
SKILL_SYNC_NAS_HTML_CHECKS='dashboard-sidebar,skills-hub-header,selectSkillsHubView,renderSkillsHubChrome' \
  scripts/verify-nas-gateway-deploy.sh <commit>
```

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
(`v0.1.3` by default).

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
