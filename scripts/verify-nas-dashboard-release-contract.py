#!/usr/bin/env python3
"""Verify the narrow release contract for the read-only NAS dashboard.

The NAS gateway only reads the canonical snapshot and peer status.  A dashboard
change therefore must not be held hostage by unrelated pending sync decisions,
but that is safe only while the release contains *only* dashboard presentation
code and the checks that define this boundary.  This script makes that boundary
machine-verifiable instead of accepting an operator-supplied bypass flag.
"""

from __future__ import annotations

import argparse
import configparser
import subprocess
import sys
from pathlib import Path


PROJECT_NAME = "skill-sync-sidecar"

ALLOWED_PATHS = frozenset(
    {
        "src/skill_sync_sidecar/dashboard.py",
        "tests/test_ops_status.py",
        "tests/test_release_contract.py",
        "scripts/verify-release.sh",
        "scripts/verify-nas-dashboard-release-contract.py",
        "docs/release.md",
        "docs/nas-gateway-deployment.md",
    }
)

# These are checked against the rendered gateway HTML after a deployment.  Keep
# them here with the contract, rather than relying on a stale generic probe.
DASHBOARD_HTML_CHECKS = (
    "dashboard-sidebar",
    "skills-hub-header",
    "selectSkillsHubView",
    "renderSkillsHubChrome",
)


def project_name(repo: Path) -> str | None:
    metadata = configparser.ConfigParser()
    if not metadata.read(repo / "setup.cfg", encoding="utf-8"):
        return None
    return metadata.get("metadata", "name", fallback=None)


def run_git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def validate(repo: Path, base: str) -> list[str]:
    errors: list[str] = []
    if project_name(repo) != PROJECT_NAME:
        errors.append(
            f"nas-dashboard-only belongs to {PROJECT_NAME}; "
            "the release repository does not identify as that project"
        )
        return errors
    if run_git(repo, "status", "--porcelain"):
        errors.append("release worktree must be clean")

    if subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", base, "HEAD"],
        check=False,
    ).returncode:
        errors.append(f"HEAD is not descended from {base}")
        return errors

    changed = [line for line in run_git(repo, "diff", "--name-only", f"{base}..HEAD").splitlines() if line]
    if not changed:
        errors.append(f"no release changes found after {base}")
        return errors

    unexpected = sorted(set(changed) - ALLOWED_PATHS)
    if unexpected:
        errors.append("dashboard-only contract rejects paths outside its allowlist: " + ", ".join(unexpected))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--base", default="origin/main", help="release mainline ref (default: origin/main)")
    args = parser.parse_args()

    errors = validate(args.repo.resolve(), args.base)
    if errors:
        for error in errors:
            print(f"nas_dashboard_contract=failed: {error}", file=sys.stderr)
        return 2

    print("nas_dashboard_contract=ok")
    print(f"project={PROJECT_NAME}")
    print("allowed_paths=" + ",".join(sorted(ALLOWED_PATHS)))
    print("html_checks=" + ",".join(DASHBOARD_HTML_CHECKS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
