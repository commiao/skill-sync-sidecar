import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


REPO = Path(__file__).resolve().parents[1]
CONTRACT = REPO / "scripts" / "verify-nas-dashboard-release-contract.py"
RELEASE_GATE = REPO / "scripts" / "verify-release.sh"


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def commit(repo: Path, message: str) -> None:
    subprocess.check_call(["git", "-C", str(repo), "add", "."])
    subprocess.check_call(["git", "-C", str(repo), "commit", "-qm", message])


class NasDashboardReleaseContractTest(unittest.TestCase):
    def make_repo(self, base: Path) -> tuple[Path, str]:
        repo = base / "repo"
        subprocess.check_call(["git", "init", "-q", str(repo)])
        git(repo, "config", "user.name", "release-test")
        git(repo, "config", "user.email", "release-test@example.invalid")
        repo.joinpath("README.md").write_text("base\n", encoding="utf-8")
        repo.joinpath("setup.cfg").write_text("[metadata]\nname = skill-sync-sidecar\n", encoding="utf-8")
        commit(repo, "base")
        return repo, git(repo, "rev-parse", "HEAD")

    def verify(self, repo: Path, base: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CONTRACT), "--repo", str(repo), "--base", base],
            capture_output=True,
            text=True,
        )

    def test_allows_only_dashboard_contract_paths(self):
        with TemporaryDirectory() as tmp:
            repo, base = self.make_repo(Path(tmp))
            dashboard = repo / "src" / "skill_sync_sidecar" / "dashboard.py"
            dashboard.parent.mkdir(parents=True)
            dashboard.write_text("DASHBOARD_HTML = 'new-ui'\n", encoding="utf-8")
            commit(repo, "dashboard")
            result = self.verify(repo, base)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nas_dashboard_contract=ok", result.stdout)
        self.assertIn("dashboard-sidebar", result.stdout)

    def test_rejects_other_project_even_with_matching_dashboard_path(self):
        with TemporaryDirectory() as tmp:
            repo, base = self.make_repo(Path(tmp))
            repo.joinpath("setup.cfg").write_text("[metadata]\nname = other-service\n", encoding="utf-8")
            dashboard = repo / "src" / "skill_sync_sidecar" / "dashboard.py"
            dashboard.parent.mkdir(parents=True)
            dashboard.write_text("DASHBOARD_HTML = 'new-ui'\n", encoding="utf-8")
            commit(repo, "other project dashboard")
            result = self.verify(repo, base)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("belongs to skill-sync-sidecar", result.stderr)

    def test_rejects_missing_project_identity(self):
        with TemporaryDirectory() as tmp:
            repo, base = self.make_repo(Path(tmp))
            repo.joinpath("setup.cfg").unlink()
            dashboard = repo / "src" / "skill_sync_sidecar" / "dashboard.py"
            dashboard.parent.mkdir(parents=True)
            dashboard.write_text("DASHBOARD_HTML = 'new-ui'\n", encoding="utf-8")
            commit(repo, "dashboard without project identity")
            result = self.verify(repo, base)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("belongs to skill-sync-sidecar", result.stderr)

    def test_rejects_sync_or_write_path(self):
        with TemporaryDirectory() as tmp:
            repo, base = self.make_repo(Path(tmp))
            remote = repo / "src" / "skill_sync_sidecar" / "remote.py"
            remote.parent.mkdir(parents=True)
            remote.write_text("changed = True\n", encoding="utf-8")
            commit(repo, "remote change")
            result = self.verify(repo, base)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside its allowlist", result.stderr)
        self.assertIn("remote.py", result.stderr)

    def test_rejects_dirty_release_worktree(self):
        with TemporaryDirectory() as tmp:
            repo, base = self.make_repo(Path(tmp))
            dashboard = repo / "src" / "skill_sync_sidecar" / "dashboard.py"
            dashboard.parent.mkdir(parents=True)
            dashboard.write_text("DASHBOARD_HTML = 'new-ui'\n", encoding="utf-8")
            commit(repo, "dashboard")
            dashboard.write_text("DASHBOARD_HTML = 'dirty'\n", encoding="utf-8")
            result = self.verify(repo, base)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("worktree must be clean", result.stderr)

    def test_release_gate_exposes_the_narrow_profile(self):
        text = RELEASE_GATE.read_text(encoding="utf-8")
        self.assertIn("nas-dashboard-only", text)
        self.assertIn("verify-nas-dashboard-release-contract.py", text)
        self.assertIn("ops-status --allow-new --fail-on-blocked --fail-on-error", text)


if __name__ == "__main__":
    unittest.main()
