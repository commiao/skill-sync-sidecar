from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional, Sequence

from .approved_push import _assert_remote_matches_cache, _build_merged_snapshot, _snapshot_id, _write_sync_base_record
from .model import SkillFile
from .remote import Remote, build_upload_plan, upload_snapshot
from .scanner import hash_skill_files, scan_skill
from .sync_apply import PUSH_ACTIONS
from .sync_plan import build_sync_plan
from .sync_state import build_sync_status


class LocalSkillError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalToolTarget:
    tool_id: str
    name: str
    root: Path
    target_alias: str


DEFAULT_LOCAL_TOOL_TARGETS: tuple[LocalToolTarget, ...] = (
    LocalToolTarget("cc-switch", "cc-switch", Path.home() / ".cc-switch" / "skills", "cc-switch"),
    LocalToolTarget("skillshub", "skillshub", Path.home() / ".skillshub", "skillshub"),
    LocalToolTarget("codex", "Codex", Path.home() / ".codex" / "skills", "codex"),
    LocalToolTarget("cursor", "Cursor", Path.home() / ".cursor" / "skills-cursor", "cursor"),
    LocalToolTarget("claude-code", "Claude Code", Path.home() / ".claude" / "skills", "claude-code"),
)

DEFAULT_GLOBAL_TARGETS = ["codex", "cc-switch", "skillshub", "cursor", "claude-code", "openclaw"]
DEFAULT_PROJECT_TARGETS = ["codex", "cursor", "qoder"]
DEFAULT_EXCLUDES = ["__pycache__", "*.pyc", ".DS_Store"]
SECRET_HINTS = {".env", ".env.local", ".npmrc", ".pypirc"}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}


def analyze_local_skill(
    source_path: Path,
    *,
    tool_ids: Optional[Sequence[str]] = None,
    tool_targets: Sequence[LocalToolTarget] = DEFAULT_LOCAL_TOOL_TARGETS,
) -> dict:
    source = _skill_dir(source_path)
    record = scan_skill("local-import", source, source / "SKILL.md")
    manifest = _load_manifest(source / "manifest.json")
    scope = str(manifest.get("scope") or _infer_scope(source))
    targets = [str(item) for item in (manifest.get("targets") or _default_targets(scope))]
    skill_id = str(manifest.get("skill_id") or record.skill_id)
    manifest_data = _manifest_for(record, manifest, skill_id=skill_id, scope=scope, targets=targets)
    desired_hash = _desired_content_hash(record, manifest_data)
    selected = _selected_tool_targets(tool_targets, tool_ids)
    target_items = [_target_status(source, skill_id, manifest_data, record.content_hash, desired_hash, target) for target in selected]
    risk = _risk_summary(record, source)
    if risk.get("level") == "error":
        for item in target_items:
            if item.get("action") != "noop":
                item["allowed"] = False
                item["reason"] = "risk check failed; automatic install is blocked"
    source_record = record.to_dict()
    source_record.update({"scope": scope, "targets": targets, "manifest_path": str(source / "manifest.json") if (source / "manifest.json").exists() else None})
    return {
        "ok": True,
        "record_type": "skill-sync-local-skill-analysis",
        "mode": "analysis",
        "source_path": str(source),
        "skill_id": skill_id,
        "name": manifest_data.get("name"),
        "description": manifest_data.get("description"),
        "scope": scope,
        "targets": targets,
        "manifest": manifest_data,
        "desired_content_hash": desired_hash,
        "manifest_source": "file" if (source / "manifest.json").exists() else "generated",
        "risk": risk,
        "source": source_record,
        "tools": target_items,
        "summary": _target_summary(target_items),
        "operator_action": _operator_action(risk, target_items),
    }


