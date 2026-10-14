"""Switch only OpenClaw sync/publisher/retention units, with durable rollback."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .report_retention import atomic_json

SYNC = 'openclaw-skill-sync-sidecar-pullonly.service'
PEER = 'openclaw-skill-sync-peer-status.service'
TIMER = 'openclaw-skill-sync-retention.timer'


def command(*args):
    subprocess.run(args, check=True)


def files(runtime):
    current = runtime / 'current'
    python = runtime / 'venv-0.1.3/bin/python'
    return {
        SYNC + '.d/retention-release.conf': '[Service]\nEnvironment=PYTHONPATH=%s/src\n' % current,
        PEER + '.d/retention-release.conf': ('[Service]\nEnvironment=PYTHONPATH=%s/src\n'
              'ExecStart=\nExecStart=%s/scripts/publish-openclaw-local-peer-status.sh\n') % (current, current),
        'openclaw-skill-sync-retention.service': ('[Unit]\nDescription=Retain generated skill sync reports\n'
              '[Service]\nType=oneshot\nUser=admin\nEnvironment=PYTHONPATH=%s/src\n'
              'ExecStart=%s -m skill_sync_sidecar.report_retention --work-root %s/work/current-mac-pullonly '
              '--state-dir %s/state --apply\n') % (current, python, runtime, runtime),
        TIMER: '[Unit]\nDescription=Skill sync report retention every 15 minutes\n[Timer]\nOnBootSec=120\nOnUnitActiveSec=900\n[Install]\nWantedBy=timers.target\n',
    }


def switch_link(current, target):
    tmp = current.with_name('.current-next')
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(target)
    tmp.replace(current)


def rollback(backup, run=command):
    manifest = json.loads((backup / 'manifest.json').read_text())
    runtime, units = Path(manifest['runtime']), Path(manifest['units'])
    # Stop the cleanup first; restoring a producer while cleanup is running is unsafe.
    if (units / TIMER).exists():
        run('systemctl', 'disable', '--now', TIMER)
    if (units / 'openclaw-skill-sync-retention.service').exists():
        run('systemctl', 'stop', 'openclaw-skill-sync-retention.service')
    run('systemctl', 'stop', SYNC)
    for name, existed in manifest['files'].items():
        target = units / name
        if existed:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup / 'units' / name, target)
        elif target.exists():
            target.unlink()
    current = runtime / 'current'
    if manifest['previous_current'] is not None:
        switch_link(current, manifest['previous_current'])
    elif current.is_symlink():
        current.unlink()
    run('systemctl', 'daemon-reload')
    run('systemctl', 'start', SYNC)
    if manifest['timer_enabled']:
        run('systemctl', 'enable', '--now', TIMER)
    run('systemctl', 'start', 'openclaw-skill-sync-peer-status.timer')
    atomic_json(backup / 'rollback-result.json', {'ok': True, 'at': time.time()})


def promote(release, runtime=Path('/opt/skill-sync-sidecar'), units=Path('/etc/systemd/system'),
            run=command, wait=True):
    release = release.resolve()
    marker = json.loads((release / 'RELEASE.json').read_text())
    if len(marker.get('commit', '')) != 40 or not marker.get('full_gate_passed'):
        raise ValueError('release identity/full gate proof missing')
    current = runtime / 'current'
    if current.exists() and not current.is_symlink():
        raise ValueError('current must be absent or a symlink')
    if wait:
        deadline = time.monotonic() + 360
        state = runtime / 'state/openclaw-daemon-pullonly-state.json'
        while True:
            data = json.loads(state.read_text())
            if not data.get('active_cycle') and time.time() - state.stat().st_mtime < 60:
                break
            if time.monotonic() > deadline:
                raise RuntimeError('no safe between-cycle release window')
            time.sleep(2)
    backup = runtime / 'backups' / ('retention-' + marker['commit'][:12] + '-' + str(time.time_ns()))
    backup.mkdir(parents=True, mode=0o700)
    targets = files(runtime)
    manifest = {'runtime': str(runtime), 'units': str(units),
                'previous_current': os.readlink(current) if current.is_symlink() else None,
                'files': {}, 'timer_enabled': False, 'commit': marker['commit']}
    # Exact snapshots of every overridden path precede any mutation. Original
    # service units/releases remain in place; drop-ins can be removed on rollback.
    for name in targets:
        path = units / name
        if path.is_symlink():
            raise ValueError('managed unit path must not be a symlink')
        manifest['files'][name] = path.exists()
        if path.exists():
            dest = backup / 'units' / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
    if run is command:
        manifest['timer_enabled'] = subprocess.run(['systemctl', 'is-enabled', TIMER],
                                                   capture_output=True).returncode == 0
    atomic_json(backup / 'manifest.json', manifest)
    print('rollback_backup=' + str(backup), flush=True)
    try:
        run('systemctl', 'stop', SYNC)
        # Publisher owns the same cache; wait for its current oneshot to finish.
        run('systemctl', 'stop', 'openclaw-skill-sync-peer-status.timer')
        if run is command:
            deadline = time.monotonic() + 180
            while subprocess.run(['systemctl', 'is-active', '--quiet', PEER]).returncode == 0:
                if time.monotonic() > deadline:
                    raise RuntimeError('peer publisher did not finish')
                time.sleep(2)
        switch_link(current, str(release))
        for name, content in targets.items():
            path = units / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            path.chmod(0o644)
        run('systemctl', 'daemon-reload')
        run('systemctl', 'start', SYNC)
        run('systemctl', 'start', 'openclaw-skill-sync-peer-status.timer')
        run('systemctl', 'enable', '--now', TIMER)
        run('systemctl', 'start', 'openclaw-skill-sync-retention.service')
        for unit in (SYNC, TIMER):
            run('systemctl', 'is-active', '--quiet', unit)
    except Exception:
        rollback(backup, run)
        run('systemctl', 'start', 'openclaw-skill-sync-peer-status.timer')
        raise
    atomic_json(backup / 'deployment-result.json', {'ok': True, 'commit': marker['commit'], 'at': time.time()})
    return backup


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path)
    parser.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    if args.rollback:
        rollback(args.rollback)
    elif args.release:
        promote(args.release)
    else:
        parser.error('--release or --rollback required')


if __name__ == '__main__':
    main()
