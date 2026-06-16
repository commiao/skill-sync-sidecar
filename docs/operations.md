# Skill Sync Sidecar Operations

This guide covers the safe path from smoke test to supervised daemon mode.

## Safety Defaults

- Use a dedicated dev prefix until production policy is agreed.
- Do not write to `cc-switch-sync`; the CLI refuses HTTP uploads to that prefix.
- Start daemon runs with `--dry-run`.
- Use `--state-file` so operators can inspect the latest cycle without reading logs.
- Leave delete propagation disabled; one-sided deletes produce tombstones only.

## WebDAV Smoke Test

Run this after changing remote, sync-cycle, sync-daemon, or service configuration:

```bash
scripts/webdav-smoke.sh "skill-sync-sidecar-dev/smoke-$(date +%Y%m%d%H%M%S)"
```

The script validates:

- zero-skill canary upload and pull
- synthetic `sync-cycle --yes` install into `/private/tmp`
- synthetic `sync-daemon --yes --max-cycles 1` install into `/private/tmp`

It uploads only synthetic data under the prefix passed as the first argument.

## Local Real-Root Dry Run

Before any real skill content is uploaded to WebDAV, validate the local root through a file-backed remote:

```bash
scripts/local-real-dryrun.sh "$HOME/.cc-switch/skills"
```

This uses the real local skill directory, but all snapshot, remote, cache, work, and state files stay under `/private/tmp`. It does not upload real skill content to WebDAV and does not write into the source skill root.

Uploading the real `~/.cc-switch/skills` snapshot to WebDAV is a separate data movement decision. It should require explicit approval because private skills may contain proprietary workflows, prompts, scripts, or operational knowledge even when no credentials are present.

After explicit approval, run:

```bash
SKILL_SYNC_ALLOW_PRIVATE_WEBDAV_UPLOAD=1 \
  scripts/real-webdav-dryrun.sh "$HOME/.cc-switch/skills" "skill-sync-sidecar-dev/real-$(hostname -s)-$(date +%Y%m%d%H%M%S)"
```

The script uploads real private skill content to the provided WebDAV prefix, reads it back, and verifies `sync-daemon --dry-run --max-cycles 1` with a state file. It still does not run `sync-daemon --yes` against the real root.

## One-Shot Dry Run

```bash
PYTHONPATH=src python3 -m skill_sync_sidecar sync-cycle \
  --local-root "$HOME/.cc-switch/skills" \
  --cc-switch-webdav \
  --prefix "skill-sync-sidecar-dev/$(hostname -s)" \
  --cache-dir "$HOME/Library/Caches/skill-sync-sidecar/cache" \
  --work-dir "$HOME/Library/Application Support/skill-sync-sidecar/work" \
  --dry-run \
  --json
```

Use `--allow-new` only after reviewing the plan. Use `--yes` only after the dry-run output has no blocked items and the target root is intentional.

## macOS launchd

Template:

```text
examples/launchd/com.skill-sync-sidecar.plist
```

Before loading it:

1. Replace `YOUR_USER`, `YOUR_DEVICE`, and `/PATH/TO/skill-sync-sidecar`.
2. Keep `--dry-run` for the first supervised run.
3. Keep the prefix under `skill-sync-sidecar-dev/...` until production policy is set.
4. Confirm `~/.cc-switch/settings.json` has valid WebDAV settings.

Suggested validation:

```bash
plutil -lint examples/launchd/com.skill-sync-sidecar.plist
```

Load only after editing a copied plist:

```bash
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.skill-sync-sidecar.plist"
launchctl print gui/$(id -u)/com.skill-sync-sidecar
cat "$HOME/Library/Application Support/skill-sync-sidecar/state.json"
```

Current-device installer:

```bash
SKILL_SYNC_ALLOW_PRIVATE_WEBDAV_UPLOAD=1 \
SKILL_SYNC_DAEMON_MODE=yes \
SKILL_SYNC_PREFIX=skill-sync-sidecar-dev/current-mac \
  scripts/install-current-launchd.sh
```

