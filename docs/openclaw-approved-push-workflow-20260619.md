# OpenClaw Approved Push Workflow - 2026-06-19

## Goal

Close the final governance gap for OpenClaw pull-only sync: when a local OpenClaw edit is intentionally blocked by `writer_policy=pull-only`, operators can review the blocked report and save only explicitly approved local skills to WebDAV without changing the unattended service to `push-pull`.

## Capability

New command:

```text
skill-sync approved-push
```

Inputs:

```text
--blocked-report blocked-report.json
--skill-id <reviewed-skill-id>
--local-root /home/admin/clawd/skills
--remote-snapshot /opt/skill-sync-sidecar/cache/current-mac-pullonly
--last-applied-record /opt/skill-sync-sidecar/state/openclaw-base-record.json
--base-record-out /opt/skill-sync-sidecar/state/openclaw-base-record.json
--out /opt/skill-sync-sidecar/work/current-mac-pullonly/approved-push
--dry-run | --yes
```

Safety behavior:

- Requires explicit `--skill-id`; it never approves all blocked items implicitly.
- Requires a `skill-sync-blocked-report`.
- Accepts only writer-policy-blocked local `push` or `push_new` candidates.
- Refuses if the selected skill's base/local/remote hashes changed since the blocked report was generated.
- Refuses if the live remote `index.json` no longer matches the cached `--remote-snapshot`.
- Builds a merged remote snapshot from the cached remote index plus only approved local skill archives.
- Uploads only the approved archives and final `index.json`.
- Writes an approved-push audit record and a new base record.
- Leaves unselected local pushes unapproved, so they continue to appear as pending/blocked in later reports.

## Local Validation

```text
commit=c2401cf
tests=77 passed
compileall=passed
diff_check=passed
status_current=overall_ok True
mac_status_summary={'unchanged': 93}
```

Synthetic tests cover:

```text
approved alpha local change is published
unapproved beta local change stays remote-base and remains pending push
stale blocked-report hashes are refused
```

## OpenClaw Passive Deployment

The source was copied to OpenClaw without switching systemd units and without restarting OpenClaw gateway:

```text
release_path=/opt/skill-sync-sidecar/releases/c2401cf
python=/opt/skill-sync-sidecar/venv-0.1.3/bin/python
help_check=PYTHONPATH=/opt/skill-sync-sidecar/releases/c2401cf/src python -m skill_sync_sidecar approved-push --help
compileall=passed
```

Service state after deployment:

```text
openclaw-skill-sync-sidecar-pullonly.service=active running
pullonly_NRestarts=0
openclaw-skill-sync-sidecar-dryrun.service=active running
openclaw-gateway pid=2966537
openclaw-gateway uptime=7-04:46:03
```

No OpenClaw skill content was uploaded during this deployment. The command is now available for the next real blocked local edit review.

## Operator Flow

1. Review `/opt/skill-sync-sidecar/work/current-mac-pullonly/blocked-report/blocked-report.md`.
2. Select one or more specific skills to publish.
3. Run `approved-push --dry-run` with the selected `--skill-id` values.
4. If the preview is correct, rerun the same command with `--yes`.
5. Confirm `sync-status` shows approved skills as `unchanged`; unapproved local edits should remain `push` or blocked.

Do not change the unattended OpenClaw service away from `--writer-policy pull-only` just to publish a reviewed local edit.
