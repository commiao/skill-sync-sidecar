from __future__ import annotations

import base64
import json
import os
import socket
import shutil
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree


class RemoteError(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteEntry:
    path: str
    kind: str
    size: Optional[int] = None


@dataclass
class UploadPlan:
    files: List[Tuple[Path, str]]

    @property
    def total_bytes(self) -> int:
        return sum(path.stat().st_size for path, _ in self.files)


class Remote:
    def exists(self, path: str) -> bool:
        raise NotImplementedError

    def list(self, path: str = "") -> List[RemoteEntry]:
        raise NotImplementedError

    def get_bytes(self, path: str) -> bytes:
        raise NotImplementedError

    def put_bytes(self, path: str, data: bytes) -> None:
        raise NotImplementedError

    def ensure_dir(self, path: str) -> None:
        raise NotImplementedError


class FileRemote(Remote):
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()

    def _path(self, path: str) -> Path:
        clean = path.strip("/")
        target = (self.root / clean).resolve()
        if target != self.root and self.root not in target.parents:
            raise RemoteError(f"Path escapes file remote root: {path}")
        return target

    def exists(self, path: str) -> bool:
        return self._path(path).exists()

    def list(self, path: str = "") -> List[RemoteEntry]:
        target = self._path(path)
        if not target.exists():
            return []
        if target.is_file():
            rel = target.relative_to(self.root).as_posix()
            return [RemoteEntry(rel, "file", target.stat().st_size)]
        entries = []
        for child in sorted(target.iterdir()):
            rel = child.relative_to(self.root).as_posix()
            entries.append(RemoteEntry(rel, "dir" if child.is_dir() else "file", None if child.is_dir() else child.stat().st_size))
        return entries

    def get_bytes(self, path: str) -> bytes:
        try:
            return self._path(path).read_bytes()
        except OSError as exc:
            raise RemoteError(str(exc)) from exc

    def put_bytes(self, path: str, data: bytes) -> None:
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def ensure_dir(self, path: str) -> None:
        self._path(path).mkdir(parents=True, exist_ok=True)


class WebDavRemote(Remote):
    def __init__(
        self,
        base_url: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: int = 30,
        retries: int = 2,
        retry_delay: float = 0.5,
    ):
        self.base_url = base_url.rstrip("/") + "/"
        self.username = username
        self.password = password
        self.timeout = timeout
        self.retries = retries
        self.retry_delay = retry_delay
        self._list_cache: dict[str, List[RemoteEntry]] = {}

    def _url(self, path: str) -> str:
        clean = "/".join(quote(part) for part in path.strip("/").split("/") if part)
        return self.base_url + clean

    def _request(self, method: str, path: str = "", data: Optional[bytes] = None, headers: Optional[dict] = None) -> bytes:
        request_headers = dict(headers or {})
        if self.username is not None and self.password is not None:
            token = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
            request_headers["Authorization"] = f"Basic {token}"
        request = Request(self._url(path), data=data, headers=request_headers, method=method)
        for attempt in range(self.retries + 1):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return response.read()
            except HTTPError as exc:
                raise RemoteError(f"HTTP {exc.code} {exc.reason} for {method} {path}") from exc
            except (URLError, socket.timeout, TimeoutError, OSError) as exc:
                if attempt >= self.retries:
                    reason = exc.reason if isinstance(exc, URLError) else str(exc)
                    raise RemoteError(f"Network error for {method} {path}: {reason}") from exc
                time.sleep(self.retry_delay)
        raise RemoteError(f"Network error for {method} {path}: retry loop exhausted")

    def exists(self, path: str) -> bool:
        try:
            self._request("HEAD", path)
            return True
        except RemoteError as exc:
            if "HTTP 404" in str(exc):
                return False
            return self._exists_via_propfind(path)

    def list(self, path: str = "") -> List[RemoteEntry]:
        body = self._request("PROPFIND", path, headers={"Depth": "1"})
        return parse_propfind(body, path)

    def _exists_via_propfind(self, path: str) -> bool:
        clean = path.strip("/")
        parent_path = str(PurePosixPath(clean).parent)
        parent = "" if parent_path == "." else parent_path
        try:
            entries = self._list_cache.get(parent)
            if entries is None:
                entries = self.list(parent)
                self._list_cache[parent] = entries
        except RemoteError as exc:
            if "HTTP 404" in str(exc):
                return False
            raise
        return any(entry.path.strip("/").endswith(clean) for entry in entries)

    def get_bytes(self, path: str) -> bytes:
        return self._request("GET", path)

    def put_bytes(self, path: str, data: bytes) -> None:
        self._request("PUT", path, data=data)

    def ensure_dir(self, path: str) -> None:
        parts = [part for part in path.strip("/").split("/") if part]
        current = ""
        for part in parts:
            current = f"{current}/{part}" if current else part
            try:
                self._request("MKCOL", current)
            except RemoteError as exc:
                if "HTTP 405" not in str(exc) and "HTTP 409" not in str(exc):
                    raise


def parse_propfind(body: bytes, requested_path: str = "") -> List[RemoteEntry]:
    namespace = {"d": "DAV:"}
    root = ElementTree.fromstring(body)
    entries: List[RemoteEntry] = []
    requested = requested_path.strip("/")
    for response in root.findall("d:response", namespace):
        href = response.findtext("d:href", default="", namespaces=namespace)
        path = unquote(urlparse(href).path).strip("/")
        if requested and path.endswith(requested):
            continue
        prop = response.find("d:propstat/d:prop", namespace)
        if prop is None:
            continue
        resource_type = prop.find("d:resourcetype", namespace)
        kind = "dir" if resource_type is not None and resource_type.find("d:collection", namespace) is not None else "file"
        size_text = prop.findtext("d:getcontentlength", default=None, namespaces=namespace)
        size = int(size_text) if size_text and size_text.isdigit() else None
        entries.append(RemoteEntry(path, kind, size))
    return entries


def open_remote(
    url: str,
    username_env: str = "SKILL_SYNC_WEBDAV_USER",
    password_env: str = "SKILL_SYNC_WEBDAV_PASSWORD",
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> Remote:
    parsed = urlparse(url)
    if parsed.scheme == "file":
        return FileRemote(Path(unquote(parsed.path)))
    if parsed.scheme in {"http", "https"}:
        return WebDavRemote(
            url,
            username if username is not None else os.environ.get(username_env),
            password if password is not None else os.environ.get(password_env),
        )
    if not parsed.scheme:
        return FileRemote(Path(url))
    raise RemoteError(f"Unsupported remote URL scheme: {parsed.scheme}")


def build_upload_plan(snapshot_dir: Path) -> UploadPlan:
    files = []
    for path in sorted(snapshot_dir.rglob("*")):
        if path.is_file():
            files.append((path, path.relative_to(snapshot_dir).as_posix()))
    return UploadPlan(files)


def upload_snapshot(
    snapshot_dir: Path,
    remote: Remote,
    remote_prefix: str = "",
    include_paths: Optional[Set[str]] = None,
    skip_existing_archives: bool = True,
) -> UploadPlan:
    plan = build_upload_plan(snapshot_dir)
    uploaded_files: List[Tuple[Path, str]] = []
    ordered_files = sorted(plan.files, key=lambda item: item[1] == "index.json")
    for local_path, rel_path in ordered_files:
        if include_paths is not None and rel_path != "index.json" and rel_path not in include_paths:
            continue
        remote_path = join_remote_path(remote_prefix, rel_path)
        if rel_path != "index.json" and skip_existing_archives and remote.exists(remote_path):
            continue
        parent_path = str(PurePosixPath(remote_path).parent)
        if parent_path != ".":
            parent = parent_path
            remote.ensure_dir(parent)
        remote.put_bytes(remote_path, local_path.read_bytes())
        uploaded_files.append((local_path, rel_path))
    return UploadPlan(uploaded_files)


def download_snapshot(remote: Remote, cache_dir: Path, remote_prefix: str = "") -> dict:
    index_path = join_remote_path(remote_prefix, "index.json")
    index = json.loads(remote.get_bytes(index_path).decode("utf-8"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for skill in index.get("skills", []):
        archive = skill.get("archive")
        if not archive:
            continue
        target = cache_dir / archive
        if target.exists() and target.stat().st_size > 0:
            continue
        data = remote.get_bytes(join_remote_path(remote_prefix, archive))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return index


def copy_snapshot_to_file_remote(snapshot_dir: Path, output_dir: Path) -> None:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    shutil.copytree(snapshot_dir, output_dir)


def join_remote_path(prefix: str, path: str) -> str:
    left = prefix.strip("/")
    right = path.strip("/")
    if left and right:
        return f"{left}/{right}"
    return left or right