The installer uploads the current local root to the chosen prefix, writes a stable base record, performs a dry-run preflight, writes `~/Library/LaunchAgents/com.skill-sync-sidecar.plist`, then starts `sync-daemon`. Use `SKILL_SYNC_DAEMON_MODE=dry-run` for observation-only mode.

Check status:

```bash
launchctl print gui/$(id -u)/com.skill-sync-sidecar
cat "$HOME/Library/Application Support/skill-sync-sidecar/state.json"
```

One-screen sidecar status:

```bash
PYTHONPATH=src python3 -m skill_sync_sidecar ops-status --allow-new
```

By default, `ops-status` also searches `/private/tmp/openclaw-skill-sync-validate` for the latest OpenClaw `reconcile-report.json` and shows the read-only gate state when one exists. Include an explicit report when reviewing a specific peer-writer drift run:

```bash
PYTHONPATH=src python3 -m skill_sync_sidecar ops-status \
  --allow-new \
  --openclaw-reconcile-report /private/tmp/openclaw-skill-sync-validate/reconcile-20260614-after-drift-3/reconcile/reconcile-report.json
```

State interpretation:

- `active_cycle={"cycle": N, "status": "running"}` means the daemon has started a cycle and is currently in scan, WebDAV, or apply work.
- A cycle with `status=error` is recoverable; the daemon records the error and continues on the next interval.
- `summary={"noop": 91}` with `applied=0` and `uploaded=0` means the local root, cache, and WebDAV snapshot are aligned.

WebDAV write behavior:

- Archives are content-addressed by `content_hash`; existing archive paths are skipped on upload.
- `index.json` is uploaded last. If a push is interrupted before the final index write, readers keep seeing the previous complete snapshot.
- If `index.json` ever points to a missing archive, repair by uploading the missing archive first, then uploading the matching `index.json`.
- Some WebDAV providers reset `HEAD` requests. The client falls back to `PROPFIND` directory checks and caches those directory listings during a push.
- If direct HTTP(S) WebDAV `PUT` is slow or timing out, prefer a local WebDAV sync folder as a file remote. On the Mac validation device, the installed daemon uses:

```text
--remote file:///Users/mac/public-sync
--prefix skill-sync-sidecar-dev/current-mac
```

In this mode the sidecar writes archives and `index.json` to `/Users/mac/public-sync/skill-sync-sidecar-dev/current-mac`, and the desktop WebDAV client handles cloud upload. This avoids long direct `PUT` calls while preserving the same archive-first, index-last protocol.

Stop the service:

```bash
launchctl bootout gui/$(id -u) "$HOME/Library/LaunchAgents/com.skill-sync-sidecar.plist"
```

## Linux / OpenClaw systemd

Template:

```text
examples/systemd/skill-sync-sidecar.service
```

OpenClaw preflight:

```bash
ssh root@oc-vps-aliyun-us 'python3 --version; ls -ld /home/admin/clawd/skills /home/admin/.cc-switch/skills /root/.cc-switch/skills 2>/dev/null || true'
```

Known OpenClaw constraints:

- `/home/admin/clawd/skills` is the OpenClaw project skill root used for second-node validation.
- `/home/admin/.cc-switch/settings.json` contains the cc-switch WebDAV configuration.
- `/root/.cc-switch/settings.json` can exist without WebDAV credentials, so a root-owned service must not assume `--cc-switch-webdav` will read the admin user's config.
- The observed system Python is 3.6.8, while sidecar requires Python >=3.9.
- Do not replace `/usr/bin/python3`, use `alternatives`, or install sidecar dependencies into system Python. OpenClaw uses an isolated runtime under `/opt/skill-sync-sidecar`.

OpenClaw isolated runtime:

```text
uv=/opt/skill-sync-sidecar/bin/uv
python=/opt/skill-sync-sidecar/python/cpython-3.11-linux-x86_64-gnu/bin/python3.11
venv=/opt/skill-sync-sidecar/venv
skill-sync=/opt/skill-sync-sidecar/venv/bin/skill-sync
state=/opt/skill-sync-sidecar/state
cache=/opt/skill-sync-sidecar/cache
work=/opt/skill-sync-sidecar/work
```

System Python remains unchanged:

```text
/usr/bin/python3 -> Python 3.6.8
/opt/skill-sync-sidecar/venv-0.1.3/bin/python -> Python 3.11.15
```

The OpenClaw dry-run systemd template is:

```text
examples/systemd/openclaw-skill-sync-sidecar-dryrun.service
```

It runs as `admin`, uses the isolated venv, reads `/home/admin/.cc-switch/settings.json`, and remains in `--dry-run`.

The installed OpenClaw unit is:

```text
/etc/systemd/system/openclaw-skill-sync-sidecar-dryrun.service
```

Useful checks:

```bash
systemctl status openclaw-skill-sync-sidecar-dryrun.service --no-pager
journalctl -u openclaw-skill-sync-sidecar-dryrun.service --no-pager -n 80
python3 -m json.tool /opt/skill-sync-sidecar/state/openclaw-daemon-dryrun-state.json
```

Expected steady-state after the reviewed P0, P1 Wave-1/Wave-2/Wave-3/Wave-4/Wave-5/Wave-6/Wave-7/Wave-8/Wave-9, and P2a Wave-1/Wave-2/Wave-3 allowlists have been installed:

```text
cycle_status=dry_run
summary={"noop": 70, "pull_new": 22}
blocked=0
applied=0
uploaded=0
```

This service is allowed to stay running because it is dry-run-only. Do not convert it to `--yes` until the remaining 22 `pull_new` skills are explicitly reviewed for OpenClaw live installation.

Current installed OpenClaw dry-run service runtime:

```text
version=skill-sync 0.1.3
exec=/opt/skill-sync-sidecar/venv-0.1.3/bin/python -m skill_sync_sidecar sync-daemon ... --dry-run
unit_backup=/etc/systemd/system/openclaw-skill-sync-sidecar-dryrun.service.bak-20260616-0641
```

Admission review:

```text
docs/openclaw-admission-20260615.md
```

Current classification:

```text
p0_candidate=8
p1_review=18
p2_defer=34
```

The P0 candidate set passed isolated apply validation on both the Mac and OpenClaw `/tmp`, then was applied to `/home/admin/clawd/skills` as a supervised allowlist batch:

```text
apply_record=/home/admin/clawd/skills/.skill-sync-backups/20260616-035050-088751/.apply-record.json
installed=8
scan_after=40
post_apply_summary={"remote_new": 52, "same_without_base": 40}
dryrun_service_summary={"noop": 40, "pull_new": 52}
```

The first live attempt failed before installing anything because `.skill-sync-backups` was root-owned. The directory was corrected to `admin:admin` mode `755`, and the second apply succeeded. Keep future live operations running as `admin` or ensure backup/work directories are owned by the service user before applying.

OpenClaw live root currently has 70 sidecar-recognized skill packages after the reviewed P0, P1 Wave-1/Wave-2/Wave-3/Wave-4/Wave-5/Wave-6/Wave-7/Wave-8/Wave-9, and P2a Wave-1/Wave-2/Wave-3 allowlist applies. Existing non-package directories without `SKILL.md` are ignored by package scanning.

P1 Wave-1 isolated validation:

```text
allowlist=context-restore, context-save, investigate, learn, plan-tune, using-superpowers
local_snapshot=/private/tmp/openclaw-admission-p1-wave1-snapshot-20260616
openclaw_snapshot=/tmp/openclaw-admission-p1-wave1-snapshot-20260616-0624
openclaw_target=/tmp/openclaw-admission-p1-wave1-validate-20260616-0640/target
apply=6
scan=6
live_root_apply=false
```

During this validation, v0.1.2 failed on Linux because core staging used macOS-only `/private/tmp`. v0.1.3 removed that hardcoded temp path from `sync-apply`, conflict packaging, and tombstone packaging. Use v0.1.3 or later for any future OpenClaw apply validation.

P1 Wave-1 live allowlist apply:

```text
allowlist=context-restore, context-save, investigate, learn, plan-tune, using-superpowers
preflight_summary={"remote_new": 52, "same_without_base": 40}
apply_record=/home/admin/clawd/skills/.skill-sync-backups/20260616-071526-975930/.apply-record.json
installed=6
scan_after=46
post_apply_summary={"remote_new": 46, "same_without_base": 46}
dryrun_service_summary={"noop": 46, "pull_new": 46}
```

