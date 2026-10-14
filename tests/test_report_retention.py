import json
import os
import shutil
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from skill_sync_sidecar import tombstones
from skill_sync_sidecar import report_retention as retention
from skill_sync_sidecar.sync_cycle import _build_tombstones_if_needed


class ReportRetentionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.local = self.root / 'local'
        (self.local / 'demo').mkdir(parents=True)
        (self.local / 'demo/SKILL.md').write_text('original')
        self.remote = self.root / 'remote'
        self.remote.mkdir()
        self.out = self.root / 'tombstones'
        self.item = {'skill_id': 'demo', 'action': 'remote_deleted', 'reason': 'deleted',
                     'base_hash': 'base', 'local_hash': 'base', 'remote_hash': None}
        self.status = {'items': [self.item]}

    def build(self):
        with patch.object(tombstones, 'build_sync_status', return_value=self.status):
            return tombstones.build_tombstones(self.local, self.remote, self.out)

    def test_repeated_cycles_reuse_same_material(self):
        first = self.build()
        before = Path(first['tombstones'][0]['path'])
        stamp = before.stat().st_mtime
        for _ in range(5):
            result = self.build()
            self.assertEqual(result['tombstones'][0]['path'], str(before))
            self.assertEqual(result['reused'], 1)
        self.assertEqual(before.stat().st_mtime, stamp)
        self.assertEqual(len(retention.records(self.out)), 1)
        self.assertEqual((before / 'local/SKILL.md').read_text(), 'original')

    def test_content_change_creates_new_snapshot_keeps_first(self):
        first = self.build()['tombstones'][0]['path']
        self.item['local_hash'] = 'new'
        (self.local / 'demo/SKILL.md').write_text('new')
        second = self.build()['tombstones'][0]['path']
        self.assertNotEqual(first, second)
        self.assertTrue(Path(first).exists())
        self.assertEqual(len(retention.records(self.out)), 2)

    def test_retention_keeps_first_latest_recent_and_references(self):
        first = Path(self.build()['tombstones'][0]['path'])
        now = time.time()
        os.utime(first, (now - 9000, now - 9000))
        copies = []
        for i, age in enumerate((8000, 7000, 6000, 100)):
            target = self.out / ('copy-%d' % i)
            shutil.copytree(first, target)
            os.utime(target, (now-age, now-age))
            copies.append(target)
        retention.atomic_json(self.out / 'tombstone-index.json', {'tombstones': [{'path': str(copies[1])}]})
        result = retention.prune_tombstones(self.out, now=now)
        self.assertEqual(set(result['removed']), {copies[0].name, copies[2].name})
        for kept in (first, copies[1], copies[3]):
            self.assertTrue(kept.exists())
        self.assertEqual((self.local / 'demo/SKILL.md').read_text(), 'original')

    def test_empty_cycle_preserves_history_and_refreshes_index(self):
        first = Path(self.build()['tombstones'][0]['path'])
        self.status = {'items': []}
        with patch.object(tombstones, 'build_sync_status', return_value=self.status):
            result = _build_tombstones_if_needed(self.status, self.local, self.remote,
                                                self.root, None)
        self.assertIsNone(result)
        self.assertTrue(first.exists())
        self.assertEqual(json.loads((self.out / 'tombstone-index.json').read_text())['tombstones'], [])

    def test_invalid_index_fails_closed(self):
        self.build()
        (self.out / 'tombstone-index.json').write_text('{broken')
        with self.assertRaises(ValueError):
            retention.prune_tombstones(self.out)
        self.assertEqual(len(retention.records(self.out)), 1)

    def test_missing_reference_fails_closed(self):
        self.build()
        retention.atomic_json(self.out / 'tombstone-index.json', {'tombstones': [{'path': str(self.out / 'missing')}]})
        with self.assertRaises(ValueError):
            retention.prune_tombstones(self.out)

    def test_symlinks_and_unknown_reports_stay_untouched(self):
        first = Path(self.build()['tombstones'][0]['path'])
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'valuable').write_text('data')
        link = self.out / 'external'
        link.symlink_to(outside)
        (self.out / 'unrecognized').mkdir()
        retention.prune_tombstones(self.out)
        self.assertTrue(link.is_symlink())
        self.assertTrue((outside / 'valuable').exists())
        self.assertTrue((self.out / 'unrecognized').exists())
        self.assertTrue(first.exists())

    def test_dry_run_does_not_delete(self):
        self.build()
        before = sorted(str(p) for p in self.out.rglob('*'))
        retention.prune_tombstones(self.out, apply=False)
        self.assertEqual(before, sorted(str(p) for p in self.out.rglob('*')))

    def test_only_old_owned_temporary_files_removed(self):
        now = time.time()
        state = self.root / 'state'
        state.mkdir()
        (state / 'peer.json').write_text('{}')
        for name, age in [('peer.json.tools.old', 90000), ('peer.json.tools.new', 10),
                          ('peer.json.tmp.old', 90000), ('unknown.json.tools.old', 90000)]:
            p = state / name
            p.write_text('{}')
            os.utime(p, (now-age, now-age))
        self.assertEqual(set(retention.prune_state_temps(state, now)),
                         {'peer.json.tools.old', 'peer.json.tmp.old'})
        self.assertTrue((state / 'peer.json').exists())
        self.assertTrue((state / 'peer.json.tools.new').exists())
        self.assertTrue((state / 'unknown.json.tools.old').exists())

    def test_failed_materialization_does_not_publish_partial_record(self):
        with patch.object(tombstones, '_write_local_material', side_effect=OSError('copy failed')):
            with self.assertRaises(OSError):
                self.build()
        self.assertEqual(retention.records(self.out), [])
        self.assertFalse((self.out / 'tombstone-index.json').exists())

    def test_publisher_removes_tools_before_disabling_trap(self):
        script = (Path(__file__).parents[1] / 'scripts/publish-openclaw-local-peer-status.sh').read_text()
        self.assertIn('mv "$tmp_file" "$STATUS_FILE"\nrm -f "$tools_file"\ntrap - EXIT', script)

    def test_root_symlink_refused_before_build(self):
        outside = self.root / 'outside'
        outside.mkdir()
        self.out.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.build()
        self.assertEqual(list(outside.iterdir()), [])

    def test_publisher_success_leaves_no_generated_temp_files(self):
        import subprocess
        import sys
        state = self.root / 'state'
        state.mkdir()
        wrapper = self.root / 'python-wrapper'
        wrapper.write_text('#!/bin/sh\nif [ "$1" = "-m" ]; then\n'
                           ' case "$3" in\nops-status) echo \'{"health":"green"}\';;\nesac\nexit 0\nfi\n'
                           'exec "%s" "$@"\n' % sys.executable)
        wrapper.chmod(0o700)
        env = dict(os.environ, OPENCLAW_PYTHON=str(wrapper), OPENCLAW_SKILL_ROOT=str(self.local),
                   OPENCLAW_PEER_STATUS_FILE=str(state/'peer.json'),
                   OPENCLAW_REMOTE_SNAPSHOT=str(self.remote))
        script = Path(__file__).parents[1] / 'scripts/publish-openclaw-local-peer-status.sh'
        result = subprocess.run(['bash',str(script)], env=env, capture_output=True,text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([p.name for p in state.iterdir()], ['peer.json'])

    def test_retention_waits_for_builder_lock(self):
        import subprocess
        import sys
        self.build()
        marker = self.root / 'acquired'
        code = ('from pathlib import Path; from skill_sync_sidecar.report_retention import report_lock; '
                'import sys\nwith report_lock(Path(sys.argv[1])):\n Path(sys.argv[2]).write_text("locked")\n')
        with retention.report_lock(self.out):
            proc = subprocess.Popen([sys.executable, '-c', code, str(self.out), str(marker)],
                                    env=dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1]/'src')))
            try:
                time.sleep(.15)
                self.assertFalse(marker.exists())
            except Exception:
                proc.kill()
                proc.wait()
                raise
        self.assertEqual(proc.wait(timeout=5), 0)
        self.assertEqual(marker.read_text(), 'locked')
