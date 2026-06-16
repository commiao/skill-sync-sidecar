# OpenClaw Admission Report - 2026-06-15

Purpose: classify the 60 `pull_new` skills from the Mac/WebDAV canonical snapshot before any OpenClaw live apply.

This report is dry-run only. It does not install skills.

## Snapshot

```text
remote_snapshot_id=20260615T113322.799109Z
remote_total=92
remote_new_total=60
summary={'p0_candidate': 8, 'p1_review': 18, 'p2_defer': 34}
```

## Admission Policy

- `p0_candidate`: small, low-risk, or OpenClaw-native; eligible for a tiny supervised batch.
- `p1_review`: potentially useful but needs human review before install.
- `p2_defer`: large, warning-risk, browser/deploy/setup/automation-heavy, or otherwise unsuitable for first live batch.

## P0 Candidates

These were the initial candidates for the first supervised OpenClaw live batch.

| Skill | Risk | Files | Size | Reason |
| --- | --- | ---: | ---: | --- |
| aliyun-sls-query | ok | 1 | 4086 | small low-risk package |
| codeup-personal-git | ok | 3 | 3109 | small low-risk package |
| freeze | ok | 3 | 8861 | small low-risk package |
| gstack-openclaw-ceo-review | ok | 1 | 10576 | OpenClaw-native skill |
| gstack-openclaw-investigate | ok | 1 | 5469 | OpenClaw-native skill |
| gstack-openclaw-office-hours | ok | 1 | 16964 | OpenClaw-native skill |
| gstack-openclaw-retro | ok | 1 | 9773 | OpenClaw-native skill |
| unfreeze | ok | 2 | 2868 | small low-risk package |

## P1 Review

| Skill | Risk | Files | Size | Reason |
| --- | --- | ---: | ---: | --- |
| autoplan | ok | 2 | 125745 | needs human review before OpenClaw install |
| codex | ok | 2 | 97503 | needs human review before OpenClaw install |
| context-restore | ok | 2 | 40968 | needs human review before OpenClaw install |
| context-save | ok | 2 | 49203 | needs human review before OpenClaw install |
| design-consultation | ok | 2 | 98672 | needs human review before OpenClaw install |
| design-html | ok | 3 | 113939 | needs human review before OpenClaw install |
| design-shotgun | ok | 2 | 69721 | needs human review before OpenClaw install |
| hackernews-frontpage | ok | 5 | 26390 | needs human review before OpenClaw install |
| investigate | ok | 2 | 52994 | needs human review before OpenClaw install |
| learn | ok | 2 | 41532 | needs human review before OpenClaw install |
| make-pdf | ok | 18 | 134519 | needs human review before OpenClaw install |
| mcp-builder | ok | 10 | 121756 | needs human review before OpenClaw install |
| office-hours | ok | 2 | 162577 | needs human review before OpenClaw install |
| pdf | ok | 12 | 58692 | needs human review before OpenClaw install |
| plan-design-review | ok | 2 | 124286 | needs human review before OpenClaw install |
| plan-tune | ok | 2 | 61528 | needs human review before OpenClaw install |
| review | ok | 13 | 140241 | needs human review before OpenClaw install |
| using-superpowers | ok | 4 | 12678 | needs human review before OpenClaw install |

## P2 Defer

