"""Installers must not let the tree they happen to run from become production.

Every scripts/install-*.sh derives a repo_root and writes it into a launchd
plist -- as PYTHONPATH, as WorkingDirectory, as the program path. Whatever that
root is, it *is* the running service's code source from then on.

Until 2026-09-22 each installer derived it by self-locating, so installing from
a checkout silently made that checkout production. Six jobs on the Mac ran from
~/workspace_codex/skill-sync-sidecar for months that way. The six were not six
problems: they were one line, copied seven times.

These tests pin the two halves of the fix -- the refusal, and the fact that the
refusal cannot be bypassed by accident.
"""

import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
COMMON = SCRIPTS / "_install-common.sh"

PROBE = """#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_install-common.sh"
repo_root="$(skill_sync_resolve_repo_root "$(dirname "${BASH_SOURCE[0]}")")"
echo "resolved=${repo_root}"
"""


def make_root(base: Path, *, git: bool) -> Path:
    """A minimal tree with the shape the helper inspects."""
    (base / "src" / "skill_sync_sidecar").mkdir(parents=True)
    (base / "scripts").mkdir(parents=True)
    shutil.copy(COMMON, base / "scripts" / "_install-common.sh")
    probe = base / "scripts" / "probe.sh"
    probe.write_text(PROBE, encoding="utf-8")
    probe.chmod(0o755)
    if git:
        subprocess.run(["git", "init", "-q", str(base)], check=True)
    return probe


def run(probe: Path, **env_extra) -> subprocess.CompletedProcess:
    env = dict(os.environ, **env_extra)
    return subprocess.run(["bash", str(probe)], capture_output=True, text=True, env=env)


class InstallersRefuseWorktreesTest(unittest.TestCase):
    def test_a_git_checkout_is_refused(self):
        with TemporaryDirectory() as tmp:
            probe = make_root(Path(tmp) / "checkout", git=True)
            result = run(probe)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        # Assert on the deciding word, and assert the run stopped: a helper that
        # printed a warning and carried on would still make the checkout
        # production.
        self.assertIn("refusing to install from a development worktree", result.stderr)
        self.assertNotIn("resolved=", result.stdout)

    def test_the_refusal_says_how_to_do_it_right(self):
        """A refusal with no way forward gets bypassed, not obeyed."""
        with TemporaryDirectory() as tmp:
            probe = make_root(Path(tmp) / "checkout", git=True)
            result = run(probe)
        self.assertIn("mac-release.py", result.stderr)
        self.assertIn("SKILL_SYNC_ALLOW_WORKTREE_INSTALL=1", result.stderr)

    def test_an_artifact_without_git_is_accepted(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "artifact"
            probe = make_root(root, git=False)
            result = run(probe)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"resolved={root}", result.stdout)

    def test_the_override_is_honoured_and_says_so(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "checkout"
            probe = make_root(root, git=True)
            result = run(probe, SKILL_SYNC_ALLOW_WORKTREE_INSTALL="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"resolved={root}", result.stdout)
        self.assertIn("warning", result.stderr)

    def test_a_root_without_the_package_is_refused(self):
        """Guards against pointing an installer at, say, ~/.local/share."""
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "empty"
            (base / "scripts").mkdir(parents=True)
            shutil.copy(COMMON, base / "scripts" / "_install-common.sh")
            probe = base / "scripts" / "probe.sh"
            probe.write_text(PROBE, encoding="utf-8")
            probe.chmod(0o755)
            result = run(probe)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("not a skill-sync-sidecar root", result.stderr)


class TheCurrentSymlinkSurvivesResolutionTest(unittest.TestCase):
    """`current` must reach the plist as `current`, not as `releases/<sha>`.

    Resolving it would pin one release into the plist, and the failure mode is
    silent: the next release swaps `current`, the job keeps running the old
    sha, and nothing reports it. One `cd -P` or `realpath` added later would
    reintroduce exactly that, which is why this is a test and not a comment.
    """

    def test_resolution_keeps_the_stable_entry(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            pinned = base / "releases" / "abc123"
            make_root(pinned, git=False)
            (base / "current").symlink_to(Path("releases") / "abc123")
            result = run(base / "current" / "scripts" / "probe.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"resolved={base / 'current'}", result.stdout)
        self.assertNotIn("releases/abc123", result.stdout)


class NoInstallerDerivesItsCodeSourceFromItsOwnLocationTest(unittest.TestCase):
    """One implementation, not seven copies -- the seven copies were the bug.

    The invariant is *not* "every installer sources the helper". The systemd
    installer for the Linux peer satisfies it another way, by taking an
    explicit OPENCLAW_RELEASE_ROOT and never looking at where it sits; the
    Linux side has had release discipline all along and only the Mac side did
    not. Asserting "sources the helper" reported that correct script as broken
    -- a false alarm spends credibility, so the assertion states the thing that
    actually matters instead.
    """

    def installers(self) -> list[Path]:
        return sorted(SCRIPTS.glob("install-*.sh"))

    def root_assignments(self, path: Path):
        """Every `<something>root=` line, so a renamed variable stays covered."""
        for number, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            if re.match(r"\s*[a-z_]*root=", line):
                yield number, line

    def test_there_are_installers_to_check(self):
        self.assertTrue(self.installers(), f"no install-*.sh under {SCRIPTS}")

    def test_no_installer_self_locates_a_root(self):
        checked = 0
        for path in self.installers():
            for number, line in self.root_assignments(path):
                checked += 1
                # Self-location is allowed only behind the helper, which is
                # where the worktree refusal lives.
                before_helper = line.split("skill_sync_resolve_repo_root")[0]
                with self.subTest(script=path.name, line=number):
                    self.assertNotIn(
                        "dirname", before_helper,
                        f"{path.name}:{number} makes whatever tree it runs from "
                        f"the code source: {line.strip()}")
        self.assertTrue(checked, "no root assignments found -- the scan broke")

    def test_the_mac_installers_go_through_the_helper(self):
        """Narrowed to launchd on purpose: see the class docstring."""
        mac = [p for p in self.installers() if p.name.endswith("-launchd.sh")]
        self.assertTrue(mac, "no launchd installers found -- the scan broke")
        for path in mac:
            with self.subTest(script=path.name):
                self.assertIn("_install-common.sh", path.read_text("utf-8"))

    def test_the_helper_never_resolves_symlinks(self):
        text = COMMON.read_text("utf-8")
        body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        for forbidden in ("cd -P", "pwd -P", "realpath", "readlink -f"):
            with self.subTest(construct=forbidden):
                self.assertNotIn(forbidden, body)


if __name__ == "__main__":
    unittest.main()
