# OpenClaw generated report retention

Unchanged deletion records reuse their existing complete tombstone instead of
copying local/remote material every five minutes. Content, source roots, action,
propagation and state identify a version. Changed content creates a new version.
The builder atomically updates the index after material is complete. Builders
and the retention job take the same advisory lock.

Every cycle and the independent 15-minute systemd timer retain the first and
latest copy of each content signature, every current index reference, and all
snapshots less than one hour old. Unknown/malformed reports and symlinks are
untouched; an invalid index or missing reference fails closed. The timer also
removes only old (>24h) `.json.tools.*` / `.json.tmp.*` files beside their existing
formal state JSON. The publisher removes its tools file on success and failure.
This does not delete installed skills, remote archives, sessions or business
backups. Unique content versions are retained; this is not a quota on business
data or an instruction to propagate deletions.

## OpenClaw release

From a clean task branch, merge reviewed changes to main before publishing:

```
scripts/release-openclaw.sh <main-commit>
```

The script checks real remote main ancestry, deploy-standard's machine gate and
the complete default release gate in a fresh checkout of that exact commit. It
uploads an immutable git archive, verifies every tracked file, and compiles with
the existing isolated Python 3.11 runtime. It waits for a gap between cycles,
backs up every overwritten systemd drop-in and the old current target, then
switches `/opt/skill-sync-sidecar/current`. Only the pull-only sync daemon,
peer-status publisher and independent retention timer use the new stable entry.
The existing daemon arguments, local skill root and writer policy are unchanged.
It does not restart the OpenClaw gateway, publish Mac/NAS or replace system Python.

The release prints `rollback_backup=/opt/skill-sync-sidecar/backups/...`.
A failed promotion restores the previous units and current target automatically.
For operator rollback, use the new immutable artifact (it is retained):

```
sudo -n env PYTHONPATH=/opt/skill-sync-sidecar/releases/<new-sha>/src \
  /opt/skill-sync-sidecar/venv-0.1.3/bin/python \
  -m skill_sync_sidecar.openclaw_release --rollback <rollback-backup>
```

Backup coverage is the full set of mutated configuration paths plus current;
old source releases, cache, base/state JSON and material snapshots are not
replaced by deployment. Retention's deletion semantics apply immediately; a
rollback disables the new timer but cannot reconstruct deleted duplicates.

After release, check service environment/ExecStart, actual process start time,
RELEASE.json hashes, timer result and two real daemon cycles. A blocked review
queue can remain blocked; do not resolve unrelated skill differences merely to
make that queue green. No cycle may upload central changes under pull-only.
Verify retained first/latest snapshots and index references still exist and
unchanged tombstones stop increasing. Check that the peer publisher leaves no
new tools files. Existing conflict disk guard continues independently.
