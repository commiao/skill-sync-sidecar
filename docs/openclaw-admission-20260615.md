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

*_Still dry-run only. These are candidates for the first supervised OpenClaw live batch, not approved installs._

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

## Next Gate

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

No live OpenClaw skill root apply was performed for the P0 set.
