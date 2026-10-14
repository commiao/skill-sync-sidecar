#!/usr/bin/env bash
set -euo pipefail

# Publish only a verified main commit. No live edits or mutable source tree.
usage() {
  echo 'Usage: scripts/release-openclaw.sh <main-commit>'
}
if [[ "${1:-}" = '--help' || "${1:-}" = '-h' ]]; then usage; exit 0; fi
if [[ $# != 1 ]]; then usage >&2; exit 64; fi
repo="$(git rev-parse --show-toplevel)"
sha="$(git rev-parse "${1}^{commit}")"
remote_main="$(GIT_TERMINAL_PROMPT=0 git ls-remote --heads origin main | awk '{print $1}')"
[[ "$remote_main" =~ ^[0-9a-f]{40}$ ]] || { echo 'real remote main unavailable' >&2; exit 1; }
git fetch origin main
git merge-base --is-ancestor "$sha" "$remote_main"
fleet_root="$HOME/.local/share/fleet-ops/current"
python3 - "$repo" "$sha" "$fleet_root" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[3]) / 'lib'))
from fleetops_gate import run_gate
ok, lines = run_gate(sys.argv[1], sys.argv[2], sys.argv[3])
print('\n'.join(lines))
if not ok:
    raise SystemExit('deploy-standard gate refused release')
PY
stage="$(mktemp -d /private/tmp/skill-sync-openclaw-release.XXXXXX)"
checkout="$stage/checkout"
cleanup() {
  git -C "$repo" worktree remove "$checkout" --force 2>/dev/null || true
  rm -rf "$stage"
}
trap cleanup EXIT
git worktree add --detach "$checkout" "$sha"
# Full default gate, including operational status; no SKIP_OPS override.
(cd "$checkout" && unset SKILL_SYNC_SKIP_OPS_STATUS && PYTHON_BIN=/usr/bin/python3 scripts/verify-release.sh full) > "$stage/full-gate.log" 2>&1 || {
  cat "$stage/full-gate.log"; exit 1;
}
cat "$stage/full-gate.log"
mkdir "$stage/payload"
git archive "$sha" | tar -xf - -C "$stage/payload"
python3 - "$stage/payload" "$sha" "$stage/full-gate.log" <<'PY'
import hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[1])
manifest={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
          for p in root.rglob('*') if p.is_file()}
(root/'RELEASE.json').write_text(json.dumps({'commit':sys.argv[2], 'full_gate_passed':True,
    'full_gate_log_sha256':hashlib.sha256(Path(sys.argv[3]).read_bytes()).hexdigest(),
    'source_sha256':manifest},indent=2)+'\n')
PY
tar -czf "$stage/payload.tar.gz" -C "$stage/payload" .
remote_upload="$(ssh admin@100.79.177.102 'umask 077; mktemp /home/admin/clawd/logs/skill-sync-release.XXXXXX')"
[[ "$remote_upload" =~ ^/home/admin/clawd/logs/skill-sync-release\.[a-zA-Z0-9]+$ ]] || {
  echo 'unexpected remote upload path' >&2; exit 1;
}
scp "$stage/payload.tar.gz" "admin@100.79.177.102:$remote_upload"
ssh admin@100.79.177.102 sudo -n /opt/skill-sync-sidecar/venv-0.1.3/bin/python - "$remote_upload" "$sha" <<'PY'
import hashlib,json,subprocess,sys,tarfile
from pathlib import Path
archive,sha=Path(sys.argv[1]),sys.argv[2]
root=Path('/opt/skill-sync-sidecar/releases')/sha
if root.exists():
    raise SystemExit('immutable release already exists; investigate instead of overwriting')
root.mkdir(mode=0o755)
try:
    with tarfile.open(archive) as tar:
        for item in tar.getmembers():
            if item.issym() or item.islnk() or not (item.isdir() or item.isfile()):
                raise ValueError('unexpected archive member')
            target=(root/item.name).resolve()
            target.relative_to(root)
        tar.extractall(root, filter='data')
    release=json.loads((root/'RELEASE.json').read_text())
    assert release['commit']==sha and release['full_gate_passed']
    for name,checksum in release['source_sha256'].items():
        assert hashlib.sha256((root/name).read_bytes()).hexdigest()==checksum, name
    subprocess.run(['/opt/skill-sync-sidecar/venv-0.1.3/bin/python','-m','compileall','-q',str(root/'src')],check=True)
    archive.unlink()
except Exception:
    # The entry has not been switched; preserve failed staging for diagnosis.
    raise
print('verified_release='+str(root))
PY
ssh admin@100.79.177.102 sudo -n env "PYTHONPATH=/opt/skill-sync-sidecar/releases/$sha/src" \
  /opt/skill-sync-sidecar/venv-0.1.3/bin/python -m skill_sync_sidecar.openclaw_release \
  --release "/opt/skill-sync-sidecar/releases/$sha"
printf 'openclaw_release=%s\n' "$sha"
