"""A blink of the network must not be reported as a job failure.

publish-mac-peer-status runs every 300s. Its error log had grown to 223KB of
tracebacks that nobody had ever read (T-0143), and reading it is what settled
the design: 45 network failures, 31 already rescued by the in-run retry, and 6
of the remaining 14 were "no route to host" -- the Mac had no network at all.
Retrying harder does not reach a server there is no route to, and every one of
those runs was followed 300s later by one that succeeded.

So the threshold counts *runs in a row*, the shape monitor.py already uses for
the dashboard poll. Two halves are pinned here, and the second is the one that
is easy to lose:

  * below the threshold the job exits 0
  * below the threshold the Python traceback is *withheld from stderr*

The second half is why the log grew, and it is what the fleet-ops job-failure
sweep keys on. Exiting 0 while still printing the traceback would leave that
sweep lit on every blink -- a lamp that is always on is not a lamp.
"""

import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "publish-mac-peer-status.sh"

# What the real failure looked like in the log, trimmed to its two load-bearing
# lines: the traceback marker the sweep keys on, and the urlopen reason the
# transient line is expected to quote.
FAKE_FAILING_PYTHON = """#!/usr/bin/env bash
cat >&2 <<'EOF'
Traceback (most recent call last):
  File "remote.py", line 134, in _request
    raise RemoteError(...)
urllib.error.URLError: <urlopen error EOF occurred in violation of protocol (_ssl.c:1129)>
EOF
exit 1
"""

FAKE_OK_PYTHON = """#!/usr/bin/env bash
echo "peer_status_published=true"
exit 0
"""


class PublishRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "failures"

    def python_stub(self, body: str) -> Path:
        path = self.tmp / "fake-python"
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def run_script(self, body: str, *, threshold: str = "3") -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "PYTHON": str(self.python_stub(body)),
            "SKILL_SYNC_PUBLISH_FAILURE_STATE": str(self.state),
            "SKILL_SYNC_PUBLISH_FAILURE_THRESHOLD": threshold,
            # Keep the test fast: one attempt, no sleeping.
            "SKILL_SYNC_PUBLISH_RETRY_ATTEMPTS": "1",
            "SKILL_SYNC_PUBLISH_RETRY_DELAYS": "0",
        }
        return subprocess.run(["bash", str(SCRIPT)], env=env,
                              capture_output=True, text=True, timeout=120)

    def counter(self) -> str:
        return self.state.read_text(encoding="utf-8").strip() if self.state.exists() else ""

    def test_成功时退出0并把计数清零(self) -> None:
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text("2\n", encoding="utf-8")
        done = self.run_script(FAKE_OK_PYTHON)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.counter(), "0")

    def test_单次失败算瞬断_退出0(self) -> None:
        done = self.run_script(FAKE_FAILING_PYTHON)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("transiently", done.stderr)
        self.assertEqual(self.counter(), "1")

    def test_瞬断时不许把_traceback_写进日志(self) -> None:
        """这一半最容易丢：退出码改对了，traceback 照样在涨日志、照样点亮巡检。"""
        done = self.run_script(FAKE_FAILING_PYTHON)
        self.assertNotIn("Traceback (most recent call last):", done.stderr)

    def test_瞬断那一行要说清原因(self) -> None:
        """出口要能当场看懂，否则只是换了个地方堆积。"""
        done = self.run_script(FAKE_FAILING_PYTHON)
        self.assertIn("_ssl.c:1129", done.stderr)

    def test_连续失败达到阈值就失败并放出_traceback(self) -> None:
        for expected in ("1", "2"):
            done = self.run_script(FAKE_FAILING_PYTHON)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(self.counter(), expected)

        done = self.run_script(FAKE_FAILING_PYTHON)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Traceback (most recent call last):", done.stderr,
                      "持续失败时必须留下可诊断的东西")
        self.assertIn("3 runs in a row", done.stderr)

    def test_一次成功就把连续计数打断(self) -> None:
        """判据是「连续」。少了这一条，偶发失败会慢慢累积到阈值，报一件不存在的事。"""
        self.run_script(FAKE_FAILING_PYTHON)
        self.run_script(FAKE_FAILING_PYTHON)
        self.assertEqual(self.counter(), "2")

        self.run_script(FAKE_OK_PYTHON)
        self.assertEqual(self.counter(), "0")

        done = self.run_script(FAKE_FAILING_PYTHON)
        self.assertEqual(done.returncode, 0, "计数没被成功打断")

    def test_计数文件是垃圾时当0处理而不是让作业失败(self) -> None:
        """坏掉的计数器不该成为「发布停了」的原因。"""
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text("不是数字\n", encoding="utf-8")
        done = self.run_script(FAKE_FAILING_PYTHON)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.counter(), "1")

    def test_阈值为1时第一次失败就报(self) -> None:
        """阈值可调，且调到 1 就是原来的行为 —— 别把「能调回去」弄丢。"""
        done = self.run_script(FAKE_FAILING_PYTHON, threshold="1")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Traceback (most recent call last):", done.stderr)


if __name__ == "__main__":
    unittest.main()
