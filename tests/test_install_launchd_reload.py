"""Installers must not issue bootout/bootstrap themselves (T-0148).

`launchctl bootout` returning does not mean the job has exited. fleet-ops
T-0142 observed the cost when four resident jobs were switched at once:

    Bootstrap failed: 5: Input/output error   x4
    launchctl list | grep skill-sync-sidecar  -> all four gone

Nothing was installed back, and `bootstrap` does not retry: the old jobs were
unloaded, the new ones never arrived, four services down for ~2 minutes. They
were our jobs. The same two lines had worked the day before on a single job --
a human read the output between them and that delay covered the race.

**A timing defect that hides when you do one at a time is not tested by doing
one at a time.** So these tests do not try to reproduce the race; they pin the
thing that made it possible: seven installers each issuing the pair by hand.

Four things are pinned here, and the fourth is the one that is easy to lose:

  1. no installer issues bootout/bootstrap itself
  2. every installer that writes a plist reloads it through the shared helper
  3. the helper refuses to run when the library is missing -- it does NOT fall
     back to the hand-written pair, which would put the race straight back
  4. the helper actually reaches `launchd_reload`

(4) matters because of how (3) fails: with no library present, the helper exits
early, so a test that forgets to provide one never gets far enough to test
anything -- and stays green while doing it. A peer session hit exactly that in
three repositories the same night.
"""

# `str | None` in an annotation needs 3.10+ to evaluate, and this suite runs
# under /usr/bin/python3 (3.9 on this machine). Writing it without this import
# is the same defect T-0145 was filed about -- caught here by running the tests
# rather than by knowing better.
from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
COMMON = SCRIPTS / "_install-common.sh"

# The launchd installers. Discovered, not listed: a new installer should be
# covered because it is an installer, not because someone remembered to add it
# to an array here.
def launchd_installers() -> list[Path]:
    return sorted(
        p for p in SCRIPTS.glob("install-*.sh")
        if "launchd" in p.name
    )


def code_lines(path: Path) -> list[str]:
    """Script lines with comments and blanks dropped.

    _install-common.sh quotes the old pair in its own documentation, on
    purpose. Grepping the raw text would make this test fail on the very
    explanation of why it exists.
    """
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(line)
    return out


class InstallersDoNotReloadByHandTests(unittest.TestCase):
    def test_有装置脚本可查(self):
        """Denominator first: an empty glob would make every test below pass."""
        self.assertGreaterEqual(len(launchd_installers()), 7,
                                [p.name for p in launchd_installers()])

    def test_没有脚本自己发_bootout_或_bootstrap(self):
        offenders = {}
        for script in launchd_installers() + [COMMON]:
            hits = [ln.strip() for ln in code_lines(script)
                    if re.search(r"launchctl\s+(bootout|bootstrap)", ln)]
            if hits:
                offenders[script.name] = hits
        self.assertEqual(offenders, {}, f"这些脚本又自己发装载命令了：{offenders}")

    def test_每个装置脚本都通过共享_helper_重载(self):
        missing = [s.name for s in launchd_installers()
                   if not any("skill_sync_launchd_reload" in ln
                              for ln in code_lines(s))]
        self.assertEqual(missing, [], f"这些脚本没有走共享 helper：{missing}")

    def test_每个装置脚本都_source_了共享文件(self):
        """Otherwise the helper is an unbound command and the script dies at
        the very last step, after having already written the plist."""
        missing = [s.name for s in launchd_installers()
                   if not any("_install-common.sh" in ln for ln in code_lines(s))]
        self.assertEqual(missing, [], missing)


class HelperBehaviourTests(unittest.TestCase):
    """Run the helper for real. Reading the source only proves it was written."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.calls = self.tmp / "calls.txt"

    def run_helper(self, *, lib: str | None) -> subprocess.CompletedProcess:
        probe = self.tmp / "probe.sh"
        probe.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f'. "{COMMON}"\n'
            'skill_sync_launchd_reload "$1"\n',
            encoding="utf-8")
        probe.chmod(0o755)
        env = {**os.environ}
        if lib is None:
            # A path that cannot exist, to exercise the refusal.
            env["FLEET_OPS_LAUNCHD_LIB"] = str(self.tmp / "no-such-lib.sh")
        else:
            env["FLEET_OPS_LAUNCHD_LIB"] = lib
        return subprocess.run(["bash", str(probe), str(self.tmp / "x.plist")],
                              capture_output=True, text=True, env=env, timeout=60)

    def stub_lib(self, body: str = "return 0") -> str:
        """A stand-in for fleet-ops' library that records being called."""
        lib = self.tmp / "stub-launchd.sh"
        lib.write_text(
            "launchd_reload() {\n"
            f'  echo "$1" >>{self.calls}\n'
            f"  {body}\n"
            "}\n",
            encoding="utf-8")
        return str(lib)

    def test_库不在时明确失败(self):
        done = self.run_helper(lib=None)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("missing fleet-ops launchd library", done.stderr)

    def test_库不在时不回退到手写那两行(self):
        """A fallback would restore the race, and would do it on exactly the
        machines that never installed the library -- the least likely to notice."""
        done = self.run_helper(lib=None)
        self.assertNotIn("bootout", done.stdout)
        self.assertNotIn("bootstrap", done.stdout)

    def test_真的走到了_launchd_reload(self):
        """The one that is easy to lose: with no library the helper exits early,
        so a test that forgets the stub tests nothing and stays green."""
        done = self.run_helper(lib=self.stub_lib())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(self.calls.exists(), "helper 根本没调到 launchd_reload")
        self.assertEqual(self.calls.read_text(encoding="utf-8").strip(),
                         str(self.tmp / "x.plist"))

    def test_launchd_reload_失败时装置脚本跟着失败(self):
        """Exit 3 is 'the old job never went away, so I did not bootstrap'.
        Swallowing it is how the original incident stayed invisible."""
        done = self.run_helper(lib=self.stub_lib(body="return 3"))
        self.assertEqual(done.returncode, 3, done.stderr)


if __name__ == "__main__":
    unittest.main()
