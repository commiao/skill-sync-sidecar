from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .sync_state import build_sync_status


class BaseAdoptionError(RuntimeError):
    pass


ADOPTABLE_ACTIONS = {"same_without_base", "unchanged", "already_converged"}


def build_base_adoption_preview(
    local_root: Path,
    remote_snapshot_dir: Path,
    last_applied_record: Optional[Path] = None,
) -> Dict[str, object]:
    status = build_sync_status(local_root, remote_snapshot_dir, last_applied_record)
    blocked = []
    applied = []

    for item in status["items"]:
        action = str(item["action"])
        local_hash = item.get("local_hash")
        remote_hash = item.get("remote_hash")
        if action not in ADOPTABLE_ACTIONS or not local_hash or local_hash != remote_hash:
            blocked.append(
                {
                    "skill_id": item["skill_id"],
                    "action": action,
                    "reason": item["reason"],
                    "local_hash": local_hash,
                    "remote_hash": remote_hash,
                }
            )
            continue
        applied.append(
            {
                "skill_id": item["skill_id"],
                "content_hash": local_hash,
            }
        )

    return {
        "dry_run": True,
        "mode": "base-adoption",
        "safe_to_adopt": not blocked,
        "local_root": status["local_root"],
        "remote_snapshot": status["remote_snapshot"],
        "last_applied_record": status["last_applied_record"],
        "total": status["total"],
        "summary": status["summary"],
        "adoptable": len(applied),
        "blocked": len(blocked),
        "blocked_items": blocked,
        "applied": applied,
    }


def execute_base_adoption(
    local_root: Path,
    remote_snapshot_dir: Path,
    out: Path,
    last_applied_record: Optional[Path] = None,
    remote_prefix: str = "",
) -> Dict[str, object]:
    preview = build_base_adoption_preview(local_root, remote_snapshot_dir, last_applied_record)
    if not preview["safe_to_adopt"]:
        raise BaseAdoptionError(f"base adoption has {preview['blocked']} blocked item(s)")

    snapshot_index = _load_snapshot_index(remote_snapshot_dir)
    sync_id = _timestamp_id()
    record = {
        "protocol_version": 0,
        "record_type": "skill-sync-base",
        "sync_id": sync_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target_root": str(local_root.resolve()),
        "remote_prefix": remote_prefix,
        "remote_snapshot": str(remote_snapshot_dir.resolve()),
        "snapshot_id": snapshot_index.get("snapshot_id"),
        "adoption_summary": preview["summary"],
        "applied": preview["applied"],
    }

    out = out.expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.name}.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(out)

    return {
        **preview,
        "dry_run": False,
        "status": "complete",
        "record_path": str(out.resolve()),
        "sync_id": sync_id,
        "snapshot_id": snapshot_index.get("snapshot_id"),
    }


def _load_snapshot_index(remote_snapshot_dir: Path) -> Dict[str, object]:
    index_path = remote_snapshot_dir / "index.json"
    if not index_path.exists():
        raise BaseAdoptionError(f"remote snapshot has no index.json: {remote_snapshot_dir}")
    data = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise BaseAdoptionError("remote snapshot index is not a JSON object")
    return data


def _timestamp_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
