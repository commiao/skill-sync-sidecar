# Skill Sync Sidecar Acceptance Report

Date: 2026-06-12

## Scope

This report covers the current MVP acceptance state for Skill Sync Sidecar:

- scan and package local skills
- upload and pull synthetic WebDAV snapshots
- run safe `sync-cycle` and `sync-daemon`
- produce conflict/tombstone review material
- build an installable Python wheel
- provide launchd/systemd rollout templates

## Local Test Gate

```text
python3 -m unittest tests/test_scanner.py
Ran 52 tests
OK
```

```text
python3 -m compileall -q src tests
OK
```

## Packaging Gate

```text
python3 -m pip wheel . --no-deps --no-build-isolation
Created wheel: skill_sync_sidecar-0.1.0-py3-none-any.whl
```

Temporary venv install was validated:

```text
skill-sync --version
skill-sync 0.1.0
```

## Synthetic WebDAV Gate

Final smoke prefix:

```text
skill-sync-sidecar-dev/final-smoke-20260611204645
```

Result:

```text
webdav_smoke_ok=true
canary_prefix=skill-sync-sidecar-dev/final-smoke-20260611204645/canary
cycle_prefix=skill-sync-sidecar-dev/final-smoke-20260611204645/cycle
daemon_prefix=skill-sync-sidecar-dev/final-smoke-20260611204645/daemon
```

Coverage:

- zero-skill canary upload, remote-status, and pull-cache
- synthetic `sync-cycle --yes` install into `/private/tmp`
- synthetic `sync-daemon --yes --max-cycles 1` install into `/private/tmp`
- daemon state-file existence check

## Real Local Root Non-Network Gate

Command:

```text
scripts/local-real-dryrun.sh /Users/mac/.cc-switch/skills
```

Result:

```text
local_real_dryrun_ok=true
base=/private/tmp/skill-sync-real-local-file-dryrun-20260611205417
snapshot_total=91
push_files=92
daemon_cycles_run=1
daemon_cycle_status=dry_run
daemon_cycle_summary={"noop": 91}
state_daemon_status=complete
```

This validates the real local `~/.cc-switch/skills` directory through a file-backed remote. It does not upload private skill content to WebDAV and does not write into the source root.

## Real Private Skill WebDAV Gate

Explicit approval was granted to upload private skills to WebDAV.

Command shape:

```text
snapshot ~/.cc-switch/skills
push --cc-switch-webdav --prefix skill-sync-sidecar-dev/mac-real-20260611205923 --yes
remote-status
pull-cache
sync-daemon --dry-run --max-cycles 1 --state-file
```

Result:

```text
base=/private/tmp/skill-sync-real-webdav-20260611205923
prefix=skill-sync-sidecar-dev/mac-real-20260611205923
snapshot_total=91
push_files=92
push_bytes=22411123
remote_status_ok=true
remote_snapshot_id=mac-real-webdav
remote_total=91
pull_total=91
daemon_cycles_run=1
daemon_cycle_status=dry_run
daemon_cycle_summary={"noop": 91}
state_daemon_status=complete
```

Reusable command:

```bash
SKILL_SYNC_ALLOW_PRIVATE_WEBDAV_UPLOAD=1 \
  scripts/real-webdav-dryrun.sh "$HOME/.cc-switch/skills" "skill-sync-sidecar-dev/real-$(hostname -s)-$(date +%Y%m%d%H%M%S)"
```

## Current Device Launchd Gate

The current macOS device has been installed as a launchd user service in writable mode against the sidecar dev prefix.

Command:

```bash
SKILL_SYNC_ALLOW_PRIVATE_WEBDAV_UPLOAD=1 \
SKILL_SYNC_DAEMON_MODE=yes \
SKILL_SYNC_PREFIX=skill-sync-sidecar-dev/current-mac \
  scripts/install-current-launchd.sh
```

Installed service:

