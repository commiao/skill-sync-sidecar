from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


class ApplyPlanError(RuntimeError):
    pass


class ApplyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApplyPlanItem:
    key: str
    skill_id: str
    content_hash: str
    source_path: str
    target_path: str
    backup_path: str
    action: str
    scope: str
    allowed: bool
    reason: Optional[str] = None


def build_apply_plan(
    staged_snapshot_dir: Path,
    target: str,
    target_root: Optional[Path] = None,
    project_root: Optional[Path] = None,
) -> Dict[str, object]:
    stage_index_path = staged_snapshot_dir / ".stage-index.json"
    if not stage_index_path.exists():
        raise ApplyPlanError(f"staged snapshot has no .stage-index.json: {staged_snapshot_dir}")
    stage_index = json.loads(stage_index_path.read_text(encoding="utf-8"))
    apply_id = _timestamp_id()
    items: List[ApplyPlanItem] = []

    if target == "cc-switch-global":
        root = target_root or Path.home() / ".cc-switch" / "skills"
        backup_root = root / ".skill-sync-backups" / apply_id
        for skill in stage_index.get("skills", []):
            skill_id = str(skill.get("skill_id"))
            scope = str(skill.get("scope") or "global")
            allowed = scope == "global"
            items.append(
                ApplyPlanItem(
                    key=str(skill.get("key")),
                    skill_id=skill_id,
                    content_hash=str(skill.get("content_hash") or ""),
                    source_path=str(skill.get("output_path")),
                    target_path=str(root / skill_id),
                    backup_path=str(backup_root / skill_id),
                    action="install_or_replace" if allowed else "skip",
                    scope=scope,
                    allowed=allowed,
                    reason=None if allowed else "project-scoped skills are not installed into global roots",
                )
            )
    elif target == "codex-project":
        if project_root is None:
            raise ApplyPlanError("--project-root is required for codex-project")
        root = project_root / "skills"
        backup_root = project_root / ".skill-sync-backups" / apply_id
        for skill in stage_index.get("skills", []):
            skill_id = str(skill.get("skill_id"))
            scope = str(skill.get("scope") or "global")
            allowed = scope == "project"
            items.append(
                ApplyPlanItem(
                    key=str(skill.get("key")),
                    skill_id=skill_id,
                    content_hash=str(skill.get("content_hash") or ""),
                    source_path=str(skill.get("output_path")),
                    target_path=str(root / skill_id),
                    backup_path=str(backup_root / skill_id),
                    action="install_or_replace" if allowed else "skip",
                    scope=scope,
                    allowed=allowed,
                    reason=None if allowed else "global skills are not installed into project roots by default",
                )
            )
    else:
        raise ApplyPlanError(f"unsupported apply target: {target}")

    return {
        "apply_id": apply_id,
        "target": target,
        "target_root": str(root.resolve()),
        "backup_root": str(backup_root.resolve()),
        "snapshot_id": stage_index.get("snapshot_id"),
        "staged_snapshot": str(staged_snapshot_dir.resolve()),
        "dry_run": True,
        "total": len(items),
        "allowed": sum(1 for item in items if item.allowed),
        "skipped": sum(1 for item in items if not item.allowed),
        "items": [item.__dict__ for item in items],
    }


