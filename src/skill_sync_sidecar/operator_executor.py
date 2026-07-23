from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from json import JSONDecoder
from pathlib import Path
from typing import Optional, Sequence

from .central_lifecycle import (
    CentralLifecycleError,
    build_central_deprecate_preview,
    build_central_reactivate_preview,
    execute_central_deprecate,
    execute_central_reactivate,
)
from .config import ConfigError, load_cc_switch_webdav_settings
from .local_skill import LocalSkillError, analyze_local_skill, install_local_skill, publish_local_skill
from .remote import open_remote
from .restore import RestoreError, restore_from_central
from .tool_status import build_device_tool_status


SKILL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAC_TOOL_INSTALL_TARGETS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "cc-switch": ("cc-switch-global", (".cc-switch", "skills"), "cc-switch"),
    "skillshub": ("skillshub-global", (".skillshub",), "skillshub"),
    "codex": ("codex-global", (".codex", "skills"), "Codex"),
    "cursor": ("cursor-global", (".cursor", "skills-cursor"), "Cursor"),
    "claude-code": ("claude-code-global", (".claude", "skills"), "Claude Code"),
}


class OperatorExecutorError(RuntimeError):
    pass


def run_openclaw_approved_push_batch(
    repo_root: Path,
    skill_ids: Sequence[str],
    *,
    yes: bool = False,
    timeout_seconds: int = 900,
    allow_publish: bool = False,
    allow_conflict_local_wins: bool = False,
    refresh_peer_status: bool = False,
) -> dict:
    repo = repo_root.expanduser().resolve()
    script = repo / "scripts" / "openclaw-approved-push-batch.sh"
    if not script.exists():
        raise OperatorExecutorError(f"approved-push helper not found: {script}")
    normalized = _normalize_skill_ids(skill_ids)
    if yes and not allow_publish:
        raise OperatorExecutorError("publish is disabled; set SKILL_SYNC_EXECUTOR_ALLOW_PUBLISH=1 to enable --yes")
    command = [str(script), "--yes" if yes else "--dry-run"]
    if allow_conflict_local_wins:
        command.append("--allow-conflict-local-wins")
    command.extend(normalized)
    started_at = datetime.now(timezone.utc).isoformat()
    proc = subprocess.run(
        command,
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    finished_at = datetime.now(timezone.utc).isoformat()
    parsed = _last_json_object(proc.stdout)
    result = {
        "ok": proc.returncode == 0,
        "mode": "publish" if yes else "dry_run",
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": proc.returncode,
        "command": " ".join(command),
        "skill_ids": normalized,
        "safe_to_push": bool(parsed.get("safe_to_push")) if isinstance(parsed, dict) else False,
        "approved": parsed.get("approved") if isinstance(parsed, dict) else None,
        "approved_skill_ids": parsed.get("approved_skill_ids") if isinstance(parsed, dict) else normalized,
        "allow_conflict_local_wins": allow_conflict_local_wins,
        "result": parsed,
        "stdout_tail": _tail(proc.stdout),
        "stderr_tail": _tail(proc.stderr),
    }
    approved = result.get("approved")
    if yes and refresh_peer_status and result["ok"] and isinstance(approved, int) and approved > 0:
        try:
            result["peer_status_refresh"] = run_openclaw_peer_status_refresh(repo)
        except Exception as exc:  # pragma: no cover - keep publish result authoritative
            result["peer_status_refresh"] = {
                "ok": False,
                "mode": "refresh_openclaw_peer_status",
                "error": str(exc),
            }
    return result


def run_openclaw_peer_status_refresh(repo_root: Path, *, timeout_seconds: int = 300) -> dict:
    repo = repo_root.expanduser().resolve()
    script = repo / "scripts" / "publish-openclaw-peer-status.sh"
    if not script.exists():
        raise OperatorExecutorError(f"OpenClaw peer status helper not found: {script}")
    started_at = datetime.now(timezone.utc).isoformat()
    proc = subprocess.run(
        [str(script)],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    finished_at = datetime.now(timezone.utc).isoformat()
    return {
        "ok": proc.returncode == 0,
        "mode": "refresh_openclaw_peer_status",
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": proc.returncode,
        "command": str(script),
        "stdout_tail": _tail(proc.stdout),
        "stderr_tail": _tail(proc.stderr),
    }


def run_mac_peer_status_refresh(repo_root: Path, *, timeout_seconds: int = 300) -> dict:
    repo = repo_root.expanduser().resolve()
    script = repo / "scripts" / "publish-mac-peer-status.sh"
    if not script.exists():
        raise OperatorExecutorError(f"Mac peer status helper not found: {script}")
    started_at = datetime.now(timezone.utc).isoformat()
    proc = subprocess.run(
        [str(script)],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    finished_at = datetime.now(timezone.utc).isoformat()
    return {
        "ok": proc.returncode == 0,
        "mode": "refresh_mac_peer_status",
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": proc.returncode,
        "command": str(script),
        "stdout_tail": _tail(proc.stdout),
        "stderr_tail": _tail(proc.stderr),
    }


def run_mac_central_restore(
    skill_ids: Sequence[str],
    *,
    yes: bool = False,
    allow_local_writes: bool = False,
) -> dict:
    if yes and not allow_local_writes:
        raise OperatorExecutorError("local restore is disabled; start operator-executor with --allow-local-writes")
    result = restore_from_central(
        Path.home() / ".cc-switch" / "skills",
        Path.home() / "public-sync" / "skill-sync-sidecar-dev" / "current-mac",
        skill_ids,
        target="mixed-scope-root",
        remote_prefix=os.environ.get("SKILL_SYNC_PREFIX", "skill-sync-sidecar-dev/current-mac"),
        base_record_out=Path.home() / "Library" / "Application Support" / "skill-sync-sidecar" / "base-record.json",
        yes=yes,
    )
    return result


def run_mac_codex_install_from_central(
    skill_ids: Sequence[str],
    *,
    yes: bool = False,
    allow_local_writes: bool = False,
) -> dict:
    return run_mac_tool_install_from_central("codex", skill_ids, yes=yes, allow_local_writes=allow_local_writes)


def run_mac_tool_install_from_central(
    tool_id: str,
    skill_ids: Sequence[str],
    *,
    yes: bool = False,
    allow_local_writes: bool = False,
) -> dict:
    normalized_tool_id = _normalize_tool_id(tool_id)
    target, root_parts, label = MAC_TOOL_INSTALL_TARGETS[normalized_tool_id]
    if yes and not allow_local_writes:
        raise OperatorExecutorError(f"{label} install is disabled; start operator-executor with --allow-local-writes")
    target_root = Path.home().joinpath(*root_parts)
    result = restore_from_central(
        target_root,
        Path.home() / "public-sync" / "skill-sync-sidecar-dev" / "current-mac",
        skill_ids,
        target=target,
        remote_prefix=os.environ.get("SKILL_SYNC_PREFIX", "skill-sync-sidecar-dev/current-mac"),
        yes=yes,
    )
    result["operation"] = "mac-tool-install-from-central"
    result["tool_id"] = normalized_tool_id
    result["tool_name"] = label
    return result


def run_mac_tool_uninstall(
    tool_id: str,
    skill_ids: Sequence[str],
    *,
    yes: bool = False,
    allow_local_writes: bool = False,
) -> dict:
    normalized_tool_id = _normalize_tool_id(tool_id)
    _, root_parts, label = MAC_TOOL_INSTALL_TARGETS[normalized_tool_id]
    normalized_skill_ids = _normalize_skill_ids(skill_ids)
    if yes and not allow_local_writes:
        raise OperatorExecutorError(f"{label} uninstall is disabled; start operator-executor with --allow-local-writes")
    target_root = Path.home().joinpath(*root_parts)
    uninstall_id = _timestamp_id()
    backup_root = target_root / ".skill-sync-removed" / uninstall_id
    items = []
    blocked = []
    planned = 0
    for skill_id in normalized_skill_ids:
        target = target_root / skill_id
        item = {
            "skill_id": skill_id,
            "tool_id": normalized_tool_id,
            "tool_name": label,
            "target_path": str(target.expanduser()),
            "backup_path": str((backup_root / skill_id).expanduser()),
            "action": "move_to_removed_backup",
            "exists": target.exists(),
            "allowed": False,
            "reason": None,
        }
        if not target.exists():
            item["action"] = "skip_missing"
            item["reason"] = "skill is not installed in this tool root"
        elif not target.is_dir() or not (target / "SKILL.md").is_file():
            item["action"] = "blocked"
            item["reason"] = "target is not a skill directory with SKILL.md"
            blocked.append(item)
        else:
            item["allowed"] = True
            planned += 1
        items.append(item)
    safe_to_uninstall = len(blocked) == 0
    result = {
        "ok": safe_to_uninstall,
        "record_type": "skill-sync-mac-tool-uninstall",
        "mode": "uninstall" if yes else "dry_run",
        "dry_run": not yes,
        "safe_to_uninstall": safe_to_uninstall,
        "skill_ids": normalized_skill_ids,
        "tool_id": normalized_tool_id,
        "tool_name": label,
        "target_root": str(target_root.expanduser().resolve()),
        "backup_root": str(backup_root.expanduser()),
        "planned": planned,
        "blocked": len(blocked),
        "items": items,
    }
    if not safe_to_uninstall or not yes:
        return result
    backup_root.mkdir(parents=True, exist_ok=True)
    removed = 0
    for item in items:
        if not item["allowed"]:
            continue
        source = Path(str(item["target_path"]))
        backup = Path(str(item["backup_path"]))
        backup.parent.mkdir(parents=True, exist_ok=True)
        if backup.exists():
            raise OperatorExecutorError(f"backup path already exists: {backup}")
        shutil.move(str(source), str(backup))
        removed += 1
    record_path = backup_root / "uninstall-record.json"
    record = {**result, "removed": removed, "record_path": str(record_path)}
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    record["dry_run"] = False
    return record


def run_central_deprecate(
    skill_ids: Sequence[str],
    *,
    reason: str = "",
    yes: bool = False,
    allow_publish: bool = False,
) -> dict:
    snapshot_dir = Path.home() / "public-sync" / "skill-sync-sidecar-dev" / "current-mac"
    if yes and not allow_publish:
        raise OperatorExecutorError("central deprecate is disabled; start operator-executor with --allow-publish")
    if not yes:
        return build_central_deprecate_preview(snapshot_dir, skill_ids, actor="mac", reason=reason)
    remote, prefix = _local_publish_remote()
    return execute_central_deprecate(
        snapshot_dir,
        skill_ids,
        remote,
        remote_prefix=prefix,
        actor="mac",
        reason=reason,
    )


def run_central_reactivate(
    skill_ids: Sequence[str],
    *,
    reason: str = "",
    yes: bool = False,
    allow_publish: bool = False,
) -> dict:
    snapshot_dir = Path.home() / "public-sync" / "skill-sync-sidecar-dev" / "current-mac"
    if yes and not allow_publish:
        raise OperatorExecutorError("central reactivate is disabled; start operator-executor with --allow-publish")
    if not yes:
        return build_central_reactivate_preview(snapshot_dir, skill_ids, actor="mac", reason=reason)
    remote, prefix = _local_publish_remote()
    return execute_central_reactivate(
        snapshot_dir,
        skill_ids,
        remote,
        remote_prefix=prefix,
        actor="mac",
        reason=reason,
    )


def local_publish_root_for_source(source_path: Path, skill_id: str) -> Path:
    source = source_path.expanduser()
    skill_dir = source.parent if source.name == "SKILL.md" else source
    if skill_dir.name != skill_id:
        candidate = skill_dir / skill_id
        if (candidate / "SKILL.md").exists():
            return skill_dir
    return skill_dir.parent


def run_openclaw_central_restore(
    repo_root: Path,
    skill_ids: Sequence[str],
    *,
    yes: bool = False,
    allow_local_writes: bool = False,
    timeout_seconds: int = 900,
) -> dict:
    if yes and not allow_local_writes:
        raise OperatorExecutorError("OpenClaw restore is disabled; start operator-executor with --allow-local-writes")
    repo = repo_root.expanduser().resolve()
    script = repo / "scripts" / "openclaw-restore-from-central.sh"
    if not script.exists():
        raise OperatorExecutorError(f"OpenClaw restore helper not found: {script}")
    normalized = _normalize_skill_ids(skill_ids)
    command = [str(script), "--yes" if yes else "--dry-run", *normalized]
    started_at = datetime.now(timezone.utc).isoformat()
    proc = subprocess.run(
        command,
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    finished_at = datetime.now(timezone.utc).isoformat()
    parsed = _last_json_object(proc.stdout)
    return {
        "ok": proc.returncode == 0,
        "mode": "restore" if yes else "dry_run",
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": proc.returncode,
        "command": " ".join(command),
        "skill_ids": normalized,
        "safe_to_restore": bool(parsed.get("safe_to_restore")) if isinstance(parsed, dict) else False,
        "restored": parsed.get("restored") if isinstance(parsed, dict) else None,
        "result": parsed,
        "stdout_tail": _tail(proc.stdout),
        "stderr_tail": _tail(proc.stderr),
    }


def run_openclaw_conflict_package(
    repo_root: Path,
    skill_ids: Sequence[str],
    *,
    timeout_seconds: int = 900,
) -> dict:
    repo = repo_root.expanduser().resolve()
    script = repo / "scripts" / "openclaw-conflict-package.sh"
    if not script.exists():
        raise OperatorExecutorError(f"OpenClaw conflict package helper not found: {script}")
    normalized = _normalize_skill_ids(skill_ids)
    command = [str(script), *normalized]
    started_at = datetime.now(timezone.utc).isoformat()
    proc = subprocess.run(
        command,
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    finished_at = datetime.now(timezone.utc).isoformat()
    parsed = _last_json_object(proc.stdout)
    return {
        "ok": proc.returncode == 0,
        "mode": "conflict_package",
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": proc.returncode,
        "command": " ".join(command),
        "skill_ids": normalized,
        "total_conflicts": parsed.get("total_conflicts") if isinstance(parsed, dict) else None,
        "packages": _enrich_conflict_packages(parsed.get("packages") if isinstance(parsed, dict) else []),
        "result": parsed,
        "stdout_tail": _tail(proc.stdout),
        "stderr_tail": _tail(proc.stderr),
    }


def _enrich_conflict_packages(packages: object) -> list[dict]:
    if not isinstance(packages, list):
        return []
    enriched: list[dict] = []
    for raw in packages:
        if not isinstance(raw, dict):
            continue
        package = dict(raw)
        package_path = Path(str(package.get("path") or ""))
        if package_path.exists() and package_path.is_dir():
            package["review"] = {
                "local_label": "OpenClaw 版",
                "remote_label": "共享仓库版",
                "base_label": "共同基线",
                "local": _summarize_conflict_material(package_path / "local"),
                "remote": _summarize_conflict_material(package_path / "remote"),
                "base": _summarize_conflict_material(package_path / "base"),
                "decision_hint": "先比较 OpenClaw 版和共享仓库版；确定哪边正确后，再选择写入动作。",
            }
        enriched.append(package)
    return enriched


def _summarize_conflict_material(path: Path) -> dict:
    if not path.exists():
        return {"state": "absent", "title": "缺失", "description": "", "file_count": 0, "files": []}
    if not path.is_dir():
        return {"state": "unknown", "title": path.name, "description": "", "file_count": 0, "files": []}

    files = sorted(
        str(file.relative_to(path))
        for file in path.rglob("*")
        if file.is_file() and "__pycache__" not in file.parts
    )
    skill_md = path / "SKILL.md"
    title = path.name
    description = ""
    if skill_md.exists():
        title, description = _summarize_skill_md(skill_md)
    return {
        "state": "present",
        "title": title,
        "description": description,
        "file_count": len(files),
        "files": files[:12],
        "has_more_files": len(files) > 12,
        "skill_md": str(skill_md) if skill_md.exists() else None,
    }


def _summarize_skill_md(path: Path) -> tuple[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return path.parent.name, ""
    frontmatter = _frontmatter(text)
    title = frontmatter.get("name") or _first_heading(text) or path.parent.name
    description = frontmatter.get("description") or _first_paragraph(text)
    return title.strip() or path.parent.name, description.strip()


def _frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    values: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key in {"name", "description"}:
            values[key] = value.strip().strip("\"'")
    return values


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _first_paragraph(text: str) -> str:
    in_frontmatter = False
    frontmatter_done = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "---" and not frontmatter_done:
            in_frontmatter = not in_frontmatter
            if not in_frontmatter:
                frontmatter_done = True
            continue
        if in_frontmatter or not stripped or stripped.startswith("#"):
            continue
        return stripped
    return ""


def _normalize_skill_ids(skill_ids: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in skill_ids:
        skill_id = str(raw).strip()
        if not skill_id:
            continue
        if not SKILL_ID_RE.match(skill_id):
            raise OperatorExecutorError(f"invalid skill id: {skill_id}")
        if skill_id not in seen:
            result.append(skill_id)
            seen.add(skill_id)
    if not result:
        raise OperatorExecutorError("at least one skill id is required")
    return result


def _normalize_tool_id(tool_id: object) -> str:
    normalized = str(tool_id or "").strip().lower()
    if normalized == "claude":
        normalized = "claude-code"
    if normalized not in MAC_TOOL_INSTALL_TARGETS:
        raise OperatorExecutorError(f"unsupported Mac tool id: {normalized or '<empty>'}")
    return normalized


def _timestamp_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


def _last_json_object(text: str) -> Optional[dict]:
    decoder = JSONDecoder()
    found: Optional[dict] = None
    index = 0
    while True:
        start = text.find("{", index)
        if start < 0:
            return found
        try:
            value, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(value, dict):
            found = value
        index = start + end


def _tail(text: str, *, max_chars: int = 6000) -> str:
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def serve_operator_executor(host: str, port: int, repo_root: Path, *, allow_publish: bool = False, allow_local_writes: bool = False) -> None:
    repo = repo_root.expanduser().resolve()

    class OperatorExecutorHandler(BaseHTTPRequestHandler):
        server_version = "SkillSyncOperatorExecutor/0"

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._send_json(204, {})

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "service": "skill-sync-operator-executor",
                        "repo_root": str(repo),
                        "allow_publish": allow_publish,
                        "allow_local_writes": allow_local_writes,
                        "time": datetime.now(timezone.utc).isoformat(),
                    },
                )
                return
            if path == "/api/local-workspace":
                tools = build_device_tool_status()
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "device_id": "mac",
                        "device_name": "Mac 本机",
                        "scope": "local",
                        "authority": "本机 executor 只读取本机 skill 目录；默认只允许 dry-run，不默认发布。",
                        "allow_publish": allow_publish,
                        "allow_local_writes": allow_local_writes,
                        "measured_at": datetime.now(timezone.utc).isoformat(),
                        "tools": tools,
                        "total_skills": sum(int(tool.get("skills") or 0) for tool in tools),
                        "operations": {
                            "scan_local": True,
                            "dry_run": True,
                            "analyze_local_skill": True,
                            "install_local_skill": allow_local_writes,
                            "publish_to_central": allow_publish,
                            "operate_other_devices": False,
                        },
                    },
                )
                return
            self._send_json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            try:
                payload = self._read_json()
                skill_ids = payload.get("skill_ids") if isinstance(payload, dict) else None
                allow_conflict_local_wins = bool(payload.get("allow_conflict_local_wins")) if isinstance(payload, dict) else False
                if path == "/api/openclaw-approved-push-dry-run":
                    result = run_openclaw_approved_push_batch(
                        repo,
                        skill_ids or [],
                        yes=False,
                        allow_conflict_local_wins=allow_conflict_local_wins,
                    )
                    self._send_json(200 if result["ok"] else 500, result)
                    return
                if path == "/api/openclaw-approved-push-publish":
                    confirm = payload.get("confirm") if isinstance(payload, dict) else None
                    if confirm != "PUBLISH":
                        self._send_json(400, {"ok": False, "error": "confirm must be PUBLISH"})
                        return
                    result = run_openclaw_approved_push_batch(
                        repo,
                        skill_ids or [],
                        yes=True,
                        allow_publish=allow_publish,
                        allow_conflict_local_wins=allow_conflict_local_wins,
                        refresh_peer_status=True,
                    )
                    self._send_json(200 if result["ok"] else 500, result)
                    return
                if path == "/api/openclaw-peer-status-refresh":
                    result = run_openclaw_peer_status_refresh(repo)
                    self._send_json(200 if result["ok"] else 500, result)
                    return
                if path == "/api/mac-peer-status-refresh":
                    result = run_mac_peer_status_refresh(repo)
                    self._send_json(200 if result["ok"] else 500, result)
                    return
                if path == "/api/mac-central-restore-dry-run":
                    result = run_mac_central_restore(skill_ids or [], yes=False)
                    self._send_json(200, result)
                    return
                if path == "/api/mac-central-restore":
                    confirm = payload.get("confirm") if isinstance(payload, dict) else None
                    if confirm != "RESTORE":
                        self._send_json(400, {"ok": False, "error": "confirm must be RESTORE"})
                        return
                    result = run_mac_central_restore(
                        skill_ids or [],
                        yes=True,
                        allow_local_writes=allow_local_writes,
                    )
                    refresh = run_mac_peer_status_refresh(repo)
                    result["peer_status_refresh"] = refresh
                    self._send_json(200 if result["ok"] and refresh["ok"] else 500, result)
                    return
                if path == "/api/mac-codex-install-from-central-dry-run":
                    result = run_mac_codex_install_from_central(skill_ids or [], yes=False)
                    self._send_json(200, result)
                    return
                if path == "/api/mac-codex-install-from-central":
                    confirm = payload.get("confirm") if isinstance(payload, dict) else None
                    if confirm != "INSTALL":
                        self._send_json(400, {"ok": False, "error": "confirm must be INSTALL"})
                        return
                    result = run_mac_codex_install_from_central(
                        skill_ids or [],
                        yes=True,
                        allow_local_writes=allow_local_writes,
                    )
                    refresh = run_mac_peer_status_refresh(repo)
                    result["peer_status_refresh"] = refresh
                    self._send_json(200 if result["ok"] and refresh["ok"] else 500, result)
                    return
                if path == "/api/mac-tool-install-from-central-dry-run":
                    result = run_mac_tool_install_from_central(_payload_tool_id(payload), skill_ids or [], yes=False)
                    self._send_json(200, result)
                    return
                if path == "/api/mac-tool-install-from-central":
                    confirm = payload.get("confirm") if isinstance(payload, dict) else None
                    if confirm != "INSTALL":
                        self._send_json(400, {"ok": False, "error": "confirm must be INSTALL"})
                        return
                    result = run_mac_tool_install_from_central(
                        _payload_tool_id(payload),
                        skill_ids or [],
                        yes=True,
                        allow_local_writes=allow_local_writes,
                    )
                    refresh = run_mac_peer_status_refresh(repo)
                    result["peer_status_refresh"] = refresh
                    self._send_json(200 if result["ok"] and refresh["ok"] else 500, result)
                    return
                if path == "/api/mac-tool-uninstall-dry-run":
                    result = run_mac_tool_uninstall(_payload_tool_id(payload), skill_ids or [], yes=False)
                    self._send_json(200 if result["ok"] else 500, result)
                    return
                if path == "/api/mac-tool-uninstall":
                    confirm = payload.get("confirm") if isinstance(payload, dict) else None
                    if confirm != "REMOVE":
                        self._send_json(400, {"ok": False, "error": "confirm must be REMOVE"})
                        return
                    result = run_mac_tool_uninstall(
                        _payload_tool_id(payload),
                        skill_ids or [],
                        yes=True,
                        allow_local_writes=allow_local_writes,
                    )
                    refresh = run_mac_peer_status_refresh(repo)
                    result["peer_status_refresh"] = refresh
                    self._send_json(200 if result["ok"] and refresh["ok"] else 500, result)
                    return
                if path == "/api/central-deprecate-dry-run":
                    reason = _payload_reason(payload)
                    result = run_central_deprecate(skill_ids or [], reason=reason, yes=False)
                    self._send_json(200, result)
                    return
                if path == "/api/central-deprecate":
                    confirm = payload.get("confirm") if isinstance(payload, dict) else None
                    if confirm != "DEPRECATE":
                        self._send_json(400, {"ok": False, "error": "confirm must be DEPRECATE"})
                        return
                    reason = _payload_reason(payload)
                    result = run_central_deprecate(
                        skill_ids or [],
                        reason=reason,
                        yes=True,
                        allow_publish=allow_publish,
                    )
                    refresh = run_mac_peer_status_refresh(repo)
                    result["peer_status_refresh"] = refresh
                    self._send_json(200 if result["ok"] and refresh["ok"] else 500, result)
                    return
                if path == "/api/central-reactivate-dry-run":
                    reason = _payload_reason(payload)
                    result = run_central_reactivate(skill_ids or [], reason=reason, yes=False)
                    self._send_json(200, result)
                    return
                if path == "/api/central-reactivate":
                    confirm = payload.get("confirm") if isinstance(payload, dict) else None
                    if confirm != "REACTIVATE":
                        self._send_json(400, {"ok": False, "error": "confirm must be REACTIVATE"})
                        return
                    reason = _payload_reason(payload)
                    result = run_central_reactivate(
                        skill_ids or [],
                        reason=reason,
                        yes=True,
                        allow_publish=allow_publish,
                    )
                    refresh = run_mac_peer_status_refresh(repo)
                    result["peer_status_refresh"] = refresh
                    self._send_json(200 if result["ok"] and refresh["ok"] else 500, result)
                    return
                if path == "/api/openclaw-central-restore-dry-run":
                    result = run_openclaw_central_restore(repo, skill_ids or [], yes=False)
                    self._send_json(200 if result["ok"] else 500, result)
                    return
                if path == "/api/openclaw-central-restore":
                    confirm = payload.get("confirm") if isinstance(payload, dict) else None
                    if confirm != "RESTORE":
                        self._send_json(400, {"ok": False, "error": "confirm must be RESTORE"})
                        return
                    result = run_openclaw_central_restore(
                        repo,
                        skill_ids or [],
                        yes=True,
                        allow_local_writes=allow_local_writes,
                    )
                    refresh = run_openclaw_peer_status_refresh(repo)
                    result["peer_status_refresh"] = refresh
                    self._send_json(200 if result["ok"] and refresh["ok"] else 500, result)
                    return
                if path == "/api/openclaw-conflict-package":
                    result = run_openclaw_conflict_package(repo, skill_ids or [])
                    self._send_json(200 if result["ok"] else 500, result)
                    return
                if path == "/api/local-skill/analyze":
                    source_path = _payload_path(payload)
                    tool_ids = _payload_tool_ids(payload)
                    result = analyze_local_skill(source_path, tool_ids=tool_ids)
                    result["allow_local_writes"] = allow_local_writes
                    self._send_json(200, result)
                    return
                if path == "/api/local-skill/install":
                    confirm = payload.get("confirm") if isinstance(payload, dict) else None
                    if confirm != "INSTALL":
                        self._send_json(400, {"ok": False, "error": "confirm must be INSTALL"})
                        return
                    source_path = _payload_path(payload)
                    tool_ids = _payload_tool_ids(payload)
                    result = install_local_skill(
                        source_path,
                        tool_ids=tool_ids,
                        yes=True,
                        allow_local_writes=allow_local_writes,
                    )
                    self._send_json(200, result)
                    return
                if path == "/api/local-skill/publish-dry-run":
                    source_path = _payload_path(payload)
                    analysis = analyze_local_skill(source_path)
                    skill_id = analysis["skill_id"]
                    local_root = local_publish_root_for_source(source_path, skill_id)
                    remote, prefix = _local_publish_remote()
                    result = publish_local_skill(
                        local_root,
                        Path.home() / "public-sync" / "skill-sync-sidecar-dev" / "current-mac",
                        skill_id,
                        remote,
                        remote_prefix=prefix,
                        last_applied_record=Path.home() / "Library" / "Application Support" / "skill-sync-sidecar" / "base-record.json",
                        base_record_out=Path.home() / "Library" / "Application Support" / "skill-sync-sidecar" / "base-record.json",
                    )
                    self._send_json(200, result)
                    return
                if path == "/api/local-skill/publish":
                    if not allow_publish:
                        self._send_json(403, {"ok": False, "error": "publish is disabled; start operator-executor with --allow-publish"})
                        return
                    confirm = payload.get("confirm") if isinstance(payload, dict) else None
                    if confirm != "PUBLISH":
                        self._send_json(400, {"ok": False, "error": "confirm must be PUBLISH"})
                        return
                    source_path = _payload_path(payload)
                    analysis = analyze_local_skill(source_path)
                    skill_id = analysis["skill_id"]
                    local_root = local_publish_root_for_source(source_path, skill_id)
                    remote, prefix = _local_publish_remote()
                    result = publish_local_skill(
                        local_root,
                        Path.home() / "public-sync" / "skill-sync-sidecar-dev" / "current-mac",
                        skill_id,
                        remote,
                        remote_prefix=prefix,
                        last_applied_record=Path.home() / "Library" / "Application Support" / "skill-sync-sidecar" / "base-record.json",
                        base_record_out=Path.home() / "Library" / "Application Support" / "skill-sync-sidecar" / "base-record.json",
                        yes=True,
                    )
                    self._send_json(200, result)
                    return
                self._send_json(404, {"ok": False, "error": "not found"})
            except (OperatorExecutorError, LocalSkillError, RestoreError, CentralLifecycleError, ConfigError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8"))

        def _send_json(self, status: int, payload: dict) -> None:
            body = b"" if status == 204 else (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            if os.environ.get("SKILL_SYNC_EXECUTOR_LOG_REQUESTS") == "1":
                super().log_message(format, *args)

    server = ThreadingHTTPServer((host, port), OperatorExecutorHandler)
    print(f"skill-sync operator executor: http://{host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _payload_path(payload: object) -> Path:
    if not isinstance(payload, dict):
        raise OperatorExecutorError("payload must be a JSON object")
    raw_path = str(payload.get("path") or "").strip()
    if not raw_path:
        raise OperatorExecutorError("path is required")
    return Path(raw_path).expanduser()


def _payload_tool_ids(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    raw = payload.get("tool_ids")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise OperatorExecutorError("tool_ids must be a list")
    return [str(item) for item in raw if str(item).strip()]


def _payload_tool_id(payload: object) -> str:
    if not isinstance(payload, dict):
        raise OperatorExecutorError("payload must be a JSON object")
    return _normalize_tool_id(payload.get("tool_id"))


def _payload_reason(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("reason") or "").strip()[:500]


def _local_publish_remote():
    settings = load_cc_switch_webdav_settings()
    remote = open_remote(settings.base_url, username=settings.username, password=settings.password)
    prefix = os.environ.get("SKILL_SYNC_PREFIX", "skill-sync-sidecar-dev/current-mac")
    return remote, prefix
