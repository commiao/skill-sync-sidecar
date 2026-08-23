from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterable, Optional, Sequence
from zipfile import ZipFile

from .local_skill import DEFAULT_LOCAL_TOOL_TARGETS, LocalToolTarget
from .scanner import scan_skill
from .stage import StageError, stage_snapshot


class LegacySkillshubMigrationError(RuntimeError):
    pass


LEGACY_ROOT_NAME = "skillshub"
TOOL_ALIASES = {
    "cc-switch": {"cc-switch"},
    "codex": {"codex"},
    "cursor": {"cursor"},
    "claude-code": {"claude-code", "claude"},
    "qoder": {"qoder"},
    "deepseek-harness": {"deepseek-harness", "deepseek"},
}


def default_legacy_skillshub_root() -> Path:
    return Path.home() / ".skillshub"


def default_central_snapshot_root() -> Path:
    return Path.home() / "public-sync" / "skill-sync-sidecar-dev" / "current-mac"


def build_legacy_skillshub_migration_preview(
    legacy_root: Path,
    remote_snapshot_dir: Path,
    *,
    consumer_roots: Optional[dict[str, Path]] = None,
) -> dict:
    legacy = _require_legacy_root(legacy_root)
    snapshot = _load_snapshot(remote_snapshot_dir)
    central = _central_by_skill_id(snapshot, remote_snapshot_dir)
    consumers = _consumer_roots(consumer_roots)
    items = []

    for skill_path in _legacy_skill_dirs(legacy):
        record = scan_skill(LEGACY_ROOT_NAME, skill_path, skill_path / "SKILL.md")
        links = _consumer_links(skill_path, consumers)
        central_item = central.get(record.skill_id)
        central_state, central_reason = _central_state(record, central_item)
        allowed_links, blocked_links = _classify_links(links, central_item, central_state)
        action, action_detail = _action_for(central_state, links, allowed_links, blocked_links)
        items.append(
            {
                "skill_id": record.skill_id,
                "name": record.name or record.skill_id,
                "description": record.description or "",
                "legacy_path": str(skill_path),
                "legacy_content_hash": record.content_hash,
                "legacy_risk": record.risk_level,
                "central_state": central_state,
                "central_reason": central_reason,
                "central_content_hash": central_item.get("content_hash") if central_item else None,
                "central_targets": list(central_item.get("targets") or []) if central_item else [],
                "links": links,
                "detachable_links": allowed_links,
                "blocked_links": blocked_links,
                "action": action,
                "action_detail": action_detail,
            }
        )

    items.sort(key=lambda item: str(item["skill_id"]))
    summary = {
        "legacy_skills": len(items),
        "linked_skills": sum(1 for item in items if item["links"]),
        "linked_entries": sum(len(item["links"]) for item in items),
        "central_missing": sum(1 for item in items if item["central_state"] == "missing"),
        "central_match": sum(1 for item in items if item["central_state"] == "match"),
        "central_changed": sum(1 for item in items if item["central_state"] == "changed"),
        "detachable_skills": sum(1 for item in items if item["detachable_links"]),
        "detachable_links": sum(len(item["detachable_links"]) for item in items),
        "blocked_links": sum(len(item["blocked_links"]) for item in items),
    }
    return {
        "ok": True,
        "record_type": "skill-sync-legacy-skillshub-migration-preview",
        "mode": "dry_run",
        "dry_run": True,
        "legacy_root": str(legacy),
        "remote_snapshot": str(Path(remote_snapshot_dir).expanduser().resolve()),
        "snapshot_id": snapshot.get("snapshot_id"),
        "summary": summary,
        "operator_action": _operator_action(summary),
        "items": items,
    }


