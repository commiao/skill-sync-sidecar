"""mac-release.json must name every resident launchd job, and the idle check must be honest.

fleet-ops `mac-release.py` restarts the jobs this file declares after it switches
`current`. A release that does not restart a resident job is not live: on
2026-10-13 sync-daemon and the dashboard were still running 2026-09-30 code and
the executor was pinned to a pruned release directory (fleet-ops T-0265). The
list used to live only in docs/release.md, so it depended on someone passing
four `--reload` flags by hand every time.

Two things are pinned here:

  1. The declared labels equal the resident jobs the installers create. The rule
     is mechanical: an installer whose plist has StartInterval starts fresh every
     run and needs no restart; every other installer creates a resident job. The
     expected set is derived from scripts/, not written down a second time, so
     adding an installer without declaring it turns this red.
  2. scripts/sync-daemon-idle.py says "idle" only between cycles. Killing the
     daemon mid-cycle can cut an upload or a local apply in half.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DECL = REPO / "mac-release.json"
IDLE = REPO / "scripts/sync-daemon-idle.py"

_spec = importlib.util.spec_from_file_location("sync_daemon_idle", IDLE)
idle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(idle)


def installer_jobs() -> dict[str, bool]:
    """label -> resident? for every launchd installer in scripts/."""
    jobs = {}
    for script in sorted((REPO / "scripts").glob("install-*launchd.sh")):
        text = script.read_text(encoding="utf-8")
        labels = set(re.findall(r'LABEL:-(com\.skill-sync-sidecar[\w.-]*)\}', text))
        labels |= set(re.findall(r'"Label":\s*"(com\.skill-sync-sidecar[\w.-]*)"', text))
        if "\"Label\"" not in text:
            continue          # not a plist writer
        if len(labels) != 1:
            raise AssertionError(f"{script.name}: expected one label, found {sorted(labels)}")
        jobs[labels.pop()] = "StartInterval" not in text
    return jobs


def declared() -> dict[str, dict]:
    data = json.loads(DECL.read_text(encoding="utf-8"))
    out = {}
    for entry in data["reload"]:
        entry = {"label": entry} if isinstance(entry, str) else entry
        out[entry["label"]] = entry
    return out


class DeclarationTests(unittest.TestCase):

    def test_installers_are_found(self):
        jobs = installer_jobs()
        # If the scan silently matched nothing, every check below would pass.
        self.assertGreaterEqual(len(jobs), 6, jobs)
        self.assertIn("com.skill-sync-sidecar", jobs)

    def test_every_resident_job_is_declared_and_nothing_else(self):
        resident = {label for label, keeps in installer_jobs().items() if keeps}
        self.assertEqual(set(declared()), resident)

    def test_sync_daemon_waits_for_a_gap_and_the_others_do_not(self):
        decl = declared()
        self.assertEqual(decl["com.skill-sync-sidecar"]["when_idle"],
                         ["/usr/bin/python3", "scripts/sync-daemon-idle.py"])
        for label, entry in decl.items():
            if label != "com.skill-sync-sidecar":
                self.assertNotIn("when_idle", entry, label)

    def test_the_idle_command_runs_from_a_release_tree(self):
        """mac-release.py runs it with cwd = the release directory."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "missing.json"
            done = subprocess.run([sys.executable, "scripts/sync-daemon-idle.py",
                                   "--state-file", str(state)],
                                  cwd=REPO, capture_output=True, text=True)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("busy: cannot read", done.stdout)


class IdleCheckTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state.json"
        self.now = datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)

    def write(self, seconds_ago: float, **extra) -> None:
        state = {"daemon_status": "running", "interval_seconds": 300.0,
                 "updated_at": (self.now - timedelta(seconds=seconds_ago)).isoformat()}
        state.update(extra)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def check(self) -> bool:
        return idle.check(self.state, self.now)[0]

    def test_between_cycles_is_idle(self):
        self.write(30)
        self.assertTrue(self.check())

    def test_a_running_cycle_is_busy(self):
        self.write(30, active_cycle={"cycle": 7, "status": "running"})
        self.assertFalse(self.check())

    def test_right_before_the_next_cycle_is_busy(self):
        self.write(270)
        self.assertFalse(self.check())

    def test_an_overdue_cycle_is_busy(self):
        self.write(400)
        self.assertFalse(self.check())

    def test_unreadable_or_incomplete_state_is_busy(self):
        self.state.write_text("{not json", encoding="utf-8")
        self.assertFalse(self.check())
        self.state.write_text(json.dumps({"daemon_status": "running"}), encoding="utf-8")
        self.assertFalse(self.check())

    def test_a_finished_daemon_has_nothing_to_interrupt(self):
        self.write(9999, daemon_status="complete")
        self.assertTrue(self.check())


if __name__ == "__main__":
    unittest.main()