| Skill | Risk | Files | Size | Reason |
| --- | --- | ---: | ---: | --- |
| benchmark | ok | 2 | 38939 | tooling, browser, deploy, setup, or automation side effects |
| benchmark-models | ok | 2 | 32261 | tooling, browser, deploy, setup, or automation side effects |
| browse | warning | 154 | 30463312 | risk=warning |
| browser | ok | 9 | 17102 | tooling, browser, deploy, setup, or automation side effects |
| canary | ok | 2 | 49354 | tooling, browser, deploy, setup, or automation side effects |
| careful | warning | 3 | 9236 | risk=warning |
| claude-mem | warning | 13 | 269565 | risk=warning |
| cso | warning | 3 | 107523 | risk=warning |
| design-review | ok | 2 | 101539 | tooling, browser, deploy, setup, or automation side effects |
| devex-review | ok | 2 | 65891 | tooling, browser, deploy, setup, or automation side effects |
| document-release | ok | 2 | 64047 | tooling, browser, deploy, setup, or automation side effects |
| find-skills | ok | 1 | 5446 | tooling, browser, deploy, setup, or automation side effects |
| gstack | ok | 409 | 6244099 | large package or many files |
| gstack-upgrade | warning | 7 | 34482 | risk=warning |
| guard | warning | 2 | 6458 | risk=warning |
| health | ok | 2 | 53737 | tooling, browser, deploy, setup, or automation side effects |
| land-and-deploy | ok | 2 | 130596 | tooling, browser, deploy, setup, or automation side effects |
| landing-report | ok | 2 | 45007 | tooling, browser, deploy, setup, or automation side effects |
| open-gstack-browser | ok | 2 | 48050 | tooling, browser, deploy, setup, or automation side effects |
| pair-agent | ok | 2 | 49706 | tooling, browser, deploy, setup, or automation side effects |
| plan-ceo-review | warning | 2 | 184389 | risk=warning |
| plan-devex-review | warning | 3 | 139351 | risk=warning |
| plan-eng-review | warning | 2 | 118281 | risk=warning |
| pua | ok | 25 | 160286 | tooling, browser, deploy, setup, or automation side effects |
| qa | ok | 4 | 85007 | tooling, browser, deploy, setup, or automation side effects |
| qa-only | ok | 2 | 54489 | tooling, browser, deploy, setup, or automation side effects |
| retro | ok | 2 | 112392 | tooling, browser, deploy, setup, or automation side effects |
| scrape | ok | 2 | 43079 | tooling, browser, deploy, setup, or automation side effects |
| setup-browser-cookies | ok | 2 | 25634 | tooling, browser, deploy, setup, or automation side effects |
| setup-deploy | ok | 2 | 45925 | tooling, browser, deploy, setup, or automation side effects |
| setup-gbrain | warning | 3 | 82685 | risk=warning |
| ship | ok | 2 | 195086 | tooling, browser, deploy, setup, or automation side effects |
| skill-creator | ok | 18 | 224992 | tooling, browser, deploy, setup, or automation side effects |
| skillify | ok | 2 | 62861 | tooling, browser, deploy, setup, or automation side effects |

## Initial Next Gate

1. Review the P0 list and select a tiny first batch.
2. Run an isolated target-root apply test for that batch, not `/home/admin/clawd/skills`.
3. Run OpenClaw live apply only after an explicit allowlist exists.
4. Keep `openclaw-skill-sync-sidecar-dryrun.service` in `--dry-run` mode until full admission is complete.

## P0 Isolated Apply Validation

The P0 candidate set was materialized into a filtered snapshot:

```text
snapshot_id=openclaw-admission-p0-20260615
total=8
local_snapshot=/private/tmp/openclaw-admission-p0-snapshot-20260615
```

Local isolated target validation:

```text
target=/private/tmp/openclaw-admission-p0-local-20260615233256/target
stage=8
apply_dry_run=8
apply=8
scan=8
```

OpenClaw isolated target validation:

```text
snapshot=/tmp/openclaw-admission-p0-snapshot-20260615
target=/tmp/openclaw-admission-p0-validate-20260615/target
stage=8
apply_dry_run=8
apply=8
scan=8
skill_md_count=8
```

Validated skills:

```text
aliyun-sls-query
codeup-personal-git
freeze
gstack-openclaw-ceo-review
gstack-openclaw-investigate
gstack-openclaw-office-hours
gstack-openclaw-retro
unfreeze
```

Post-validation live-root reconcile stayed clean:

```text
report=/private/tmp/openclaw-skill-sync-validate/reconcile-after-p0-isolated-20260615233355/reconcile/reconcile-report.json
safe_to_auto_apply=true
summary={"remote_new": 60, "same_without_base": 32}
changed_since_previous=0
```

## P0 Live Allowlist Apply