def build_legacy_skillshub_peer_report(
    legacy_root: Path,
    remote_snapshot_dir: Path,
    *,
    consumer_roots: Optional[dict[str, Path]] = None,
    measured_at: Optional[str] = None,
) -> dict:
    """Return a WebDAV-safe legacy dependency report without local paths."""
    observed_at = measured_at or datetime.now(timezone.utc).isoformat()
    try:
        preview = build_legacy_skillshub_migration_preview(
            legacy_root,
            remote_snapshot_dir,
            consumer_roots=consumer_roots,
        )
    except (LegacySkillshubMigrationError, OSError, ValueError):
        return {
            "legacy_skillshub_report_version": 1,
            "available": False,
            "measured_at": observed_at,
            "reason": "旧 Skillshub 目录或共享库快照不可读取。",
            "summary": {},
            "items": [],
        }

    items = []
    for item in preview["items"]:
        links = item.get("links") if isinstance(item.get("links"), list) else []
        tools = sorted(
            {
                str(link.get("tool_id"))
                for link in links
                if isinstance(link, dict) and link.get("tool_id")
            }
        )
        items.append(
            {
                "skill_id": item["skill_id"],
                "name": item["name"],
                "central_state": item["central_state"],
                "central_reason": item["central_reason"],
                "tools": tools,
                "link_count": len(links),
                "detachable_link_count": len(item.get("detachable_links") or []),
                "action": item["action"],
                "action_detail": item["action_detail"],
            }
        )
    return {
        "legacy_skillshub_report_version": 1,
        "available": True,
        "measured_at": observed_at,
        "snapshot_id": preview.get("snapshot_id"),
        "summary": preview["summary"],
        "items": items,
    }


def execute_legacy_skillshub_migration(
    legacy_root: Path,
    remote_snapshot_dir: Path,
    skill_ids: Sequence[str],
    *,
    consumer_roots: Optional[dict[str, Path]] = None,
    record_out: Optional[Path] = None,
    yes: bool = False,
    allow_local_writes: bool = False,
) -> dict:
    preview = build_legacy_skillshub_migration_preview(
        legacy_root,
        remote_snapshot_dir,
        consumer_roots=consumer_roots,
    )
    selected = _normalize_skill_ids(skill_ids)
    if not selected:
        raise LegacySkillshubMigrationError("select at least one verified skill before migration")
    by_id = {str(item["skill_id"]): item for item in preview["items"]}
    missing_ids = [skill_id for skill_id in selected if skill_id not in by_id]
    if missing_ids:
        raise LegacySkillshubMigrationError(f"legacy skill not found: {', '.join(missing_ids)}")
    candidates = [by_id[skill_id] for skill_id in selected]
    unsafe = [item["skill_id"] for item in candidates if not item["detachable_links"]]
    if unsafe:
        raise LegacySkillshubMigrationError(
            f"selected skill is not safe to detach from the legacy root: {', '.join(unsafe)}"
        )
    if not yes:
        return {
            **preview,
            "selected_skill_ids": selected,
            "planned_links": sum(len(item["detachable_links"]) for item in candidates),
            "safe_to_migrate": True,
        }
    if not allow_local_writes:
        raise LegacySkillshubMigrationError("local writes are disabled; start the operator executor with --allow-local-writes")

    migration_id = _timestamp_id()
    record_path = (record_out or _default_record_path(migration_id)).expanduser()
    if record_path.exists():
        raise LegacySkillshubMigrationError(f"migration record already exists: {record_path}")
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "protocol_version": 1,
        "record_type": "skill-sync-legacy-skillshub-migration",
        "migration_id": migration_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "legacy_root": preview["legacy_root"],
        "remote_snapshot": preview["remote_snapshot"],
        "snapshot_id": preview["snapshot_id"],
        "selected_skill_ids": selected,
        "applied": [],
    }
    _write_record(record_path, record)

    try:
        with TemporaryDirectory(prefix="skill-sync-legacy-skillshub-") as tmp:
            staged = stage_snapshot(Path(remote_snapshot_dir).expanduser(), Path(tmp), clean=True)
            staged_by_id = _staged_by_skill_id(staged)
            for item in candidates:
                source = staged_by_id.get(str(item["skill_id"]))
                if source is None:
                    raise LegacySkillshubMigrationError(f"central stage is missing: {item['skill_id']}")
                for link in item["detachable_links"]:
                    record["applied"].append(
                        _replace_legacy_link(
                            Path(str(link["target_path"])),
                            Path(source),
                            migration_id,
                            record_path.parent,
                            item,
                            link,
                        )
                    )
                    _write_record(record_path, record)
        record["status"] = "complete"
        record["completed_at"] = datetime.now(timezone.utc).isoformat()
        record["migrated_links"] = len(record["applied"])
        _write_record(record_path, record)
        return {"ok": True, "dry_run": False, "record_path": str(record_path), **record}
    except Exception as exc:
        rollback_errors = _rollback_applied_links(record.get("applied") or [])
        record["status"] = "rolled_back_after_failure" if not rollback_errors else "failed"
        record["error"] = str(exc)
        record["rollback_errors"] = rollback_errors
        record["failed_at"] = datetime.now(timezone.utc).isoformat()
        _write_record(record_path, record)
        if isinstance(exc, LegacySkillshubMigrationError):
            raise
        if isinstance(exc, StageError):
            raise LegacySkillshubMigrationError(str(exc)) from exc
        raise LegacySkillshubMigrationError(str(exc)) from exc


