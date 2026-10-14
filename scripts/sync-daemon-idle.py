#!/usr/bin/env python3
"""Exit 0 when sync-daemon is between cycles and can be restarted safely.

fleet-ops mac-release.py runs this (declared in mac-release.json) before it
restarts `com.skill-sync-sidecar` after a release, and keeps polling until it
exits 0 or times out. Killing the daemon in the middle of a cycle can cut a
WebDAV upload or a local apply in half, so "not sure" means "not idle".

The daemon writes state.json at the start of a cycle with an `active_cycle`
entry, and again at the end without it, then sleeps `interval_seconds`. So:

  active_cycle present         -> busy (a cycle is running)
  next cycle starts within 60s -> busy (the restart could land on its start)
  otherwise                    -> idle

Runs under /usr/bin/python3 (3.9), hence the __future__ import.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_STATE = Path.home() / "Library/Application Support/skill-sync-sidecar/state.json"
MARGIN_SECONDS = 60


def check(state_file: Path, now: datetime, margin: float = MARGIN_SECONDS) -> tuple[bool, str]:
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"cannot read {state_file}: {exc}"
    if not isinstance(state, dict):
        return False, f"{state_file} is not a JSON object"
    if state.get("daemon_status") != "running":
        return True, f"daemon_status={state.get('daemon_status')!r}, no cycle to interrupt"
    if state.get("active_cycle"):
        return False, f"cycle {state['active_cycle'].get('cycle', '?')} is running"
    try:
        updated = datetime.fromisoformat(str(state["updated_at"]))
        interval = float(state["interval_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        return False, f"state.json lacks a usable updated_at/interval_seconds: {exc}"
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    left = interval - (now - updated).total_seconds()
    if 0 <= left < margin:
        return False, f"next cycle starts in about {left:.0f}s"
    if left < 0:
        # Overdue: either sleeping past schedule or the next cycle is about to
        # write its active_cycle. Neither is a safe moment to kill it.
        return False, f"next cycle is {-left:.0f}s overdue"
    return True, f"between cycles, next one in about {left:.0f}s"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args(argv)
    idle, why = check(args.state_file, datetime.now(timezone.utc))
    print(f"sync-daemon {'idle' if idle else 'busy'}: {why}")
    return 0 if idle else 1


if __name__ == "__main__":
    sys.exit(main())