def install_local_skill(
    source_path: Path,
    *,
    tool_ids: Optional[Sequence[str]] = None,
    tool_targets: Sequence[LocalToolTarget] = DEFAULT_LOCAL_TOOL_TARGETS,
    yes: bool = False,
    allow_local_writes: bool = False,
) -> dict:
    analysis = analyze_local_skill(source_path, tool_ids=tool_ids, tool_targets=tool_targets)
    if yes and not allow_local_writes:
        raise LocalSkillError("local writes are disabled; start operator-executor with --allow-local-writes")

    install_id = _timestamp_id()
    items = []
    for item in analysis["tools"]:
        planned = dict(item)
        planned["install_id"] = install_id
        if yes and item.get("allowed") and item.get("action") != "noop":
            planned.update(_install_target(analysis, item, install_id))
        else:
            planned["executed"] = False
        items.append(planned)

    result = {
        "ok": True,
        "record_type": "skill-sync-local-skill-install",
        "mode": "install" if yes else "dry_run",
        "dry_run": not yes,
        "install_id": install_id,
        "source_path": analysis["source_path"],
        "skill_id": analysis["skill_id"],
        "manifest_source": analysis["manifest_source"],
        "risk": analysis["risk"],
        "items": items,
        "summary": _target_summary(items),
    }
    if yes and any(item.get("executed") for item in items):
        record_path = _write_install_record(result)
        result["record_path"] = str(record_path)
    return result


def publish_local_skill(
    local_root: Path,
    remote_snapshot_dir: Path,
    skill_id: str,
    remote: Remote,
    *,
    remote_prefix: str = "",
    last_applied_record: Optional[Path] = None,
    base_record_out: Optional[Path] = None,
    yes: bool = False,
) -> dict:
    normalized = str(skill_id).strip()
    if not normalized:
        raise LocalSkillError("skill_id is required")
    local_skill_dir = (local_root.expanduser() / normalized).resolve()
    if not local_skill_dir.exists():
        raise LocalSkillError(f"local skill is not installed in canonical root: {local_skill_dir}")
    local_record = scan_skill("local-publish", local_skill_dir, local_skill_dir / "SKILL.md")
    local_risk = _risk_summary(local_record, local_skill_dir)
    if local_risk.get("level") == "error":
        raise LocalSkillError(f"risk check failed; shared-library save is blocked for: {normalized}")
    status = build_sync_status(local_root, remote_snapshot_dir, last_applied_record)
    plan = build_sync_plan(status, allow_new=True, writer_policy="push-pull")
    item = next((dict(value) for value in plan.get("items", []) if value.get("skill_id") == normalized), None)
    if item is None:
        raise LocalSkillError(f"skill is not present in the sync plan: {normalized}")
    if item.get("plan_action") == "noop":
        return {
            "ok": True,
            "record_type": "skill-sync-local-skill-publish",
            "mode": "noop",
            "dry_run": not yes,
            "skill_id": normalized,
            "safe_to_push": True,
            "item": item,
            "uploaded_files": 0,
            "reason": "central snapshot already matches local skill",
        }
    if item.get("plan_action") not in PUSH_ACTIONS or not item.get("allowed"):
        raise LocalSkillError(f"skill is not publishable without manual review: {normalized}")

    if yes:
        _assert_remote_matches_cache(remote, remote_prefix, remote_snapshot_dir)

    with TemporaryDirectory(prefix="skill-sync-local-skill-publish-") as tmp:
        merged = _build_merged_snapshot(
            local_root,
            remote_snapshot_dir,
            {normalized},
            Path(tmp) / "merged-snapshot",
            label=_snapshot_id("local-skill-publish"),
        )
        include_paths = set(merged["approved_archive_paths"])
        if yes:
            upload_plan = upload_snapshot(
                merged["snapshot_dir"],
                remote,
                remote_prefix,
                include_paths=include_paths,
                skip_existing_archives=True,
            )
            base_record_path = _write_sync_base_record(
                local_root,
                merged["snapshot_index"],
                remote_prefix,
                out=base_record_out,
            )
            uploaded_files = len(upload_plan.files)
            uploaded_bytes = upload_plan.total_bytes
        else:
            full_plan = build_upload_plan(merged["snapshot_dir"])
            upload_files = [
                (path, rel)
                for path, rel in full_plan.files
                if rel == "index.json" or rel in include_paths
            ]
            base_record_path = None
            uploaded_files = len(upload_files)
            uploaded_bytes = sum(path.stat().st_size for path, _ in upload_files)

        return {
            "ok": True,
            "record_type": "skill-sync-local-skill-publish",
            "mode": "publish" if yes else "dry_run",
            "dry_run": not yes,
            "skill_id": normalized,
            "safe_to_push": True,
            "item": item,
            "snapshot_id": merged["snapshot_index"].get("snapshot_id"),
            "snapshot_total": merged["snapshot_index"].get("total"),
            "uploaded_files": uploaded_files,
            "uploaded_bytes": uploaded_bytes,
            "remote_prefix": remote_prefix,
            "base_record_path": base_record_path,
        }