On 2026-06-16, the P0 allowlist was applied to the live OpenClaw skill root after both isolated validations passed. The dry-run systemd service was stopped only during the supervised apply and was restarted immediately afterward.

The first live attempt failed before installing any skill because the existing backup directory was root-owned:

```text
backup_dir=/home/admin/clawd/skills/.skill-sync-backups
before_owner=root:root
failure=permission denied while creating .skill-sync-backups/<timestamp>
installed=0
```

The backup directory ownership was corrected to match the OpenClaw skill root owner:

```text
backup_dir=/home/admin/clawd/skills/.skill-sync-backups
after_owner=admin:admin
mode=755
```

The second live apply succeeded:

```text
snapshot=/tmp/openclaw-admission-p0-snapshot-20260615
target=/home/admin/clawd/skills
state_base=/opt/skill-sync-sidecar/state/openclaw-p0-live-apply-20260616-2
stage=8
apply_dry_run=8
apply=8
scan_after=40
apply_record=/home/admin/clawd/skills/.skill-sync-backups/20260616-035050-088751/.apply-record.json
```

All installed P0 directories and `SKILL.md` files are owned by `admin:admin`.

Post-apply reconcile:

```text
report=/private/tmp/openclaw-skill-sync-validate/reconcile-after-p0-live-apply-20260616115117/reconcile/reconcile-report.json
safe_to_auto_apply=true
summary={"remote_new": 52, "same_without_base": 40}
changed_since_previous=8 added, 0 changed, 0 removed
added=aliyun-sls-query, codeup-personal-git, freeze, gstack-openclaw-ceo-review, gstack-openclaw-investigate, gstack-openclaw-office-hours, gstack-openclaw-retro, unfreeze
```

Post-apply baseline reconcile:

```text
report=/private/tmp/openclaw-skill-sync-validate/reconcile-after-p0-live-baseline-20260616115123/reconcile/reconcile-report.json
safe_to_auto_apply=true
summary={"remote_new": 52, "same_without_base": 40}
changed_since_previous=0
```

Dry-run service steady state after restart:

```text
service=openclaw-skill-sync-sidecar-dryrun.service
active=true
daemon_status=running
cycles_run=2
summary={"noop": 40, "pull_new": 52}
blocked=0
conflicts=0
tombstones=0
applied=0
uploaded=0
```

OpenClaw live root currently has 49 first-level directories excluding `.skill-sync-backups`; 40 contain `SKILL.md` and are recognized as sidecar skill packages. The 9 non-package directories are not counted by the sidecar scanner:

```text
cron-protection
email-manager
hook-framework
investment-monitor
latency-analyzer
leonardo-image-gen
notes
steer-mode-monitor
task-workflow-engine
```

The remaining 52 `pull_new` skills stay uninstalled pending P1/P2 review. The service remains dry-run-only; no full 92-skill live apply was performed.

## P1 Wave-1 Isolated Validation

On 2026-06-16, the first P1 review wave was selected from the original P1 list. The wave intentionally includes low-blast-radius process and workflow skills before larger design, PDF, browser, deploy, or multi-agent packages.

Wave-1 allowlist:

```text
context-restore
context-save
investigate
learn
plan-tune
using-superpowers
```

Selection policy:

```text
risk_level=ok
scope=global
targets include openclaw
small file_count packages
no live service/deploy/browser/PDF generation package in first P1 wave
```

Local filtered snapshot:

```text
snapshot_id=openclaw-admission-p1-wave1-20260616
total=6
local_snapshot=/private/tmp/openclaw-admission-p1-wave1-snapshot-20260616
```

Local isolated target validation:

```text
target=/private/tmp/openclaw-admission-p1-wave1-local-20260616/target
plan={"pull_new": 6}
apply_dry_run=6
apply=6
scan=6
risk={"ok": 6, "warning": 0, "error": 0}
```

OpenClaw isolated validation first exposed a Linux portability bug in sidecar itself:

```text
failure=FileNotFoundError: /private/tmp/skill-sync-apply-stage-...
root_cause=core sync-apply staging hardcoded macOS /private/tmp
fix=skill-sync-sidecar v0.1.3 uses platform default TemporaryDirectory
```