def execute_apply_plan(plan: Dict[str, object]) -> Dict[str, object]:
    apply_id = str(plan.get("apply_id") or _timestamp_id())
    backup_root = Path(str(plan["backup_root"]))
    record_path = backup_root / ".apply-record.json"
    applied: List[Dict[str, object]] = []
    skipped = [item for item in plan.get("items", []) if not item.get("allowed")]
    record: Dict[str, object] = {
        "protocol_version": 0,
        "record_type": "skill-sync-apply",
        "apply_id": apply_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "dry_run": False,
        "target": plan.get("target"),
        "target_root": plan.get("target_root"),
        "backup_root": str(backup_root),
        "snapshot_id": plan.get("snapshot_id"),
        "staged_snapshot": plan.get("staged_snapshot"),
        "applied": applied,
        "skipped": skipped,
    }

    try:
        backup_root.mkdir(parents=True, exist_ok=True)
        for item in plan.get("items", []):
            if not item.get("allowed"):
                continue
            applied.append(_apply_item(item, apply_id))
        record["status"] = "complete"
        record["completed_at"] = datetime.now(timezone.utc).isoformat()
        return _write_apply_record(record, record_path)
    except Exception as exc:
        record["status"] = "failed"
        record["error"] = str(exc)
        record["failed_at"] = datetime.now(timezone.utc).isoformat()
        _write_apply_record(record, record_path)
        if isinstance(exc, ApplyError):
            raise
        raise ApplyError(str(exc)) from exc


def rollback_apply_record(record_path: Path) -> Dict[str, object]:
    if not record_path.exists():
        raise ApplyError(f"apply record not found: {record_path}")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    rolled_back: List[Dict[str, object]] = []
    for item in reversed(record.get("applied", [])):
        target = Path(str(item["target_path"]))
        backup = Path(str(item["backup_path"]))
        had_existing = bool(item.get("had_existing"))
        if had_existing:
            if not backup.exists():
                raise ApplyError(f"backup missing for rollback: {backup}")
            _remove_path(target)
            _copy_path(backup, target)
            action = "restored_backup"
        else:
            _remove_path(target)
            action = "removed_new_install"
        rolled_back.append(
            {
                "key": item.get("key"),
                "skill_id": item.get("skill_id"),
                "target_path": str(target),
                "action": action,
            }
        )

    rollback = {
        "protocol_version": 0,
        "record_type": "skill-sync-rollback",
        "apply_id": record.get("apply_id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "record_path": str(record_path.resolve()),
        "rolled_back": rolled_back,
        "total": len(rolled_back),
    }
    rollback_path = record_path.parent / f".rollback-record-{_timestamp_id()}.json"
    rollback_path.write_text(json.dumps(rollback, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rollback["rollback_record_path"] = str(rollback_path)
    return rollback


def _apply_item(item: Dict[str, object], apply_id: str) -> Dict[str, object]:
    source = Path(str(item["source_path"]))
    target = Path(str(item["target_path"]))
    backup = Path(str(item["backup_path"]))
    if not source.is_dir():
        raise ApplyError(f"staged source is not a directory: {source}")

    target.parent.mkdir(parents=True, exist_ok=True)
    temp_target = target.parent / f".{target.name}.skill-sync-tmp-{apply_id}"
    if temp_target.exists():
        raise ApplyError(f"temporary target already exists: {temp_target}")
    _copy_path(source, temp_target)

    had_existing = target.exists()
    backup_written = False
    try:
        if had_existing:
            if backup.exists():
                raise ApplyError(f"backup path already exists: {backup}")
            backup.parent.mkdir(parents=True, exist_ok=True)
            _copy_path(target, backup)
            backup_written = True

        _remove_path(target)
        temp_target.replace(target)
    except Exception:
        if had_existing and backup.exists() and not target.exists():
            _copy_path(backup, target)
        _remove_path(temp_target)
        raise

    return {
        "key": item.get("key"),
        "skill_id": item.get("skill_id"),
        "content_hash": item.get("content_hash"),
        "source_path": str(source),
        "target_path": str(target),
        "backup_path": str(backup),
        "had_existing": had_existing,
        "backup_written": backup_written,
        "action": "installed_or_replaced",
    }


def _write_apply_record(record: Dict[str, object], record_path: Path) -> Dict[str, object]:
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    record["record_path"] = str(record_path)
    record["total_applied"] = len(record.get("applied", []))
    record["total_skipped"] = len(record.get("skipped", []))
    return record


def _copy_path(source: Path, target: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _timestamp_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
