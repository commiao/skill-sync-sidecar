import json
import shutil
import threading
import unittest
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from skill_sync_sidecar import operator_executor


def _make_release(root: Path, name: str) -> Path:
    release = root / "releases" / name
    scripts = release / "scripts"
    scripts.mkdir(parents=True)
    helper = scripts / "publish-mac-peer-status.sh"
    # 替身：只回显自己在哪个发布里，不碰任何真实状态。
    helper.write_text(f"#!/bin/sh\necho {name}\n", encoding="utf-8")
    helper.chmod(0o755)
    return release


def _point_current(root: Path, release: Path) -> Path:
    current = root / "current"
    tmp = root / "current.tmp"
    tmp.symlink_to(release)
    tmp.replace(current)
    return current


class OperatorExecutorRepoRootTest(unittest.TestCase):
    """launchd 以 --repo-root …/current 启动常驻 executor；它必须一直跟着 current 走。"""

    def _serve(self, repo_root: Path):
        servers = []
        real_server = operator_executor.ThreadingHTTPServer

        def capture(*args, **kwargs):
            server = real_server(*args, **kwargs)
            servers.append(server)
            return server

        patcher = mock.patch.object(operator_executor, "ThreadingHTTPServer", side_effect=capture)
        patcher.start()
        self.addCleanup(patcher.stop)
        thread = threading.Thread(
            target=operator_executor.serve_operator_executor,
            args=("127.0.0.1", 0, repo_root),
            daemon=True,
        )
        with mock.patch("builtins.print"):
            thread.start()
            for _ in range(200):
                if servers:
                    break
                threading.Event().wait(0.01)
        self.assertTrue(servers, "executor did not start")
        server = servers[0]
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def _healthz(self, server) -> dict:
        url = f"http://127.0.0.1:{server.server_port}/healthz"
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_executor_keeps_the_stable_entry_point(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _point_current(root, _make_release(root, "a"))
            server = self._serve(current)
            self.assertEqual(self._healthz(server)["repo_root"], str(current.absolute()))

    def test_helpers_still_run_after_the_startup_release_is_pruned(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _make_release(root, "a")
            current = _point_current(root, first)
            server = self._serve(current)
            repo_root = Path(self._healthz(server)["repo_root"])

            _point_current(root, _make_release(root, "b"))
            shutil.rmtree(first)

            result = operator_executor.run_mac_peer_status_refresh(repo_root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stdout_tail"].strip(), "b")


if __name__ == "__main__":
    unittest.main()