OpenClaw v0.1.3 validation was performed in a separate venv before touching the running service:

```text
venv=/opt/skill-sync-sidecar/venv-0.1.3
install_source=github.com/commiao/skill-sync-sidecar.git@main
commit=5b07d9360dd11b1c67cc24d1cd2b4656826f3331
version=skill-sync 0.1.3
```

OpenClaw `/tmp` isolated target validation after the fix:

```text
remote_cache=/tmp/openclaw-admission-p1-wave1-full-cache-20260616-0624
snapshot=/tmp/openclaw-admission-p1-wave1-snapshot-20260616-0624
target=/tmp/openclaw-admission-p1-wave1-validate-20260616-0640/target
plan={"pull_new": 6}
apply_dry_run=6
apply=6
scan=6
risk={"ok": 6, "warning": 0, "error": 0}
apply_record=/tmp/openclaw-admission-p1-wave1-validate-20260616-0640/target/.skill-sync-backups/20260616T064037.581316Z/.apply-record.json
```

The OpenClaw dry-run systemd service was then switched from the old v0.1.2 venv to the validated v0.1.3 venv. It remains dry-run-only:

```text
unit=/etc/systemd/system/openclaw-skill-sync-sidecar-dryrun.service
exec=/opt/skill-sync-sidecar/venv-0.1.3/bin/python -m skill_sync_sidecar sync-daemon ... --dry-run
backup=/etc/systemd/system/openclaw-skill-sync-sidecar-dryrun.service.bak-20260616-0641
daemon_status=running
summary={"noop": 40, "pull_new": 52}
blocked=0
conflicts=0
tombstones=0
applied=0
uploaded=0
gateway=openclaw-gateway still running
```

## P1 Wave-1 Live Allowlist Apply

On 2026-06-16, P1 Wave-1 was applied to the live OpenClaw skill root after the fresh preflight reconcile remained green:

```text
preflight_report=/private/tmp/openclaw-skill-sync-validate/reconcile-before-p1-wave1-live-20260616/reconcile/reconcile-report.json
safe_to_auto_apply=true
summary={"remote_new": 52, "same_without_base": 40}
changed_since_previous=0
```

The first live attempt intentionally used `sync-apply` against the filtered six-skill snapshot. It was refused before writing because `sync-apply` is a two-way operation and saw the existing 40 live OpenClaw skills as `push_new` relative to the filtered snapshot:

```text
dryrun_summary={"pull_new": 6, "push_new": 40}
result=refused
reason=push actions require a remote destination
live_written=false
```

The correct live allowlist path is one-way `stage` + `apply` from the filtered snapshot:

```text
snapshot=/tmp/openclaw-admission-p1-wave1-snapshot-20260616-0624
state=/tmp/openclaw-p1-wave1-live-apply-20260616-0648
stage=6
apply_dry_run=6
apply=6
apply_record=/home/admin/clawd/skills/.skill-sync-backups/20260616-071526-975930/.apply-record.json
applied=context-restore, context-save, investigate, learn, plan-tune, using-superpowers
```

Post-apply verification:

```text
scan_after=46
risk={"ok": 44, "warning": 2, "error": 0}
wave1_present=true
owner=admin:admin
skill_md_mode=644
```

Post-apply reconcile:

```text
report=/private/tmp/openclaw-skill-sync-validate/reconcile-after-p1-wave1-live-apply-20260616/reconcile/reconcile-report.json
safe_to_auto_apply=true
summary={"remote_new": 46, "same_without_base": 46}
changed_since_previous=0
```

Dry-run service steady state after restart:

```text
service=openclaw-skill-sync-sidecar-dryrun.service
active=true
daemon_status=running
summary={"noop": 46, "pull_new": 46}
blocked=0
conflicts=0
tombstones=0
applied=0
uploaded=0
gateway=openclaw-gateway still running
```

The remaining 46 `pull_new` skills stay uninstalled pending review. No full 92-skill live apply was performed.
