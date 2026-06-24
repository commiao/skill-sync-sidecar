from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Dict, List, Optional, Sequence, Set

from .remote import Remote, RemoteError, build_upload_plan, join_remote_path, upload_snapshot
from .scanner import scan_roots
from .snapshot import write_snapshot
from .sync_apply import PUSH_ACTIONS
from .sync_plan import build_sync_plan
from .sync_state import build_sync_status


class ApprovedPushError(RuntimeError):
    pass


def build_approved_push_preview(
    local_root: Path,
    remote_snapshot_dir: Path,
    blocked_report_path: Path,
    skill_ids: Sequence[str],
    last_applied_record: Optional[Path] = None,
    allow_new: Optional[bool] = None,
    allow_conflict_local_wins: bool = False,
) -> Dict[str, object]:
    report = _load_blocked_report(blocked_report_path)
    approved_ids = _normalize_skill_ids(skill_ids)
    effective_allow_new = bool(report.get("allow_new")) if allow_new is None else allow_new

    status = build_sync_status(local_root, remote_snapshot_dir, last_applied_record)
    plan = build_sync_plan(status, allow_new=effective_allow_new, writer_policy="push-pull")
    plan_by_id = {str(item["skill_id"]): dict(item) for item in plan.get("items", [])}
    report_by_id = _report_items_by_skill_id(report)

    approved_items = [
        _approved_item(skill_id, report_by_id, plan_by_id, allow_conflict_local_wins=allow_conflict_local_wins)
        for skill_id in approved_ids
    ]
    deferred_pushes = [
        {
            "skill_id": item["skill_id"],
            "plan_action": item["plan_action"],
            "reason": "not selected for this approved push",
        }
        for item in plan.get("items", [])
        if item.get("plan_action") in PUSH_ACTIONS and str(item.get("skill_id")) not in approved_ids
    ]

    with TemporaryDirectory(prefix="skill-sync-approved-push-preview-") as tmp:
        merged = _build_merged_snapshot(
            local_root,
            remote_snapshot_dir,
            set(approved_ids),
            Path(tmp) / "merged-snapshot",
            label=_snapshot_id("approved-push-preview"),
        )
        upload_plan = build_upload_plan(merged["snapshot_dir"])
        upload_files = len(upload_plan.files)
        upload_bytes = upload_plan.total_bytes
        upload_archives = sorted(merged["approved_archive_paths"])
        upload_snapshot_id = merged["snapshot_index"].get("snapshot_id")
        upload_total = merged["snapshot_index"].get("total")

    return {
        "record_type": "skill-sync-approved-push-preview",
        "dry_run": True,
        "safe_to_push": True,
        "local_root": str(local_root.resolve()),
        "remote_snapshot": str(remote_snapshot_dir.resolve()),
        "blocked_report": str(blocked_report_path.resolve()),
        "last_applied_record": str(last_applied_record.resolve()) if last_applied_record else None,
        "allow_new": effective_allow_new,
        "allow_conflict_local_wins": allow_conflict_local_wins,
        "approved": len(approved_items),
        "approved_skill_ids": approved_ids,
        "deferred_pushes": deferred_pushes,
        "current_plan_summary": plan.get("summary", {}),
        "upload_preview": {
            "snapshot_id": upload_snapshot_id,
            "total": upload_total,
            "files": upload_files,
            "bytes": upload_bytes,
            "archives": upload_archives,
        },
        "items": approved_items,
    }