Use one-way `stage` + `apply` for reviewed partial live allowlists. Do not use `sync-apply` against a filtered snapshot on a populated live root unless a remote destination is intentionally provided; the two-way plan will see installed skills outside the filtered snapshot as `push_new`.

P1 Wave-2 live allowlist apply:

```text
allowlist=hackernews-frontpage, mcp-builder, pdf
preflight_summary={"remote_new": 46, "same_without_base": 46}
apply_record=/home/admin/clawd/skills/.skill-sync-backups/20260616-073448-384841/.apply-record.json
installed=3
scan_after=49
post_apply_summary={"remote_new": 43, "same_without_base": 49}
dryrun_service_summary={"noop": 49, "pull_new": 43}
```

P1 Wave-3 live allowlist apply:

```text
allowlist=design-consultation, design-shotgun, plan-design-review
preflight_summary={"remote_new": 43, "same_without_base": 49}
apply_record=/home/admin/clawd/skills/.skill-sync-backups/20260616-090724-681477/.apply-record.json
installed=3
scan_after=52
post_apply_summary={"remote_new": 40, "same_without_base": 52}
dryrun_service=active
```

P1 Wave-4 live allowlist apply:

```text
allowlist=design-html
preflight_summary={"remote_new": 40, "same_without_base": 52}
apply_record=/home/admin/clawd/skills/.skill-sync-backups/20260616-091809-502825/.apply-record.json
installed=1
scan_after=53
post_apply_summary={"remote_new": 39, "same_without_base": 53}
dryrun_service=active
```

P1 Wave-5 live allowlist apply:

```text
allowlist=make-pdf
preflight_summary={"remote_new": 39, "same_without_base": 53}
apply_record=/home/admin/clawd/skills/.skill-sync-backups/20260616-092324-622312/.apply-record.json
installed=1
scan_after=54
post_apply_summary={"remote_new": 38, "same_without_base": 54}
dryrun_service=active
```

P1 Wave-6 live allowlist apply:

```text
allowlist=office-hours
preflight_summary={"remote_new": 38, "same_without_base": 54}
apply_record=/home/admin/clawd/skills/.skill-sync-backups/20260616-092934-013600/.apply-record.json
installed=1
scan_after=55
post_apply_summary={"remote_new": 37, "same_without_base": 55}
dryrun_service=active
```

P1 Wave-7 live allowlist apply:

```text
allowlist=review
preflight_summary={"remote_new": 37, "same_without_base": 55}
apply_record=/home/admin/clawd/skills/.skill-sync-backups/20260616-100015-673428/.apply-record.json
installed=1
scan_after=56
post_apply_summary={"remote_new": 36, "same_without_base": 56}
dryrun_service=active
```

P1 Wave-8 live allowlist apply:

```text
allowlist=codex
preflight_summary={"remote_new": 36, "same_without_base": 56}
apply_record=/home/admin/clawd/skills/.skill-sync-backups/20260616-100635-761972/.apply-record.json
installed=1
scan_after=57
post_apply_summary={"remote_new": 35, "same_without_base": 57}
dryrun_service=active
```

P1 Wave-9 live allowlist apply:

```text
allowlist=autoplan, plan-ceo-review, plan-devex-review, plan-eng-review
selection=dependency-complete autoplan bundle; warnings reviewed as gstack cleanup/instructional destructive patterns
preflight_summary={"remote_new": 35, "same_without_base": 57}
apply_record=/home/admin/clawd/skills/.skill-sync-backups/20260616-101258-541631/.apply-record.json
installed=4
scan_after=61
post_apply_summary={"remote_new": 31, "same_without_base": 61}
dryrun_service=active
gateway=openclaw-gateway not restarted
```

P2a Wave-1 live allowlist apply:

```text
allowlist=careful, guard, pua
selection=small safety/private-workflow batch; pua secret scan found placeholders only
preflight_summary={"remote_new": 31, "same_without_base": 61}
apply_record=/home/admin/clawd/skills/.skill-sync-backups/20260616-102335-527826/.apply-record.json
installed=3
scan_after=64
post_apply_summary={"remote_new": 28, "same_without_base": 64}
dryrun_service=active
gateway=openclaw-gateway not restarted
```

P2a Wave-2 live allowlist apply:

```text
allowlist=browser, find-skills
selection=small discovery/browser-foundation batch; secret scan found no credential patterns
preflight_summary={"remote_new": 28, "same_without_base": 64}
apply_record=/home/admin/clawd/skills/.skill-sync-backups/20260616-102826-785168/.apply-record.json
installed=2
scan_after=66
post_apply_summary={"remote_new": 26, "same_without_base": 66}
dryrun_service=active
gateway=openclaw-gateway not restarted
```

P2a Wave-3 live allowlist apply:

```text
allowlist=document-release, health, landing-report, retro
selection=report/documentation/code-health workflow batch; secret scan found no credential patterns
preflight_summary={"remote_new": 26, "same_without_base": 66}
apply_record=/home/admin/clawd/skills/.skill-sync-backups/20260616-103348-831168/.apply-record.json
installed=4
scan_after=70
post_apply_summary={"remote_new": 22, "same_without_base": 70}
dryrun_service=active
gateway=openclaw-gateway not restarted
```

Read-only OpenClaw inventory:

```bash
ssh root@oc-vps-aliyun-us 'python3 - /home/admin/clawd/skills --source openclaw' \
  < scripts/remote-inventory-py36.py \
  > /private/tmp/openclaw-inventory.json
```

OpenClaw is a peer writer, not a downstream-only mirror. When OpenClaw skills are being edited by users or agents, run a reconcile report before any apply or daemon rollout:

```bash
PYTHONPATH=src python3 -m skill_sync_sidecar reconcile-report \
  --local-inventory /private/tmp/openclaw-inventory.json \
  --remote-snapshot /private/tmp/current-mac-cache \
  --previous-local-inventory /private/tmp/openclaw-inventory-previous.json \
  --label openclaw-current-$(date +%Y%m%d) \
  --out /private/tmp/openclaw-reconcile
```

Interpretation:

- `same_without_base`: local and remote match; safe candidate for base adoption.
- `remote_new`: remote has a skill OpenClaw lacks; review before pulling into OpenClaw.
- `local_new`: OpenClaw has a skill remote lacks; review before pushing to WebDAV.
- `conflict`: local and remote differ; do not apply automatically.
- `changed_since_previous`: OpenClaw changed since the last inventory; re-run reconcile before trusting an older plan.

If people continue optimizing OpenClaw skills, this report becomes the gate between normal editing and synchronization. A daemon should stay in dry-run or blocked mode whenever `conflict > 0` or unreviewed `local_new > 0`.

Reusable read-only script:

```bash
PYTHON_BIN=/path/to/python3 \
REMOTE_CACHE=/private/tmp/openclaw-skill-sync-validate/current-mac-cache \
PREVIOUS_INVENTORY=/private/tmp/openclaw-skill-sync-validate/openclaw-inventory-current.json \
  scripts/openclaw-reconcile-readonly.sh /private/tmp/openclaw-reconcile-$(date +%Y%m%d%H%M%S)
```

Set `REMOTE_CACHE` to reuse a known complete WebDAV cache and avoid slow full archive downloads on every OpenClaw inventory check. Omit `REMOTE_CACHE` when a fresh WebDAV pull is required.

The script writes both the reconcile report and a machine-readable gate result:

```text
<out>/reconcile/reconcile-report.json
<out>/openclaw-gate.json
<out>/openclaw-gate.txt
```

Check the latest known OpenClaw gate without SSH:

```bash
PYTHONPATH=src python3 -m skill_sync_sidecar openclaw-gate --fail-on-blocked
```

Gate behavior:

- Passes when `safe_to_auto_apply=true`, `conflict=0`, `local_new=0`, and `changed_since_previous=0`.
- Blocks when OpenClaw has local-only skills, conflicts, or fresh changes since the previous inventory.
- Does not SSH, pull WebDAV, apply files, or write to `/home/admin/clawd/skills`; it only reads existing local reports.

