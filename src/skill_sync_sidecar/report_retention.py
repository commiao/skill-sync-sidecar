"""Conservative retention for generated deletion reports, never skill data."""
from __future__ import annotations

import json
import os
import shutil
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

FIELDS = ('skill_id', 'status_action', 'base_hash', 'local_hash', 'remote_hash',
          'reason', 'propagation', 'state', 'local_root', 'remote_snapshot')


def signature(metadata):
    return tuple(metadata.get(key) for key in FIELDS)


@contextmanager
def report_lock(root):
    # All builders and retention jobs share this lock, including standalone CLI.
    import fcntl
    if root.is_symlink():
        raise ValueError('report root must not be a symlink')
    root.mkdir(parents=True, exist_ok=True)
    lock = root / '.retention.lock'
    if lock.is_symlink():
        raise ValueError('report lock must not be a symlink')
    with lock.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def atomic_json(path, payload):
    temp = None
    try:
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                prefix='.' + path.name + '.', delete=False) as stream:
            temp = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


def records(root):
    result = []
    for path in root.iterdir():
        if path.is_symlink() or not path.is_dir():
            continue
        meta = path / 'tombstone.json'
        if meta.is_symlink():
            continue
        try:
            metadata = json.loads(meta.read_text(encoding='utf-8'))
            if metadata.get('record_type') != 'skill-sync-tombstone':
                continue
            if not metadata.get('skill_id') or metadata.get('status_action') not in ('local_deleted', 'remote_deleted'):
                continue
            signature(metadata)  # validate hashable fields; unknown/malformed data stays untouched
            hash(signature(metadata))
            result.append((path, metadata))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return result


def _prune_locked(root, now=None, apply=True):
    now = time.time() if now is None else now
    index = root / 'tombstone-index.json'
    # Missing/invalid index means we cannot prove its references: fail closed.
    if index.is_symlink():
        raise ValueError('tombstone index must not be a symlink')
    payload = json.loads(index.read_text(encoding='utf-8'))
    references = {Path(item['path']).resolve() for item in payload['tombstones']}
    if any(not path.is_dir() for path in references):
        raise ValueError('tombstone index contains a missing reference')
    groups = defaultdict(list)
    for path, metadata in records(root):
        groups[signature(metadata)].append(path)
    removed = []
    kept = []
    for group in groups.values():
        group.sort(key=lambda p: (p.stat().st_mtime, p.name))
        protected = {group[0], group[-1]}
        for path in group:
            if path in protected or path.resolve() in references or now - path.stat().st_mtime < 3600:
                kept.append(path.name)
            else:
                # Never traverse a symlink in a generated snapshot.
                if any(p.is_symlink() for p in path.rglob('*')):
                    kept.append(path.name)
                    continue
                if apply:
                    shutil.rmtree(path)
                removed.append(path.name)
    return {'content_groups': len(groups), 'removed': removed, 'kept': len(kept),
            'retention': 'first/latest per content, current index references, all snapshots younger than one hour'}


def prune_tombstones(root, now=None, apply=True):
    if not root.exists() or root.is_symlink():
        return {'removed': [], 'kept': 0}
    with report_lock(root):
        result = _prune_locked(root, now, apply)
        if apply:
            atomic_json(root / 'retention-status.json', dict(result, checked_at=time.time()))
        return result


def prune_state_temps(state_dir, now=None, apply=True):
    now = time.time() if now is None else now
    removed = []
    if not state_dir.is_dir() or state_dir.is_symlink():
        return removed
    for pattern, marker in (('*.json.tools.*', '.tools.'), ('*.json.tmp.*', '.tmp.')):
        for path in state_dir.glob(pattern):
            owner = state_dir / path.name.split(marker, 1)[0]
            if path.is_symlink() or not path.is_file() or not owner.is_file() or owner.is_symlink():
                continue
            if now - path.stat().st_mtime < 86400:
                continue
            if apply:
                path.unlink()
            removed.append(path.name)
    return removed


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-root', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    result = {'tombstones': prune_tombstones(args.work_root / 'tombstones', apply=args.apply),
              'state_temps_removed': prune_state_temps(args.state_dir, apply=args.apply)}
    print(json.dumps(result))


if __name__ == '__main__':
    main()
