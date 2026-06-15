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

- The 8 real OpenClaw conflicts from `reconcile-20260615-after-skill-work-settled` are reviewed and adopted or merged.
- A Python `3.9+` runtime or approved isolated container runtime exists on OpenClaw.
- A dry-run daemon writes auditable state for the intended OpenClaw target.
- A live-root apply is first tested with `sync-probe` only, not the full 92-skill snapshot.

## Next Recommended Step

Adopt the now-stable OpenClaw skill optimizations into the canonical WebDAV snapshot, then rerun:

```bash
REMOTE_CACHE=/Users/mac/public-sync/skill-sync-sidecar-dev/current-mac \
PREVIOUS_INVENTORY=/private/tmp/openclaw-skill-sync-validate/reconcile-20260615-after-skill-work-settled/openclaw-inventory.json \
  scripts/openclaw-reconcile-readonly.sh \
  /private/tmp/openclaw-skill-sync-validate/reconcile-after-openclaw-adoption
```

Proceed to OpenClaw live `sync-probe` apply only after the real conflict count is `0`.
