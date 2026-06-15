# Sync Probe Validation - 2026-06-15

Validation target: sidecar sync mechanism across Mac, WebDAV, and OpenClaw.

Sidecar version: `v0.1.2`.

Probe skill: `sync-probe`.

WebDAV prefix:

```text
skill-sync-sidecar-dev/sync-probe-20260615181108
```

Local artifact root:

```text
/private/tmp/skill-sync-probe-20260615181108
```

OpenClaw isolated root:

```text
/tmp/skill-sync-sidecar-validate/sync-probe-v1
```

## Scope

This validation intentionally used a synthetic skill and isolated target roots:

- No writes to `/home/admin/clawd/skills`.
- No OpenClaw service restart.
- No OpenClaw daemon install.
- No Python runtime installation on OpenClaw.
- OpenClaw writes were limited to `/tmp/skill-sync-sidecar-validate/...`.

OpenClaw currently has only Python `3.6.8`, so the full sidecar runtime cannot run there yet. A Python 3.6 compatible probe script was added:

```text
scripts/openclaw-sync-probe-py36.py
```

It downloads a sidecar snapshot from WebDAV using OpenClaw's cc-switch WebDAV settings, stages a selected skill into `/tmp`, validates archive safety and content hashes, and emits JSON.

## P0 - Downstream Sync Probe

Mac created and uploaded `sync-probe` to WebDAV.

OpenClaw downloaded and staged it into:

```text
/tmp/skill-sync-sidecar-validate/sync-probe-v1/staged/cc-switch/sync-probe
```

Validation result:

```json
{
  "p0_ok": true,
  "content_hash": "90f82aac7c81f443601c844f3ad533bc0a87677a8ce88dd901743521ead83ccd",
  "file_count": 2,
  "paths": ["SKILL.md", "notes/probe.txt"]
}
```

The synthetic skill also contained `.env`, `__pycache__`, and `dist` files. They were excluded from the snapshot and did not appear in the staged OpenClaw inventory.

## P1 - Conflict Strategy Probe

Conflict was simulated without touching live skills:

1. OpenClaw isolated copy appended an OpenClaw-local edit to `SKILL.md`.
2. Mac changed the same `sync-probe` skill and pushed a new snapshot to the same dev prefix.
3. `reconcile-report` compared OpenClaw isolated inventory against the new remote snapshot using the previous OpenClaw inventory as base.

Validation result:

```json
{
  "p1_conflict_ok": true,
  "summary": {
    "conflict": 1
  },
  "changed_since_previous": {
    "changed_count": 1,
    "changed": ["sync-probe"],
    "added": [],
    "removed": []
  },
  "gate_blockers": [
    "safe_to_auto_apply=false",
    "conflict=1",
    "changed_since_previous=1"
  ]
}
```

Decision: the sidecar blocks the peer-writer conflict instead of overwriting either side.

## P2 - Automatic Sync Probe

The daemon loop was validated on a local temporary target root using the same WebDAV prefix:

```text
/private/tmp/skill-sync-probe-20260615181108/daemon-target
```

Validation result:

```json
{
  "p2_daemon_ok": true,
  "cycle": {
    "status": "complete",
    "reason": "sync actions applied",
    "snapshot_id": "sync-probe-v2-mac",
    "summary": {
      "pull_new": 1
    },
    "blocked": 0,
    "conflicts": 0,
    "tombstones": 0,
    "applied": 1,
    "uploaded": 0
  }
}
```

The first daemon attempt was run inside the local sandbox without network permission and failed with a DNS error. Re-running the same command with network permission succeeded. This was an execution-environment constraint, not a sidecar sync failure.

## Current Result

The sync mechanism is validated for:

- Mac synthetic skill packaging.
- WebDAV upload and download under a dedicated dev prefix.
- OpenClaw isolated download and staging.
- OpenClaw discovery of `SKILL.md` metadata through inventory.
- Content hash equivalence across Mac snapshot and OpenClaw staged copy.
- Default exclusion of secrets/caches/build artifacts.
- Conflict detection and blocking for peer-writer drift.
- One-cycle automatic pull through `sync-daemon` into a non-live target root.

## Remaining Gates Before OpenClaw Live Rollout

