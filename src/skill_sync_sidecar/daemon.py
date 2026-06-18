from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .remote import Remote
from .sync_cycle import run_sync_cycle


def run_sync_daemon(
    local_root: Path,
    remote: Remote,
    remote_prefix: str,
    cache_dir: Path,
    work_dir: Path,
    last_applied_record: Optional[Path] = None,
    allow_new: bool = False,
    allow_delete: bool = False,
    writer_policy: str = "push-pull",
    dry_run: bool = True,
    target: str = "cc-switch-global",
    backup_root: Optional[Path] = None,
    interval_seconds: float = 300.0,
    max_cycles: Optional[int] = None,
    stop_on_blocked: bool = True,
    state_file: Optional[Path] = None,
    base_record_file: Optional[Path] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Dict[str, object]:
    cycles: List[Dict[str, object]] = []
    count = 0
    current_base_record = last_applied_record

    while max_cycles is None or count < max_cycles:
        _write_state_file(
            state_file,
            _daemon_summary(
                dry_run,
                interval_seconds,
                max_cycles,
                stop_on_blocked,
                writer_policy,
                count,
                cycles,
                "running",
                current_base_record,
                active_cycle={"cycle": count + 1, "status": "running"},
            ),
        )
        try:
            result = run_sync_cycle(
                local_root,
                remote,
                remote_prefix,
                cache_dir,
                work_dir,
                last_applied_record=current_base_record,
                allow_new=allow_new,
                allow_delete=allow_delete,
                writer_policy=writer_policy,
                dry_run=dry_run,
                target=target,
                backup_root=backup_root,
            )
            next_record = _record_successful_base(result, base_record_file)
            if next_record is not None:
                current_base_record = next_record
            cycle = _cycle_summary(result)
        except Exception as exc:
            cycle = _cycle_error_summary(exc)
        count += 1
        cycles.append(cycle)
        _write_state_file(
            state_file,
            _daemon_summary(dry_run, interval_seconds, max_cycles, stop_on_blocked, writer_policy, count, cycles, "running", current_base_record),
        )

        if stop_on_blocked and cycle["status"] == "blocked":
            break
        if max_cycles is not None and count >= max_cycles:
            break
        sleep_fn(interval_seconds)

    summary = _daemon_summary(dry_run, interval_seconds, max_cycles, stop_on_blocked, writer_policy, count, cycles, "complete", current_base_record)
    _write_state_file(state_file, summary)
    return summary


def _daemon_summary(
    dry_run: bool,
    interval_seconds: float,
    max_cycles: Optional[int],
    stop_on_blocked: bool,
    writer_policy: str,
    count: int,
    cycles: List[Dict[str, object]],
    status: str,
    current_base_record: Optional[Path],
    active_cycle: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    summary = {
        "status": "complete",
        "daemon_status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "current_base_record": str(current_base_record.resolve()) if current_base_record else None,
        "dry_run": dry_run,
        "interval_seconds": interval_seconds,
        "max_cycles": max_cycles,
        "stop_on_blocked": stop_on_blocked,
        "writer_policy": writer_policy,
        "cycles_run": count,
        "cycles": cycles,
    }
    if active_cycle is not None:
        summary["active_cycle"] = active_cycle
    return summary


def _write_state_file(state_file: Optional[Path], payload: Dict[str, object]) -> None:
    if state_file is None:
        return
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_name(f"{state_file.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(state_file)


def _record_successful_base(result: Dict[str, object], base_record_file: Optional[Path]) -> Optional[Path]:
    if result.get("status") != "complete":
        return None
    apply_result = result.get("apply_result")
    if not isinstance(apply_result, dict):
        return None

    record_path = apply_result.get("base_record_path")
    nested = apply_result.get("apply_result")
    if not record_path and isinstance(nested, dict):
        record_path = nested.get("record_path")
    if not record_path:
        return None

    source = Path(str(record_path)).expanduser()
    if not source.exists():
        return None
    if base_record_file is None:
        return source

    base_record_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = base_record_file.with_name(f"{base_record_file.name}.tmp")
    shutil.copyfile(source, tmp)
    tmp.replace(base_record_file)
    return base_record_file


def _cycle_summary(result: Dict[str, object]) -> Dict[str, object]:
    plan = result["sync_plan"]
    return {
        "status": result["status"],
        "reason": result["reason"],
        "snapshot_id": result["snapshot_id"],
        "summary": plan["summary"],
        "blocked": plan["blocked"],
        "conflicts": result["conflicts"]["total_conflicts"] if result.get("conflicts") else 0,
        "tombstones": result["tombstones"]["total_tombstones"] if result.get("tombstones") else 0,
        "applied": result["apply_result"]["applied"] if result.get("apply_result") else 0,
        "uploaded": result["apply_result"]["uploaded"] if result.get("apply_result") else 0,
    }


def _cycle_error_summary(exc: Exception) -> Dict[str, object]:
    return {
        "status": "error",
        "reason": str(exc),
        "snapshot_id": None,
        "summary": {},
        "blocked": 0,
        "conflicts": 0,
        "tombstones": 0,
        "applied": 0,
        "uploaded": 0,
        "error_type": exc.__class__.__name__,
    }


__all__ = ["run_sync_daemon"]