def rollback_legacy_skillshub_migration(record_path: Path) -> dict:
    path = Path(record_path).expanduser()
    if not path.exists():
        raise LegacySkillshubMigrationError(f"migration record not found: {path}")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LegacySkillshubMigrationError(f"cannot read migration record: {exc}") from exc
    if record.get("record_type") != "skill-sync-legacy-skillshub-migration":
        raise LegacySkillshubMigrationError("record is not a legacy Skillshub migration")
    if record.get("status") != "complete":
        raise LegacySkillshubMigrationError(f"cannot rollback record with status={record.get('status')}")

    rolled_back = []
    for item in reversed(record.get("applied") or []):
        target = Path(str(item["target_path"]))
        backup = Path(str(item["backup_link_path"]))
        expected_hash = str(item.get("central_content_hash") or "")
        if not backup.is_symlink():
            raise LegacySkillshubMigrationError(f"legacy link backup is missing: {backup}")
        if not target.exists() or target.is_symlink():
            raise LegacySkillshubMigrationError(f"migrated target is missing or not materialized: {target}")
        resolved_target = target.resolve()
        current = scan_skill("rollback-check", resolved_target, resolved_target / "SKILL.md")
        if expected_hash and current.content_hash != expected_hash:
            raise LegacySkillshubMigrationError(f"target changed after migration; rollback is blocked: {target}")
        _remove_path(target)
        os.symlink(os.readlink(backup), target, target_is_directory=True)
        rolled_back.append({"skill_id": item["skill_id"], "tool_id": item["tool_id"], "target_path": str(target)})

    rollback = {
        "ok": True,
        "record_type": "skill-sync-legacy-skillshub-migration-rollback",
        "record_path": str(path),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "rolled_back": rolled_back,
        "total": len(rolled_back),
    }
    rollback_path = path.parent / f"rollback-{_timestamp_id()}.json"
    rollback_path.write_text(json.dumps(rollback, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rollback["rollback_record_path"] = str(rollback_path)
    return rollback


def _require_legacy_root(path: Path) -> Path:
    root = Path(path).expanduser()
    if not root.exists() or not root.is_dir():
        raise LegacySkillshubMigrationError(f"legacy Skillshub root not found: {root}")
    if root.is_symlink():
        raise LegacySkillshubMigrationError(f"legacy Skillshub root must be a real directory: {root}")
    return root.resolve()


def _load_snapshot(snapshot_dir: Path) -> dict:
    path = Path(snapshot_dir).expanduser() / "index.json"
    if not path.exists():
        raise LegacySkillshubMigrationError(f"central snapshot index not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LegacySkillshubMigrationError(f"cannot read central snapshot index: {exc}") from exc
    if not isinstance(data.get("skills"), list):
        raise LegacySkillshubMigrationError("central snapshot has no skills list")
    return data


def _central_by_skill_id(snapshot: dict, snapshot_dir: Path) -> dict[str, dict]:
    by_id: dict[str, dict] = {}
    duplicates: set[str] = set()
    for raw in snapshot.get("skills") or []:
        if not isinstance(raw, dict):
            continue
        skill_id = str(raw.get("skill_id") or "").strip()
        if not skill_id:
            continue
        if skill_id in by_id:
            duplicates.add(skill_id)
        else:
            item = dict(raw)
            item["_snapshot_dir"] = str(Path(snapshot_dir).expanduser().resolve())
            by_id[skill_id] = item
    for skill_id in duplicates:
        by_id.pop(skill_id, None)
    return by_id


def _legacy_skill_dirs(root: Path) -> Iterable[Path]:
    for path in sorted(root.iterdir()):
        if path.name.startswith(".") or path.is_symlink() or not path.is_dir():
            continue
        if (path / "SKILL.md").is_file():
            yield path


def _consumer_roots(overrides: Optional[dict[str, Path]]) -> dict[str, Path]:
    if overrides is not None:
        return {str(tool_id): Path(path).expanduser() for tool_id, path in overrides.items()}
    return {
        target.tool_id: target.root.expanduser()
        for target in DEFAULT_LOCAL_TOOL_TARGETS
        if target.tool_id != LEGACY_ROOT_NAME
    }


def _consumer_links(legacy_skill: Path, consumers: dict[str, Path]) -> list[dict]:
    links = []
    expected = legacy_skill.resolve()
    for tool_id, root in sorted(consumers.items()):
        target = root / legacy_skill.name
        if not target.is_symlink():
            continue
        try:
            resolved = target.resolve(strict=True)
        except OSError:
            continue
        if resolved != expected:
            continue
        links.append(
            {
                "tool_id": tool_id,
                "target_path": str(target),
                "link_target": os.readlink(target),
            }
        )
    return links


def _central_state(record, central_item: Optional[dict]) -> tuple[str, str]:
    if central_item is None:
        return "missing", "尚未保存到中央仓库。"
    archive = str(central_item.get("archive") or "")
    if not archive:
        return "changed", "中央仓库条目缺少可验证的归档文件。"
    # The remote package includes sidecar metadata. Migration compares the actual
    # skill payload and intentionally ignores manifest.json added by sidecar.
    try:
        central_files = _archive_payload_hashes(Path(central_item["_snapshot_dir"]), archive)
    except (KeyError, OSError, ValueError):
        return "changed", "中央仓库归档无法读取或路径不安全。"
    local_files = {file.path: file.sha256 for file in record.files if file.path != "manifest.json"}
    return (
        ("match", "中央仓库内容与旧目录一致。")
        if local_files == central_files
        else ("changed", "中央仓库同名 skill 与旧目录内容不同，需要先检查差异。")
    )


def _archive_payload_hashes(snapshot_dir: Path, archive_rel: str) -> dict[str, str]:
    root = snapshot_dir.expanduser().resolve()
    archive = (root / archive_rel).resolve()
    if archive != root and root not in archive.parents:
        raise ValueError("archive path escapes central snapshot")
    if not archive.exists():
        raise OSError(f"archive not found: {archive_rel}")
    with ZipFile(archive) as package:
        return {
            name: sha256(package.read(name)).hexdigest()
            for name in package.namelist()
            if not name.endswith("/") and not name.startswith(".skill-sync/") and name != "manifest.json"
        }


def _classify_links(links: Sequence[dict], central_item: Optional[dict], central_state: str) -> tuple[list[dict], list[dict]]:
    allowed: list[dict] = []
    blocked: list[dict] = []
    targets = {str(item) for item in (central_item or {}).get("targets") or []}
    for link in links:
        tool_id = str(link["tool_id"])
        compatible = bool(TOOL_ALIASES.get(tool_id, {tool_id}) & targets)
        if central_state == "match" and compatible:
            allowed.append(dict(link))
        else:
            reason = (
                "中央内容尚未验证一致。"
                if central_state != "match"
                else "中央仓库 manifest 未声明该工具为安装目标。"
            )
            blocked.append({**link, "reason": reason})
    return allowed, blocked


def _action_for(central_state: str, links: Sequence[dict], allowed: Sequence[dict], blocked: Sequence[dict]) -> tuple[str, str]:
    if central_state == "missing":
        return "publish_to_central", "先在 Skill 清单中选择该 skill 并保存到中央仓库。"
    if central_state == "changed":
        return "review_central_difference", "先检查两边差异，确认保留版本后再迁移。"
    if allowed:
        suffix = "" if not blocked else "；其余工具未在 manifest 中声明，暂不处理。"
        return "detach_legacy_links", f"可将 {len(allowed)} 个工具软链接替换为中央仓库副本{suffix}"
    if links:
        return "update_targets", "中央仓库内容一致，但 manifest 未声明这些工具；先补目标后再迁移。"
    return "no_linked_consumer", "没有工具软链接依赖此旧目录。"


def _operator_action(summary: dict) -> str:
    return (
        f"可立即迁移 {summary['detachable_skills']} 个已验证 skill 的 {summary['detachable_links']} 个软链接；"
        f"{summary['central_missing']} 个需先保存到中央仓库，{summary['central_changed']} 个需先检查差异。"
    )


def _staged_by_skill_id(stage_index: dict) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in stage_index.get("skills") or []:
        skill_id = str(item.get("skill_id") or "")
        if skill_id and skill_id not in result:
            result[skill_id] = str(item.get("output_path"))
    return result


def _replace_legacy_link(
    target: Path,
    staged_source: Path,
    migration_id: str,
    record_dir: Path,
    item: dict,
    link: dict,
) -> dict:
    if not target.is_symlink():
        raise LegacySkillshubMigrationError(f"legacy link changed before migration: {target}")
    original_link_target = os.readlink(target)
    backup = record_dir / "links" / str(link["tool_id"]) / str(item["skill_id"])
    if backup.exists() or backup.is_symlink():
        raise LegacySkillshubMigrationError(f"legacy link backup already exists: {backup}")
    temp = target.parent / f".{target.name}.skill-sync-legacy-tmp-{migration_id}"
    if temp.exists() or temp.is_symlink():
        raise LegacySkillshubMigrationError(f"migration temporary path already exists: {temp}")
    try:
        shutil.copytree(staged_source, temp)
        backup.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(original_link_target, backup, target_is_directory=True)
        target.unlink()
        temp.replace(target)
    except Exception:
        if temp.exists() or temp.is_symlink():
            _remove_path(temp)
        if not target.exists() and not target.is_symlink() and backup.is_symlink():
            os.symlink(original_link_target, target, target_is_directory=True)
        raise
    return {
        "skill_id": item["skill_id"],
        "tool_id": link["tool_id"],
        "target_path": str(target),
        "backup_link_path": str(backup),
        "original_link_target": original_link_target,
        "central_content_hash": item.get("central_content_hash"),
        "action": "replaced_legacy_symlink_with_central_copy",
    }


def _rollback_applied_links(items: Sequence[dict]) -> list[str]:
    errors = []
    for item in reversed(items):
        try:
            target = Path(str(item["target_path"]))
            backup = Path(str(item["backup_link_path"]))
            if target.exists() or target.is_symlink():
                _remove_path(target)
            if not backup.is_symlink():
                raise LegacySkillshubMigrationError(f"legacy link backup is missing: {backup}")
            os.symlink(os.readlink(backup), target, target_is_directory=True)
        except Exception as exc:  # pragma: no cover - surfaced in durable migration record
            errors.append(str(exc))
    return errors


def _default_record_path(migration_id: str) -> Path:
    return Path.home() / "Library" / "Application Support" / "skill-sync-sidecar" / "migrations" / f"legacy-skillshub-{migration_id}.json"


def _normalize_skill_ids(skill_ids: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for raw in skill_ids:
        skill_id = str(raw).strip()
        if skill_id and skill_id not in seen:
            seen.add(skill_id)
            result.append(skill_id)
    return result


def _remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _timestamp_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


def _write_record(path: Path, record: dict) -> None:
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