```text
plist=/Users/mac/Library/LaunchAgents/com.skill-sync-sidecar.plist
state_file=/Users/mac/Library/Application Support/skill-sync-sidecar/state.json
base_record_file=/Users/mac/Library/Application Support/skill-sync-sidecar/base-record.json
prefix=skill-sync-sidecar-dev/current-mac
mode=yes
interval_seconds=300
```

Launchd status:

```text
state=running
arguments include:
  sync-daemon
  --local-root /Users/mac/.cc-switch/skills
  --cc-switch-webdav
  --prefix skill-sync-sidecar-dev/current-mac
  --last-applied-record /Users/mac/Library/Application Support/skill-sync-sidecar/base-record.json
  --base-record-file /Users/mac/Library/Application Support/skill-sync-sidecar/base-record.json
  --yes
```

Remote status:

```text
remote_status_ok=true
snapshot_id=current-MacBook-Pro-2
remote_total=91
```

State file:

```text
daemon_status=running
dry_run=false
cycles_run>=1
summary={"noop": 91}
blocked=0
conflicts=0
tombstones=0
applied=0
uploaded=0
```

Recovery validation after a partial WebDAV write:

```text
snapshot_id=repair-current-mac-20260612
daemon_status=running
cycles_run=1
active_cycle=None
summary={"noop": 91}
blocked=0
conflicts=0
tombstones=0
applied=0
uploaded=0
```

The recovery fixed a remote state where `index.json` pointed at a missing
`session-lifetime-manager` archive. The repaired uploader now writes archives
before `index.json`, reuses cached downloads, skips already-present
content-addressed archives, falls back from unstable WebDAV `HEAD` requests to
`PROPFIND`, and records a daemon heartbeat before each cycle.

Low-risk OpenClaw `skill_md_only` adoption:

```text
adopted=9
deferred=ocr
snapshot_id=adopt-skill-md-only-9-20260612
daemon_cycle_summary={"noop": 91}
```

The adopted skills are:

```text
beijing-recruitment
feishu-keyword-responder
finance-auto
gold-analysis
model-router
role-maintainer
social-search
task-complete-summary
wechat-publisher
```

`ocr` was deferred because its archive is 4.5MB and contains OCR traineddata
assets; the WebDAV provider repeatedly timed out while uploading it. It needs a
large-asset or per-file delta strategy before automatic adoption.

OpenClaw reconcile after adopting the 9 low-risk skills:

```text
summary={"conflict": 12, "local_new": 1, "remote_new": 60, "same_without_base": 19}
changed_since_previous=1
remaining_skill_md_only=["ocr"]
remaining_code_or_config=["session-knowledge-manager", "session-lifetime-manager", "trigger-manager"]
remaining_mixed_with_code=["daily-report", "evoskill", "feishu-image-sender", "finance-auto-bookkeeping", "liblibai-skill", "puter-image-gen", "smart-reporter", "task-splitter"]
local_new=["lark-cli-adapter"]
```

## OpenClaw Conflict Adoption Complete

On 2026-06-13, the OpenClaw peer-writer drift was reviewed skill by skill and adopted into the shared `current-mac` snapshot after each package passed manual diff review.

Adopted groups:

```text
skill_md_only:
  ocr
code_or_config:
  session-knowledge-manager, session-lifetime-manager, trigger-manager
mixed_with_code:
  daily-report, evoskill, feishu-image-sender, finance-auto-bookkeeping,
  liblibai-skill, puter-image-gen, smart-reporter, task-splitter
new shared package:
  lark-cli-adapter
```

Most changes remove hardcoded Feishu credentials, direct OpenAPI token flows, webhook-style sending, or shell-built `curl` calls, replacing them with `lark-cli-adapter` or safer `lark-cli` subprocess calls. `task-splitter` also adds read-only command shims and explicit write gates for `active-tasks.md`.

