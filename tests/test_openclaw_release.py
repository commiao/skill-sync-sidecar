import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from skill_sync_sidecar.openclaw_release import promote, rollback, files, SYNC, TIMER


class OpenClawReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.runtime = self.root / 'runtime'
        self.units = self.root / 'units'
        self.units.mkdir()
        self.release = self.runtime / 'releases' / ('b' * 40)
        self.release.mkdir(parents=True)
        (self.release / 'RELEASE.json').write_text(json.dumps({'commit': 'b'*40, 'full_gate_passed': True}))
        self.old = self.runtime / 'releases' / 'old'
        self.old.mkdir()
        (self.runtime / 'current').symlink_to(self.old)
        self.calls = []

    def runner(self, *args):
        self.calls.append(args)

    def test_switch_and_complete_rollback_cover_every_overridden_file(self):
        target = self.units / (SYNC + '.d/retention-release.conf')
        target.parent.mkdir()
        target.write_text('previous dropin')
        backup = promote(self.release, self.runtime, self.units, self.runner, wait=False)
        self.assertEqual((self.runtime / 'current').resolve(), self.release)
        self.assertIn(str(self.runtime / 'current/src'), target.read_text())
        rollback(backup, self.runner)
        self.assertEqual((self.runtime / 'current').resolve(), self.old)
        self.assertEqual(target.read_text(), 'previous dropin')
        for name in files(self.runtime):
            if name != str(target.relative_to(self.units)):
                self.assertFalse((self.units / name).exists())
        self.assertTrue((self.old).exists())

    def test_failed_promotion_restores_old_entry_and_units(self):
        failed = [False]
        def runner(*args):
            self.calls.append(args)
            if args == ('systemctl', 'start', SYNC) and not failed[0]:
                failed[0] = True
                raise RuntimeError('simulated start failure')
        with self.assertRaises(RuntimeError):
            promote(self.release, self.runtime, self.units, runner, wait=False)
        self.assertEqual((self.runtime / 'current').resolve(), self.old)
        self.assertFalse((self.units / TIMER).exists())
        self.assertIn(('systemctl', 'disable', '--now', TIMER), self.calls)
        self.assertIn(('systemctl', 'start', SYNC), self.calls)

    def test_release_without_gate_refused_before_mutation(self):
        (self.release / 'RELEASE.json').write_text('{"commit":"not-a-commit"}')
        with self.assertRaises(ValueError):
            promote(self.release, self.runtime, self.units, self.runner, wait=False)
        self.assertEqual(self.calls, [])
        self.assertEqual((self.runtime / 'current').resolve(), self.old)

    def test_absent_current_is_restored_on_rollback(self):
        (self.runtime / 'current').unlink()
        backup = promote(self.release, self.runtime, self.units, self.runner, wait=False)
        rollback(backup, self.runner)
        self.assertFalse((self.runtime / 'current').is_symlink())

    def test_units_do_not_change_pull_policy_or_gateway(self):
        content = '\n'.join(files(self.runtime).values())
        self.assertNotIn('push-pull', content)
        self.assertNotIn('openclaw-gateway', content)
        self.assertNotIn('/releases/', content)

    def test_failure_before_timer_creation_still_restores_daemon(self):
        def runner(*args):
            self.calls.append(args)
            if args == ('systemctl', 'stop', 'openclaw-skill-sync-peer-status.timer'):
                raise RuntimeError('simulated timer-stop failure')
            if args == ('systemctl', 'disable', '--now', TIMER):
                raise AssertionError('must not disable a nonexistent timer')
        with self.assertRaises(RuntimeError):
            promote(self.release, self.runtime, self.units, runner, wait=False)
        self.assertEqual((self.runtime / 'current').resolve(), self.old)
        self.assertIn(('systemctl', 'start', SYNC), self.calls)