Do not enable OpenClaw live apply or daemon until:

- A Python `3.9+` runtime or approved isolated container runtime exists on OpenClaw.
- A dry-run daemon writes auditable state for the intended OpenClaw target.

## OpenClaw Optimization Adoption

The 8 stable OpenClaw skill optimizations from `reconcile-20260615-after-skill-work-settled` were adopted into the Mac canonical root and synced to the `current-mac` WebDAV snapshot.

Adopted files:

```text
adopted_files=19
backup_root=/Users/mac/.cc-switch/skills/.skill-sync-backups/openclaw-adopt-20260615-193256
remote_snapshot_id=20260615T113322.799109Z
remote_total=92
```

Validation:

```text
python_compile=ok
node_check=ok
hash_match=19
scan_ok=1
```

OpenClaw read-only reconcile after adoption:

```text
local: 32
remote: 92
safe_to_auto_apply: True
summary:
  remote_new: 60
  same_without_base: 32
changed_since_previous: 0
openclaw_gate: ok=True safe_to_auto_apply=True
```

Current ops status:

```text
sync_plan: safe_to_apply=True blocked=0 allowed=92
sync_summary: {'noop': 92}
openclaw_reconcile: safe_to_auto_apply=True local=32 remote=92
openclaw_summary: {'remote_new': 60, 'same_without_base': 32}
openclaw_changed_since_previous: 0
openclaw_gate: ok=True blockers=[]
overall_ok: True
```

Content note: the adopted OpenClaw optimizations still include several `/home/admin/clawd/...` fallbacks behind `LARK_SEND_TEXT_BIN` / `LARK_SEND_FILE_BIN` environment overrides. They are not secrets and do not block the sync mechanism gate, but they should be normalized in the next skill-content optimization pass before treating those skills as fully device-neutral.

## OpenClaw Live `sync-probe` Apply

The live-root apply gate was validated with `sync-probe` only. The Python 3.6 compatible probe script now supports an explicit write mode:

```text
--apply-root /home/admin/clawd/skills --yes-apply
```

Safety controls:

- Write mode is opt-in; `--apply-root` without `--yes-apply` refuses to run.
- Live apply is restricted to `skill_id=sync-probe`.
- The staged archive is hash-validated before and after install.
- The installed tree is ownership-aligned to the OpenClaw skill root owner.
- An apply record is written under `.skill-sync-backups`.

Live apply result:

```text
local_report=/private/tmp/openclaw-sync-probe-live-20260615201345.json
remote_out=/tmp/skill-sync-sidecar-validate/sync-probe-live-20260615201345
snapshot_id=sync-probe-v2-mac
skill_id=sync-probe
content_hash=eadf364359152305228dfd63017bc25f702170a95b48e2237b6a6640629f513a
actual_hash=eadf364359152305228dfd63017bc25f702170a95b48e2237b6a6640629f513a
target_path=/home/admin/clawd/skills/sync-probe
apply_record=/home/admin/clawd/skills/.skill-sync-backups/openclaw-sync-probe-20260615201350/apply-record.json
previous_exists=False
```

OpenClaw live-root verification:

```text
owner=admin:admin
files:
  SKILL.md
  notes/probe.txt
inventory_total=33
sync_probe_found=1
file_count=2
```

Cleanup:

```text
moved_to=/home/admin/clawd/skills/.skill-sync-backups/openclaw-sync-probe-20260615201350/sync-probe-live-cleanup
cleanup_record=/home/admin/clawd/skills/.skill-sync-backups/openclaw-sync-probe-20260615201350/cleanup-record.json
```

The cleanup moved the test skill out of the live root instead of deleting it, preserving the applied artifact for audit while keeping the normal `current-mac` reconcile gate clean.

Post-cleanup OpenClaw read-only reconcile:

```text
local: 32
remote: 92
safe_to_auto_apply: True
summary:
  remote_new: 60
  same_without_base: 32
changed_since_previous: 0
openclaw_gate: ok=True safe_to_auto_apply=True
```

## Next Recommended Step

Proceed to an OpenClaw dry-run daemon plan against the intended live root only after an approved Python `3.9+` or isolated runtime path exists. Continue avoiding full 92-skill live apply until that runtime and daemon state gate are green.