Direct HTTP(S) WebDAV `PUT` timed out on larger archives, so the installed Mac daemon was switched to the local WebDAV sync folder as a file remote:

```text
remote=file:///Users/mac/public-sync
prefix=skill-sync-sidecar-dev/current-mac
launchd=/Users/mac/Library/LaunchAgents/com.skill-sync-sidecar.plist
snapshot_id=adopt-openclaw-conflicts-complete-20260613
daemon_cycle_summary={"noop": 92}
```

Final validation:

```text
tests=53 passed
python_py_compile=passed for adopted Python entrypoints
node_check=passed for adopted Puter JavaScript entrypoints
local_sync_plan={"noop": 92}
final_reconcile_safe_to_auto_apply=true
final_reconcile_summary={"remote_new": 60, "same_without_base": 32}
changed_since_previous=0
report=/private/tmp/openclaw-skill-sync-validate/reconcile-20260613-complete/reconcile/reconcile-report.md
```

## OpenClaw Incremental Drift Adoption 2026-06-14

OpenClaw continued optimizing skills after the baseline was clean. A fresh read-only reconcile found three changed skills:

```text
initial_report=/private/tmp/openclaw-skill-sync-validate/reconcile-20260614-fresh/reconcile/reconcile-report.md
summary={"conflict": 3, "remote_new": 60, "same_without_base": 29}
changed_since_previous=3
changed_skills=liblibai-skill, puter-image-gen, session-lifetime-manager
```

Reviewed and adopted changes:

```text
liblibai-skill/send-to-feishu.sh:
  chat_id fallback changed to selector/category routing; dry-run/shadow supported.
puter-image-gen/{auto-upload-xvfb.sh,quick-upload.sh,upload-to-feishu.sh}:
  direct lark-cli calls moved to lark-cli-adapter file sending with selector support.
session-lifetime-manager/src/{auto_execution_reporter.py,auto_task_dispatcher.py}:
  hardcoded chat_id routing moved to adapter selectors/routes with legacy chat_id compatibility.
```

Validation:

```text
snapshot_id=adopt-openclaw-drift-3-20260614
daemon_cycle_summary={"noop": 92}
local_sync_plan={"noop": 92}
tests=53 passed
bash_n=passed for adopted shell entrypoints
python_py_compile=passed for adopted Python entrypoints
final_report=/private/tmp/openclaw-skill-sync-validate/reconcile-20260614-after-drift-3/reconcile/reconcile-report.md
final_reconcile_safe_to_auto_apply=true
final_reconcile_summary={"remote_new": 60, "same_without_base": 32}
changed_since_previous=0
```

## OpenClaw Second-Node Read-Only Gate

OpenClaw was validated as a second node without modifying service state or skill files.

Connectivity:

```text
host=oc-vps-aliyun-us
ssh_user=root
hostname=iZ0xi1l67zzk3qgaail2zvZ
date=Thu Jun 11 22:15:31 CST 2026
```

Runtime and roots:

```text
system_python=Python 3.6.8
python_path=/usr/bin/python3
docker=/usr/bin/docker
podman=/usr/bin/podman
openclaw_skill_root=/home/admin/clawd/skills
admin_cc_switch_skill_root=/home/admin/.cc-switch/skills
root_cc_switch_skill_root=/root/.cc-switch/skills
```

Important configuration finding:

```text
/home/admin/.cc-switch/settings.json has webdavSync.enabled=true
/root/.cc-switch/settings.json exists but does not contain the WebDAV credentials
```

This means a root-owned systemd service using `--cc-switch-webdav` would not see the admin user's WebDAV settings unless the service runs as `admin` or receives explicit environment credentials. This is the likely configuration trap for OpenClaw-side automation.

Because the host only has Python 3.6.8 and sidecar requires Python >=3.9, full sidecar execution was not started on OpenClaw. Instead, `scripts/remote-inventory-py36.py` was run over SSH as a read-only compatibility probe to compute sidecar-compatible package hashes.