def execute_approved_push(
    local_root: Path,
    remote_snapshot_dir: Path,
    blocked_report_path: Path,
    skill_ids: Sequence[str],
    remote: Remote,
    remote_prefix: str = "",
    last_applied_record: Optional[Path] = None,
    allow_new: Optional[bool] = None,
    allow_conflict_local_wins: bool = False,
    base_record_out: Optional[Path] = None,
    out_dir: Optional[Path] = None,
) -> Dict[str, object]:
    preview = build_approved_push_preview(
        local_root,
        remote_snapshot_dir,
        blocked_report_path,
        skill_ids,
        last_applied_record=last_applied_record,
        allow_new=allow_new,
        allow_conflict_local_wins=allow_conflict_local_wins,
    )
    _assert_remote_matches_cache(remote, remote_prefix, remote_snapshot_dir)

    with TemporaryDirectory(prefix="skill-sync-approved-push-") as tmp:
        merged = _build_merged_snapshot(
            local_root,
            remote_snapshot_dir,
            set(preview["approved_skill_ids"]),
            Path(tmp) / "merged-snapshot",
            label=_snapshot_id("approved-push"),
        )
        upload_plan = upload_snapshot(
            merged["snapshot_dir"],
            remote,
            remote_prefix,
            include_paths=set(merged["approved_archive_paths"]),
            skip_existing_archives=True,
        )
        uploaded_files = len(upload_plan.files)
        uploaded_bytes = upload_plan.total_bytes
        base_record_path = _write_sync_base_record(
            local_root,
            merged["snapshot_index"],
            remote_prefix,
            out=base_record_out,
        )

    record = {
        "record_type": "skill-sync-approved-push-record",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "complete",
        "dry_run": False,
        "local_root": str(local_root.resolve()),
        "remote_snapshot": str(remote_snapshot_dir.resolve()),
        "blocked_report": str(blocked_report_path.resolve()),
        "last_applied_record": str(last_applied_record.resolve()) if last_applied_record else None,
        "remote_prefix": remote_prefix,
        "approved": preview["approved"],
        "allow_conflict_local_wins": preview["allow_conflict_local_wins"],
        "approved_skill_ids": preview["approved_skill_ids"],
        "deferred_pushes": preview["deferred_pushes"],
        "uploaded_files": uploaded_files,
        "uploaded_bytes": uploaded_bytes,
        "base_record_path": base_record_path,
        "items": preview["items"],
    }
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        record["out"] = str(out_dir.resolve())
        (out_dir / "approved-push-record.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (out_dir / "approved-push-record.md").write_text(_render_markdown(record), encoding="utf-8")
    return record


def write_approved_push_preview(preview: Dict[str, object], out_dir: Path) -> Dict[str, object]:
    out_dir.mkdir(parents=True, exist_ok=True)
    result = dict(preview)
    result["out"] = str(out_dir.resolve())
    (out_dir / "approved-push-preview.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "approved-push-preview.md").write_text(_render_markdown(result), encoding="utf-8")
    return result


def _load_blocked_report(path: Path) -> Dict[str, object]:
    if not path.exists():
        raise ApprovedPushError(f"blocked report not found: {path}")
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("record_type") != "skill-sync-blocked-report":
        raise ApprovedPushError("input is not a skill-sync blocked report")
    return report


def _normalize_skill_ids(skill_ids: Sequence[str]) -> List[str]:
    result: List[str] = []
    seen: Set[str] = set()
    for raw in skill_ids:
        skill_id = raw.strip()
        if not skill_id:
            continue
        if skill_id in seen:
            raise ApprovedPushError(f"duplicate approved skill id: {skill_id}")
        seen.add(skill_id)
        result.append(skill_id)
    if not result:
        raise ApprovedPushError("approved push requires at least one --skill-id")
    return result


def _report_items_by_skill_id(report: Dict[str, object]) -> Dict[str, dict]:
    result = {}
    for item in report.get("items", []):
        skill_id = item.get("skill_id")
        if skill_id:
            result[str(skill_id)] = dict(item)
    return result


def _approved_item(skill_id: str, report_by_id: Dict[str, dict], plan_by_id: Dict[str, dict], allow_conflict_local_wins: bool = False) -> Dict[str, object]:
    report_item = report_by_id.get(skill_id)
    if report_item is None:
        raise ApprovedPushError(f"skill was not present in blocked report: {skill_id}")

    current = plan_by_id.get(skill_id)
    if current is None:
        raise ApprovedPushError(f"skill is not present in the current sync plan: {skill_id}")

    for hash_key in ("base_hash", "local_hash", "remote_hash"):
        if current.get(hash_key) != report_item.get(hash_key):
            raise ApprovedPushError(f"skill changed since blocked report was generated: {skill_id} ({hash_key})")

    if _is_approved_conflict_local_wins(report_item, current, allow_conflict_local_wins):
        return {
            "skill_id": skill_id,
            "approved_action": "conflict_local_wins",
            "status_action": current.get("status_action"),
            "base_hash": current.get("base_hash"),
            "local_hash": current.get("local_hash"),
            "remote_hash": current.get("remote_hash"),
            "blocked_reason": report_item.get("reason"),
            "approval_reason": "explicit local-wins conflict resolution from blocked-report queue",
        }

    if report_item.get("category") != "writer_policy":
        raise ApprovedPushError(f"skill is not blocked by writer policy: {skill_id}")
    if report_item.get("status_action") not in {"push", "local_new"}:
        raise ApprovedPushError(f"skill is not a local-to-remote push candidate: {skill_id}")
    if current.get("plan_action") not in PUSH_ACTIONS or not current.get("allowed"):
        raise ApprovedPushError(f"skill is not currently pushable under explicit approval: {skill_id}")

    return {
        "skill_id": skill_id,
        "approved_action": current.get("plan_action"),
        "status_action": current.get("status_action"),
        "base_hash": current.get("base_hash"),
        "local_hash": current.get("local_hash"),
        "remote_hash": current.get("remote_hash"),
        "blocked_reason": report_item.get("reason"),
        "approval_reason": "explicit approved push from blocked-report writer-policy queue",
    }


def _is_approved_conflict_local_wins(report_item: Dict[str, object], current: Dict[str, object], allow_conflict_local_wins: bool) -> bool:
    if not allow_conflict_local_wins:
        return False
    return (
        report_item.get("category") == "conflict"
        and report_item.get("status_action") == "conflict"
        and current.get("status_action") == "conflict"
        and current.get("plan_action") == "blocked"
        and not current.get("allowed")
        and bool(current.get("local_hash"))
        and bool(current.get("remote_hash"))
    )


def _build_merged_snapshot(local_root: Path, remote_snapshot_dir: Path, approved_ids: Set[str], out_dir: Path, label: str) -> Dict[str, object]:
    with TemporaryDirectory(prefix="skill-sync-approved-local-snapshot-") as tmp:
        local_snapshot_dir = Path(tmp) / "local-snapshot"
        local_index = write_snapshot(scan_roots([f"cc-switch={local_root}"]), local_snapshot_dir, label)
        local_by_id = {str(skill["skill_id"]): dict(skill) for skill in local_index.get("skills", [])}
        remote_index = json.loads((remote_snapshot_dir / "index.json").read_text(encoding="utf-8"))
        remote_skills = [dict(skill) for skill in remote_index.get("skills", []) if isinstance(skill, dict)]
        remote_ids = {str(skill.get("skill_id")) for skill in remote_skills if skill.get("skill_id")}

        approved_archive_paths: Set[str] = set()
        merged_skills: List[dict] = []
        for remote_skill in remote_skills:
            skill_id = str(remote_skill.get("skill_id"))
            if skill_id in approved_ids:
                replacement = local_by_id.get(skill_id)
                if replacement is None:
                    raise ApprovedPushError(f"approved local skill disappeared before push: {skill_id}")
                merged_skills.append(replacement)
                approved_archive_paths.add(str(replacement["archive"]))
            else:
                merged_skills.append(remote_skill)

        for skill_id in sorted(approved_ids - remote_ids):
            replacement = local_by_id.get(skill_id)
            if replacement is None:
                raise ApprovedPushError(f"approved local skill disappeared before push: {skill_id}")
            merged_skills.append(replacement)
            approved_archive_paths.add(str(replacement["archive"]))

        out_dir.mkdir(parents=True, exist_ok=True)
        for archive_path in approved_archive_paths:
            source = local_snapshot_dir / archive_path
            target = out_dir / archive_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    index = {
        "protocol_version": remote_index.get("protocol_version", 0),
        "snapshot_id": label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "total": len(merged_skills),
        "skills": merged_skills,
    }
    (out_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "snapshot_dir": out_dir,
        "snapshot_index": index,
        "approved_archive_paths": approved_archive_paths,
    }


def _assert_remote_matches_cache(remote: Remote, remote_prefix: str, remote_snapshot_dir: Path) -> None:
    try:
        cache_index = json.loads((remote_snapshot_dir / "index.json").read_text(encoding="utf-8"))
        remote_index = json.loads(remote.get_bytes(join_remote_path(remote_prefix, "index.json")).decode("utf-8"))
    except (OSError, json.JSONDecodeError, RemoteError) as exc:
        raise ApprovedPushError(f"cannot verify remote snapshot before approved push: {exc}") from exc
    if _hashes_by_skill_id(remote_index) != _hashes_by_skill_id(cache_index):
        raise ApprovedPushError("remote changed since the blocked-report cache was pulled; refresh pull-cache before approved push")


def _write_sync_base_record(local_root: Path, snapshot_index: Dict[str, object], remote_prefix: str, out: Optional[Path] = None) -> str:
    sync_id = _snapshot_id("approved-push-base")
    record_path = out
    if record_path is None:
        record_dir = local_root / ".skill-sync-bases"
        record_dir.mkdir(parents=True, exist_ok=True)
        record_path = record_dir / f"{sync_id}.json"
    else:
        record_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "protocol_version": 0,
        "record_type": "skill-sync-base",
        "sync_id": sync_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target_root": str(local_root.resolve()),
        "remote_prefix": remote_prefix,
        "snapshot_id": snapshot_index.get("snapshot_id"),
        "applied": [
            {
                "skill_id": skill.get("skill_id"),
                "content_hash": skill.get("content_hash"),
            }
            for skill in snapshot_index.get("skills", [])
            if skill.get("skill_id") and skill.get("content_hash")
        ],
    }
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(record_path.resolve())


def _hashes_by_skill_id(index: Dict[str, object]) -> Dict[str, str]:
    return {
        str(skill["skill_id"]): str(skill["content_hash"])
        for skill in index.get("skills", [])
        if isinstance(skill, dict) and skill.get("skill_id") and skill.get("content_hash")
    }


def _snapshot_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}"