Restricted OpenClaw live `sync-probe` apply:

```bash
ssh root@oc-vps-aliyun-us \
  "python3 - --prefix skill-sync-sidecar-dev/sync-probe-YYYYMMDDHHMMSS --out /tmp/skill-sync-sidecar-validate/sync-probe-live --skill-id sync-probe --apply-root /home/admin/clawd/skills --yes-apply" \
  < scripts/openclaw-sync-probe-py36.py
```

Safety behavior:

- `--apply-root` refuses to write unless `--yes-apply` is also present.
- Live apply is restricted to `skill_id=sync-probe`.
- The archive is hash-validated before and after install.
- The installed tree owner is aligned to the OpenClaw skill root owner.
- The apply record is written under `/home/admin/clawd/skills/.skill-sync-backups/...`.

After validating live discovery, move `/home/admin/clawd/skills/sync-probe` into that apply backup directory so normal `current-mac` reconcile does not report `local_new=sync-probe`.

Runtime-state handling:

- Runtime state under `data/session-timers/` and `data/session-archives/` is excluded by default.
- If a package still shows conflict after these paths are removed, treat it as real source drift.
- For `session-lifetime-manager`, the remaining drift is source-code files, not timer JSON state.

Conflict review flow:

1. Run `reconcile-report` or `scripts/openclaw-reconcile-readonly.sh`.
2. Start with `skill_md_only` conflicts. These are usually documentation/front-matter governance changes and are the safest adoption candidates.
3. Then review `code_or_config`; require code-level diff review before push or pull.
4. Review `mixed_with_code` last because these combine docs, scripts, and OpenClaw-only files.
5. Do not update the shared WebDAV baseline while a writable Mac daemon is running unless the daemon's scanner version and base record are coordinated.

Large-asset exception:

- A `skill_md_only` change can still require a large archive upload when the skill directory contains binary assets.
- `ocr` and `finance-auto-bookkeeping` are examples: small source changes can produce multi-MB archives because the package contains binary assets or data fixtures.
- If direct WebDAV upload of such a package times out, do not publish an index that points to the missing archive. Use the local WebDAV sync folder file-remote path above, or defer the skill until a per-file/delta strategy exists.
- Current adoption status: the OpenClaw peer-writer conflicts were reviewed and adopted into `adopt-openclaw-conflicts-complete-20260613`; the P0, P1 Wave-1/Wave-2/Wave-3/Wave-4/Wave-5/Wave-6/Wave-7/Wave-8/Wave-9, and P2a Wave-1/Wave-2/Wave-3 allowlists were later installed on OpenClaw and the current reconcile report shows `safe_to_auto_apply=true`, `same_without_base=70`, `pull_new=22`, and no conflicts.

Before enabling it:

1. Keep `--dry-run` until the state file shows expected plans over multiple cycles.
2. Verify the service user can read cc-switch WebDAV settings or provide env credentials.
3. Verify the service user's Python runtime is >=3.9.
4. Keep remote service connectivity checks separate from sidecar rollout.
5. Do not enable full live apply while `pull_new=22` still requires review.
6. Review any `conflict` actions before allowing writes to `/home/admin/clawd/skills`.

Suggested validation:

```bash
systemd-analyze verify examples/systemd/skill-sync-sidecar.service
```

Install only after editing a copied unit:

```bash
mkdir -p "$HOME/.config/systemd/user"
cp examples/systemd/skill-sync-sidecar.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user start skill-sync-sidecar.service
systemctl --user status skill-sync-sidecar.service
cat "$HOME/.local/state/skill-sync-sidecar/state.json"
```

## Promotion Checklist

- `python3 -m unittest tests/test_scanner.py` passes.
- `python3 -m compileall -q src tests` passes.
- `scripts/webdav-smoke.sh ...` passes against a dev prefix.
- `sync-daemon --dry-run --max-cycles 1 --state-file ...` writes a healthy state file.
- First real `--yes` run targets a temporary root or a reviewed project root.
- Production prefix, retention, and conflict review policy are documented before enabling unattended writes.