def _skill_dir(source_path: Path) -> Path:
    source = source_path.expanduser().resolve()
    if source.is_file() and source.name == "SKILL.md":
        source = source.parent
    if not source.exists():
        raise LocalSkillError(f"skill path not found: {source}")
    if not source.is_dir():
        raise LocalSkillError(f"skill path must be a directory or SKILL.md: {source}")
    if not (source / "SKILL.md").exists():
        raise LocalSkillError(f"SKILL.md not found under: {source}")
    return source


def _load_manifest(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalSkillError(f"invalid manifest.json: {exc}") from exc
    if not isinstance(value, dict):
        raise LocalSkillError("manifest.json must contain a JSON object")
    return value


def _infer_scope(source: Path) -> str:
    home = Path.home().resolve()
    global_roots = [
        home / ".cc-switch" / "skills",
        home / ".skillshub",
        home / ".codex" / "skills",
        home / ".agents" / "skills",
        home / ".claude" / "skills",
        home / ".cursor" / "skills-cursor",
    ]
    for root in global_roots:
        try:
            source.relative_to(root)
            return "global"
        except ValueError:
            continue
    if source.parent.name == "skills" and ((source.parent.parent / "AGENTS.md").exists() or (source.parent.parent / ".git").exists()):
        return "project"
    return "global"


def _default_targets(scope: str) -> list[str]:
    return list(DEFAULT_PROJECT_TARGETS if scope == "project" else DEFAULT_GLOBAL_TARGETS)


def _manifest_for(record, manifest: dict, *, skill_id: str, scope: str, targets: Sequence[str]) -> dict:
    data = {
        "protocol_version": 0,
        "skill_id": skill_id,
        "name": manifest.get("name") or record.name or skill_id,
        "description": manifest.get("description") or record.description or "",
        "scope": scope,
        "targets": list(targets),
        "exclude": list(manifest.get("exclude") or DEFAULT_EXCLUDES),
        "security": {
            "contains_secrets": bool((manifest.get("security") or {}).get("contains_secrets", False)),
            "encryption": str((manifest.get("security") or {}).get("encryption", "none")),
        },
    }
    if isinstance(manifest.get("project"), dict):
        data["project"] = manifest["project"]
    return data


def _selected_tool_targets(targets: Sequence[LocalToolTarget], tool_ids: Optional[Sequence[str]]) -> list[LocalToolTarget]:
    selected = {str(item).strip() for item in (tool_ids or []) if str(item).strip()}
    unknown = selected - {target.tool_id for target in targets}
    if unknown:
        raise LocalSkillError(f"unknown local tool target: {', '.join(sorted(unknown))}")
    return [target for target in targets if not selected or target.tool_id in selected]


def _target_status(source: Path, skill_id: str, manifest: dict, raw_source_hash: str, desired_hash: str, target: LocalToolTarget) -> dict:
    root = target.root.expanduser()
    target_path = root / skill_id
    installed = root.exists()
    same_path = _same_path(source, target_path)
    existing_hash = None
    target_manifest_exists = (target_path / "manifest.json").exists()
    if target_path.exists() and not same_path:
        existing_hash = scan_skill(target.tool_id, target_path.resolve(), target_path.resolve() / "SKILL.md").content_hash
    action = "install_new"
    allowed = installed
    reason = None
    if not installed:
        action = "skip"
        allowed = False
        reason = "tool root not found on this device"
    elif same_path and not target_manifest_exists:
        action = "write_manifest"
        reason = "sidecar metadata will be written into the existing source skill"
    elif same_path:
        action = "noop"
        reason = "source is already this tool's installed skill"
    elif target_path.exists() and existing_hash == desired_hash:
        action = "noop"
        reason = "same content already installed"
    elif target_path.exists() and not target_manifest_exists and existing_hash == raw_source_hash:
        action = "write_manifest"
        reason = "same skill exists but sidecar metadata is missing"
    elif target_path.exists():
        action = "replace_with_backup"
    return {
        "tool_id": target.tool_id,
        "tool_name": target.name,
        "root": str(root),
        "target_path": str(target_path),
        "installed": installed,
        "allowed": allowed,
        "action": action,
        "reason": reason,
        "source_hash": desired_hash,
        "existing_hash": existing_hash,
        "manifest_will_be_packaged": True,
        "targeted": target.target_alias in set(str(item) for item in manifest.get("targets", [])),
    }


def _risk_summary(record, source: Path) -> dict:
    secret_files = []
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        name = path.name
        if name in SECRET_HINTS or any(name.endswith(suffix) for suffix in SECRET_SUFFIXES):
            secret_files.append(str(path.relative_to(source)))
    issues = [issue.__dict__ for issue in record.issues]
    if secret_files:
        issues.append(
            {
                "severity": "error",
                "code": "secret_like_file",
                "message": "Secret-like files are not installed or published automatically.",
                "path": ", ".join(secret_files),
            }
        )
    level = "error" if any(issue.get("severity") == "error" for issue in issues) else ("warning" if issues else "ok")
    return {"level": level, "issues": issues, "secret_like_files": secret_files}


def _operator_action(risk: dict, targets: Sequence[dict]) -> str:
    if risk.get("level") == "error":
        return "存在高风险文件或错误，不能一键安装。"
    allowed = sum(1 for item in targets if item.get("allowed") and item.get("action") != "noop")
    noop = sum(1 for item in targets if item.get("action") == "noop")
    if allowed:
        return f"可一键安装到 {allowed} 个本机工具；{noop} 个工具已是最新。"
    if noop:
        return "本机可用工具已安装该 skill。"
    return "没有可安装的本机工具根。"


def _target_summary(items: Sequence[dict]) -> dict:
    summary: dict[str, int] = {}
    for item in items:
        action = str(item.get("action") or "unknown")
        summary[action] = summary.get(action, 0) + 1
    summary["allowed"] = sum(1 for item in items if item.get("allowed"))
    summary["will_write"] = sum(1 for item in items if item.get("allowed") and item.get("action") not in {"noop", "skip"})
    return summary


def _install_target(analysis: dict, item: dict, install_id: str) -> dict:
    source = Path(str(analysis["source_path"]))
    target = Path(str(item["target_path"]))
    backup = target.parent / ".skill-sync-backups" / install_id / target.name
    if item.get("action") == "write_manifest":
        target.mkdir(parents=True, exist_ok=True)
        manifest_path = target / "manifest.json"
        manifest_path.write_text(json.dumps(analysis["manifest"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {
            "executed": True,
            "backup_path": None,
            "had_existing": True,
            "manifest_path": str(manifest_path),
        }

    temp = target.parent / f".{target.name}.skill-sync-tmp-{install_id}"
    if temp.exists():
        raise LocalSkillError(f"temporary target already exists: {temp}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and backup.exists():
        raise LocalSkillError(f"backup already exists: {backup}")
    try:
        shutil.copytree(source, temp, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        (temp / "manifest.json").write_text(json.dumps(analysis["manifest"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        had_existing = target.exists()
        if had_existing:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(target, backup, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
            _remove_path(target)
        temp.replace(target)
    except Exception as exc:
        if temp.exists():
            _remove_path(temp)
        raise LocalSkillError(str(exc)) from exc
    return {
        "executed": True,
        "backup_path": str(backup) if backup.exists() else None,
        "had_existing": item.get("action") == "replace_with_backup",
    }


def _write_install_record(result: dict) -> Path:
    first_written = next((item for item in result["items"] if item.get("executed")), None)
    root = Path(str(first_written["target_path"])).parent if first_written else Path.home() / ".skill-sync-sidecar"
    record_dir = root / ".skill-sync-backups" / str(result["install_id"])
    record_dir.mkdir(parents=True, exist_ok=True)
    record_path = record_dir / ".local-skill-install-record.json"
    record_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return record_path


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def _remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _timestamp_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


def _desired_content_hash(record, manifest: dict) -> str:
    files = [file for file in record.files if file.path != "manifest.json"]
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    files.append(SkillFile("manifest.json", len(manifest_bytes), sha256(manifest_bytes).hexdigest()))
    files.sort(key=lambda item: item.path)
    return hash_skill_files(files)
