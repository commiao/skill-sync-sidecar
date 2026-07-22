from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from json import JSONDecoder
from pathlib import Path
from typing import Optional, Sequence

from .config import ConfigError, load_cc_switch_webdav_settings
from .local_skill import LocalSkillError, analyze_local_skill, install_local_skill, publish_local_skill
from .remote import open_remote
from .restore import RestoreError, restore_from_central
from .tool_status import build_device_tool_status


SKILL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class OperatorExecutorError(RuntimeError):
    pass


def run_openclaw_approved_push_batch(
    repo_root: Path,
    skill_ids: Sequence[str],
    *,
    yes: bool = False,
    timeout_seconds: int = 900,
    allow_publish: bool = False,
) -> dict:
    repo = repo_root.expanduser().resolve()
    script = repo / "scripts" / "openclaw-approved-push-batch.sh"
    if not script.exists():
        raise OperatorExecutorError(f"approved-push helper not found: {script}")
    normalized = _normalize_skill_ids(skill_ids)
    if yes and not allow_publish:
        raise OperatorExecutorError("publish is disabled; set SKILL_SYNC_EXECUTOR_ALLOW_PUBLISH=1 to enable --yes")
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
        "mode": "publish" if yes else "dry_run",
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": proc.returncode,
        "command": " ".join(command),
        "skill_ids": normalized,
        "safe_to_push": bool(parsed.get("safe_to_push")) if isinstance(parsed, dict) else False,
        "approved": parsed.get("approved") if isinstance(parsed, dict) else None,
        "approved_skill_ids": parsed.get("approved_skill_ids") if isinstance(parsed, dict) else normalized,
        "result": parsed,
        "stdout_tail": _tail(proc.stdout),
        "stderr_tail": _tail(proc.stderr),
    }


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
        "packages": parsed.get("packages") if isinstance(parsed, dict) else [],
        "result": parsed,
        "stdout_tail": _tail(proc.stdout),
        "stderr_tail": _tail(proc.stderr),
    }


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
                if path == "/api/openclaw-approved-push-dry-run":
                    result = run_openclaw_approved_push_batch(repo, skill_ids or [], yes=False)
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
                    skill_id = analyze_local_skill(source_path)["skill_id"]
                    remote, prefix = _local_publish_remote()
                    result = publish_local_skill(
                        Path.home() / ".cc-switch" / "skills",
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
                    skill_id = analyze_local_skill(source_path)["skill_id"]
                    remote, prefix = _local_publish_remote()
                    result = publish_local_skill(
                        Path.home() / ".cc-switch" / "skills",
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
            except (OperatorExecutorError, LocalSkillError, RestoreError, ConfigError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8"))

        def _send_json(self, status: int, payload: dict) -> None:
            body = b"" if status == 204 else (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
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


def _local_publish_remote():
    settings = load_cc_switch_webdav_settings()
    remote = open_remote(settings.base_url, username=settings.username, password=settings.password)
    prefix = os.environ.get("SKILL_SYNC_PREFIX", "skill-sync-sidecar-dev/current-mac")
    return remote, prefix
