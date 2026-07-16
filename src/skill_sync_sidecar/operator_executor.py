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


def serve_operator_executor(host: str, port: int, repo_root: Path, *, allow_publish: bool = False) -> None:
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
                        "time": datetime.now(timezone.utc).isoformat(),
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
                self._send_json(404, {"ok": False, "error": "not found"})
            except (OperatorExecutorError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
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