OpenClaw inventory:

```text
source=openclaw
root=/home/admin/clawd/skills
total=32
ok=19
warning=13
error=0
duplicates=0
```

Comparison against WebDAV prefix `skill-sync-sidecar-dev/current-mac`:

```text
remote_snapshot_id=current-MacBook-Pro-2
remote_total=91
comparison_total=92
same_without_base=24
remote_new=60
local_new=1
conflict=7
```

Conflicts requiring manual review before any OpenClaw apply:

```text
daily-report
feishu-image-sender
liblibai-skill
puter-image-gen
session-knowledge-manager
session-lifetime-manager
smart-reporter
```

File-level conflict summary:

```text
daily-report: lib/feishu_client.py
feishu-image-sender: index.js, scripts/send-to-feishu.sh
liblibai-skill: send-to-feishu.sh
puter-image-gen: 9 changed script files
session-knowledge-manager: src/main.js, src/triggers/keyword-trigger.js
session-lifetime-manager: generated data/session-timers drift; should be excluded before apply
smart-reporter: main.py
```

`model-router` initially appeared as a conflict because OpenClaw had a local `.encryption-key`. Sidecar now excludes secret-like files such as `.encryption-key`, `.env*`, `*.pem`, and `*.key` by default; after recalculating OpenClaw inventory, `model-router` became `same_without_base`.

OpenClaw-only package:

```text
lark-cli-adapter
```

## OpenClaw Peer-Writer Reconcile Gate

OpenClaw is now treated as a peer writer because users and agents continue to optimize `/home/admin/clawd/skills` directly. Before any OpenClaw apply or daemon rollout, use `reconcile-report` instead of relying on a stale dry-run.

Current command:

```bash
PYTHONPATH=src python3 -m skill_sync_sidecar reconcile-report \
  --local-inventory /private/tmp/openclaw-skill-sync-validate/openclaw-inventory-current.json \
  --remote-snapshot /private/tmp/openclaw-skill-sync-validate/current-mac-cache \
  --previous-local-inventory /private/tmp/openclaw-skill-sync-validate/openclaw-inventory-files-after-secret-exclude.json \
  --label openclaw-current-20260612 \
  --out /private/tmp/openclaw-skill-sync-validate/current-reconcile
```

Current result on 2026-06-12:

```text
local=32
remote=91
safe_to_auto_apply=false
conflict=20
local_new=1
remote_new=60
same_without_base=11
changed_since_previous=20
json=/private/tmp/openclaw-skill-sync-validate/current-reconcile/reconcile-report.json
markdown=/private/tmp/openclaw-skill-sync-validate/current-reconcile/reconcile-report.md
```

This means OpenClaw has active drift and must not be treated as a downstream-only target. The next safe step is to review the 20 conflict packages by skill, add package-specific excludes for generated state such as `session-lifetime-manager/data/session-timers`, then decide which OpenClaw changes should be pushed back to WebDAV.

Reusable read-only script validation:

```bash
PYTHON_BIN=/Users/mac/.pyenv/shims/python3 \
REMOTE_CACHE=/private/tmp/openclaw-skill-sync-validate/current-mac-cache \
PREVIOUS_INVENTORY=/private/tmp/openclaw-skill-sync-validate/openclaw-inventory-current.json \
  scripts/openclaw-reconcile-readonly.sh /private/tmp/openclaw-skill-sync-validate/reconcile-script-cache-run-20260612
```

Result:

```text
local=32
remote=91
safe_to_auto_apply=false
conflict=20
local_new=1
remote_new=60
same_without_base=11
changed_since_previous=4
changed_since_previous_skills=daily-report, feishu-image-sender, lark-cli-adapter, session-lifetime-manager
json=/private/tmp/openclaw-skill-sync-validate/reconcile-script-cache-run-20260612/reconcile/reconcile-report.json
markdown=/private/tmp/openclaw-skill-sync-validate/reconcile-script-cache-run-20260612/reconcile/reconcile-report.md
```

