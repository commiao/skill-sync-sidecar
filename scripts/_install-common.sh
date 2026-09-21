# Shared by every scripts/install-*.sh. Source it; do not execute it.
#
# Why this exists
# ---------------
# Each installer used to derive its own repo root:
#
#     repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
#
# That line is correct when the installer runs from a release artifact
# (current/scripts/install-x.sh -> current/), and wrong when it runs from a
# development checkout: whichever tree you happen to run it from silently
# becomes production's code source. Six launchd jobs on this Mac ran from
# ~/workspace_codex/skill-sync-sidecar for months because of it.
#
# A worktree as the code source costs three things, all observed:
#   - working on a branch cannot change the running service, so there is no
#     release step to insert
#   - deleting the tree stops the jobs *silently*
#   - rewriting a script while it executes crashes it (sh reads as it runs)
#
# So the derivation stays, and running from a worktree becomes a hard error.
#
# The test is "is this root a git worktree", not "does the path look like
# ~/workspace*". A `git archive` artifact has no .git; a checkout does. That
# reads behaviour instead of names, and it still holds on the Linux peer where
# no /Users/... pattern would match.

skill_sync_resolve_repo_root() {
  # $1: the directory of the calling script (its scripts/ directory)
  local script_dir="$1"
  local root

  # Logical path on purpose: no `cd -P`, no `pwd -P`, no realpath. Resolving
  # symlinks here would turn .../current into .../releases/<sha>, which pins
  # one release into the generated plist -- the next release then swaps
  # `current` and has no effect, with nothing reporting it.
  root="$(cd "${script_dir}/.." && pwd)"

  if [ ! -d "${root}/src/skill_sync_sidecar" ]; then
    echo "not a skill-sync-sidecar root (no src/skill_sync_sidecar): ${root}" >&2
    return 2
  fi

  if git -C "${root}" rev-parse --git-dir >/dev/null 2>&1; then
    if [ "${SKILL_SYNC_ALLOW_WORKTREE_INSTALL:-0}" != "1" ]; then
      cat >&2 <<MSG
refusing to install from a development worktree: ${root}

Installing from a checkout makes that checkout production's code source: the
jobs then have no release step, and deleting the tree stops them silently.

Install from a release artifact instead:

    fleet-ops/bin/mac-release.py skill-sync-sidecar --repo ${root}
    ~/.local/share/skill-sync-sidecar/current/scripts/$(basename "$0")

Point the installer at current/, never at releases/<sha>: a pinned sha makes
the next release silently ineffective.

To install from this checkout anyway (development only, not a service you
intend to leave running), set SKILL_SYNC_ALLOW_WORKTREE_INSTALL=1.
MSG
      return 2
    fi
    echo "warning: installing from worktree ${root} (SKILL_SYNC_ALLOW_WORKTREE_INSTALL=1)" >&2
  fi

  printf '%s\n' "${root}"
}