def _render_markdown(record: Dict[str, object]) -> str:
    title = "Approved Push Preview" if record.get("dry_run") else "Approved Push Record"
    lines = [
        f"# Skill Sync {title}",
        "",
        f"- Created: `{record.get('created_at') or datetime.now(timezone.utc).isoformat()}`",
        f"- Dry run: `{record.get('dry_run')}`",
        f"- Local root: `{record.get('local_root')}`",
        f"- Remote snapshot: `{record.get('remote_snapshot')}`",
        f"- Blocked report: `{record.get('blocked_report')}`",
        f"- Approved: `{record.get('approved')}`",
        "",
        "## Approved Skills",
        "",
    ]
    items = list(record.get("items", []))
    if not items:
        lines.append("- None.")
    for item in items:
        lines.extend(
            [
                f"### {item['skill_id']}",
                "",
                f"- Approved action: `{item['approved_action']}`",
                f"- Base hash: `{item.get('base_hash')}`",
                f"- Local hash: `{item.get('local_hash')}`",
                f"- Remote hash: `{item.get('remote_hash')}`",
                f"- Blocked reason: {item.get('blocked_reason')}",
                "",
            ]
        )
    deferred = list(record.get("deferred_pushes", []))
    if deferred:
        lines.extend(["## Deferred Pushes", ""])
        for item in deferred:
            lines.append(f"- `{item['skill_id']}`: `{item['plan_action']}`")
    if record.get("base_record_path"):
        lines.extend(["", f"Base record: `{record['base_record_path']}`"])
    return "\n".join(lines).rstrip() + "\n"