## Runtime-State Noise Gate

`session-lifetime-manager` contained volatile runtime state under `data/session-timers` and `data/session-archives`. These paths are now excluded by default in both the main scanner and the Python 3.6 OpenClaw inventory helper.

Validation:

```text
tests=48 passed
compileall=passed
py36_helper_compile=passed
```

Candidate reconcile against a regenerated Mac snapshot, without pushing to WebDAV:

```text
snapshot=/private/tmp/openclaw-skill-sync-validate/candidate-current-mac-cache
openclaw_inventory=/private/tmp/openclaw-skill-sync-validate/openclaw-inventory-runtime-excluded.json
report=/private/tmp/openclaw-skill-sync-validate/reconcile-runtime-excluded-20260612/reconcile-report.json
local=32
remote=91
safe_to_auto_apply=false
conflict=20
local_new=1
remote_new=60
same_without_base=11
```

The conflict count did not drop because `session-lifetime-manager` also has real source-code drift. The noise did drop sharply:

```text
before: changed=18, remote_only=75, local_only=78
after:  changed=6,  remote_only=0,  local_only=0
remaining_changed=src/auto_execution_reporter.py, src/auto_task_dispatcher.py, src/executor_auto_v2.py, src/feishu_reaction.py, src/send_feishu_messages.py, src/send_feishu_reply.py
```

## Conflict Review Package

All 20 current conflicts have been grouped and materialized for review:

```text
review_dir=/private/tmp/openclaw-skill-sync-validate/all-conflict-review
summary=/private/tmp/openclaw-skill-sync-validate/all-conflict-review/conflict-review-summary.json
markdown=/private/tmp/openclaw-skill-sync-validate/all-conflict-review/README.md
```

Groups:

```text
skill_md_only=10
  beijing-recruitment, feishu-keyword-responder, finance-auto, gold-analysis, model-router, ocr, role-maintainer, social-search, task-complete-summary, wechat-publisher
code_or_config=2
  session-knowledge-manager, session-lifetime-manager
mixed_with_code=8
  daily-report, evoskill, feishu-image-sender, finance-auto-bookkeeping, liblibai-skill, puter-image-gen, smart-reporter, task-splitter
```

The `skill_md_only` diffs were sampled and appear to be OpenClaw-side governance improvements: front matter additions, `lark-cli-adapter` channel standardization, and one `model-router` formatting fix. They are good candidates for adopting OpenClaw's `SKILL.md` into the shared WebDAV baseline after the daemon write path is paused or explicitly coordinated.

Safe next gate:

1. Do not run `sync-daemon --yes` on OpenClaw until conflicts are reviewed.
2. Decide service identity: prefer running as `admin`, or provide explicit WebDAV env credentials to a root service.
3. Provide a Python >=3.9 runtime path on OpenClaw before installing the full sidecar daemon.
4. After runtime is available, run one OpenClaw `sync-daemon --dry-run --max-cycles 1` against `/home/admin/clawd/skills` and `skill-sync-sidecar-dev/current-mac`.
5. Only after that dry-run shows the intended plan should OpenClaw move to supervised apply.

## 2026-06-14 Live OpenClaw Gate

OpenClaw continued to receive direct skill optimizations. A fresh read-only gate found three new conflicts:

```text
report=/private/tmp/openclaw-skill-sync-validate/reconcile-20260614-live-gate/reconcile/reconcile-report.json
safe_to_auto_apply=false
conflict=3
remote_new=60
same_without_base=29
changed_since_previous=3
changed=daily-report, puter-image-gen, session-lifetime-manager
```

The changed files were reviewed locally. The diffs replace hard-coded `oc_*` chat IDs with portable `category:*` selectors:

```text
daily-report/lib/token_counter.py
daily-report/src/detection_report.py
puter-image-gen/FEISHU_UPLOAD_FINAL.md
session-lifetime-manager/examples/feishu_integration.py
session-lifetime-manager/src/send_feishu_reply.py
```

Those OpenClaw updates were adopted into the Mac source root and pushed through the file-backed WebDAV sidecar path:

```text
sync-daemon --yes --max-cycles 1
cycle=complete
summary={"noop": 89, "push": 3}
blocked=0
```

Final read-only OpenClaw reconcile after adoption:

```text
report=/private/tmp/openclaw-skill-sync-validate/reconcile-20260614-after-live-adopt/reconcile/reconcile-report.json
safe_to_auto_apply=true
summary={"remote_new": 60, "same_without_base": 32}
changed_since_previous=0
```

Final ops gate:

```text
remote_snapshot=20260614T133326.883987Z
sync_summary={"noop": 92}
openclaw_gate=ok
overall_ok=true
```

## 2026-06-15 OpenClaw Stable Optimization Adoption

After the OpenClaw skill optimization work stabilized, a fresh read-only reconcile still showed 8 conflicts:

```text
report=/private/tmp/openclaw-skill-sync-validate/reconcile-20260615-after-skill-work-settled/reconcile/reconcile-report.json
safe_to_auto_apply=false
summary={"conflict": 8, "remote_new": 60, "same_without_base": 24}
changed_since_previous=0
```

Those 8 stable OpenClaw updates were reviewed, backed up, adopted into the Mac canonical root, and synced to the `skill-sync-sidecar-dev/current-mac` WebDAV snapshot.

```text
adopted_files=19
backup_root=/Users/mac/.cc-switch/skills/.skill-sync-backups/openclaw-adopt-20260615-193256
remote_snapshot_id=20260615T113322.799109Z
remote_total=92
```

Validation gates:

```text
python_compile=ok
node_check=ok
hash_match=19
sync_summary={"noop": 92}
```

Final read-only OpenClaw reconcile after adoption:

```text
report=/private/tmp/openclaw-skill-sync-validate/reconcile-after-openclaw-adoption-20260615-1933/reconcile/reconcile-report.json
safe_to_auto_apply=true
summary={"remote_new": 60, "same_without_base": 32}
changed_since_previous=0
openclaw_gate=ok
```

Known content follow-up: several adopted OpenClaw skills still carry `/home/admin/clawd/...` fallbacks behind environment variable overrides. This is acceptable for the sync-mechanism gate but should be normalized during the next skill-content optimization pass.

## 2026-06-15 OpenClaw Live `sync-probe` Apply Gate

The OpenClaw live-root apply gate was validated with the synthetic `sync-probe` only, not the full 92-skill snapshot. The Python 3.6 compatible bridge script was extended with an explicit `--apply-root ... --yes-apply` mode that refuses non-`sync-probe` live apply.

```text
local_report=/private/tmp/openclaw-sync-probe-live-20260615201345.json
remote_out=/tmp/skill-sync-sidecar-validate/sync-probe-live-20260615201345
snapshot_id=sync-probe-v2-mac
content_hash=eadf364359152305228dfd63017bc25f702170a95b48e2237b6a6640629f513a
actual_hash=eadf364359152305228dfd63017bc25f702170a95b48e2237b6a6640629f513a
target_path=/home/admin/clawd/skills/sync-probe
apply_record=/home/admin/clawd/skills/.skill-sync-backups/openclaw-sync-probe-20260615201350/apply-record.json
previous_exists=False
```

OpenClaw verified the installed skill:

```text
owner=admin:admin
files=SKILL.md, notes/probe.txt
inventory_total=33
sync_probe_found=1
file_count=2
```

The test skill was then moved out of the live root into the apply backup directory to keep the normal `current-mac` gate clean:

```text
moved_to=/home/admin/clawd/skills/.skill-sync-backups/openclaw-sync-probe-20260615201350/sync-probe-live-cleanup
cleanup_record=/home/admin/clawd/skills/.skill-sync-backups/openclaw-sync-probe-20260615201350/cleanup-record.json
```

Post-cleanup reconcile:

```text
report=/private/tmp/openclaw-skill-sync-validate/reconcile-after-live-sync-probe-cleanup-20260615201513/reconcile/reconcile-report.json
safe_to_auto_apply=true
summary={"remote_new": 60, "same_without_base": 32}
changed_since_previous=0
openclaw_gate=ok
```

## 2026-06-15 OpenClaw Isolated Python Runtime

OpenClaw keeps the system Python untouched:

```text
/usr/bin/python3 -> Python 3.6.8
```

An isolated sidecar runtime was installed under `/opt/skill-sync-sidecar`:

```text
uv=0.11.21
uv_sha256=8c88519b0ef0af9801fcdee419bbb12116bd9e6b18e162ae093c932d8b264050
python=/opt/skill-sync-sidecar/python/cpython-3.11-linux-x86_64-gnu/bin/python3.11
python_version=3.11.15
venv=/opt/skill-sync-sidecar/venv
skill-sync=0.1.2
wheel_sha256=01ada3f2f5ab3bd72d424a148726daf6644a7e1944c9736da0c4da810b9e090f
```

The sidecar wheel was installed offline from the copied wheel artifact. No system Python package or global interpreter path was replaced.

OpenClaw one-cycle daemon dry-run using the isolated runtime:

```text
command_user=admin
local_root=/home/admin/clawd/skills
remote_prefix=skill-sync-sidecar-dev/current-mac
state_file=/opt/skill-sync-sidecar/state/openclaw-daemon-dryrun-state.json
snapshot_id=20260615T113322.799109Z
cycle_status=dry_run
summary={"noop": 32, "pull_new": 60}
blocked=0
conflicts=0
applied=0
uploaded=0
```

The 60 `pull_new` entries are expected because OpenClaw intentionally has 32 installed skills while the canonical Mac/WebDAV snapshot has 92. This dry-run proves runtime and WebDAV compatibility only; it is not approval for full live apply.

## Safety Boundary

Uploading the real `~/.cc-switch/skills` snapshot to WebDAV is now validated only under a sidecar dev prefix after explicit approval. Official or production prefixes remain a separate decision.

Allowed without further approval:

- synthetic WebDAV smoke data under `skill-sync-sidecar-dev/...`
- real local root dry-run using a file-backed remote under `/private/tmp`
- daemon `--dry-run` against already-approved remote snapshots

Requires explicit approval:

- running `sync-daemon --yes` against a real tool root
- installing launchd/systemd services that write to real roots
- using the official `cc-switch-sync` prefix

## Current Readiness

Ready:

- source-tree usage with `PYTHONPATH=src`
- wheel build and install
- synthetic WebDAV validation
- real local root non-network dry-run
- real private skill WebDAV dry-run under sidecar dev prefix
- launchd/systemd template customization
- current macOS user launchd service running in `--yes` mode against `skill-sync-sidecar-dev/current-mac`
- OpenClaw second-node read-only inventory and WebDAV comparison
- OpenClaw stable optimization adoption into Mac/WebDAV canonical snapshot
- OpenClaw read-only gate passing with zero conflicts and no drift since the settled inventory
- OpenClaw live-root `sync-probe` apply, scan verification, and audit-preserving cleanup
- OpenClaw isolated Python 3.11 runtime and sidecar v0.1.2 installation under `/opt/skill-sync-sidecar`
- OpenClaw one-cycle daemon dry-run as `admin` using the isolated runtime

Not yet enabled:

- destructive delete propagation
- official production prefix usage
- OpenClaw full writable sidecar daemon
- OpenClaw live-root apply beyond the narrow `sync-probe` validation
