#!/usr/bin/env python3
"""Python 3.6 compatible WebDAV probe for OpenClaw validation.

Downloads one sidecar snapshot from cc-switch WebDAV settings, stages archives
into an isolated directory, validates content hashes, and emits a JSON report.
This is intentionally not the full sidecar runtime; OpenClaw currently has
Python 3.6, so this script is the safe bridge for read/write mechanism probes.
"""

from __future__ import print_function

import argparse
import base64
import hashlib
import json
import os
import shutil
import sys
import zipfile

try:
    from urllib.parse import quote
    from urllib.request import Request, urlopen
except ImportError:  # pragma: no cover - Python 2 fallback, kept harmless.
    from urllib import quote
    from urllib2 import Request, urlopen


def load_webdav_settings(path):
    with open(path, "r") as handle:
        data = json.load(handle)
    webdav = data.get("webdavSync") or data.get("webdav_sync") or {}
    base_url = webdav.get("baseUrl") or webdav.get("base_url")
    if not base_url:
        raise RuntimeError("WebDAV baseUrl missing in %s" % path)
    return {
        "base_url": base_url.rstrip("/") + "/",
        "username": webdav.get("username"),
        "password": webdav.get("password"),
    }


def remote_url(base_url, path):
    clean = "/".join(quote(part) for part in path.strip("/").split("/") if part)
    return base_url + clean


def request_bytes(settings, path, timeout):
    headers = {}
    username = settings.get("username")
    password = settings.get("password")
    if username is not None and password is not None:
        token = ("%s:%s" % (username, password)).encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(token).decode("ascii")
    request = Request(remote_url(settings["base_url"], path), headers=headers)
    response = urlopen(request, timeout=timeout)
    try:
        return response.read()
    finally:
        response.close()


def join_remote(prefix, path):
    left = prefix.strip("/")
    right = path.strip("/")
    if left and right:
        return left + "/" + right
    return left or right


def safe_extract(archive, target_dir):
    for name in archive.namelist():
        if name.endswith("/"):
            continue
        parts = name.split("/")
        if name.startswith("/") or ".." in parts:
            raise RuntimeError("unsafe archive member: %s" % name)
        if name == ".skill-sync/manifest.json":
            continue
        out_path = os.path.join(target_dir, name)
        parent = os.path.dirname(out_path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(out_path, "wb") as handle:
            handle.write(archive.read(name))


def sha256_file(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def hash_skill_files(files):
    hasher = hashlib.sha256()
    for item in sorted(files, key=lambda entry: entry["path"]):
        hasher.update(item["path"].encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(str(item["size"]).encode("ascii"))
        hasher.update(b"\0")
        hasher.update(item["sha256"].encode("ascii"))
        hasher.update(b"\0")
    return hasher.hexdigest()


def validate_staged_skill(target_dir, manifest, expected_hash):
    files = []
    for item in manifest.get("files") or []:
        rel_path = item.get("path")
        expected_sha = item.get("sha256")
        expected_size = item.get("size")
        if not rel_path or not expected_sha:
            raise RuntimeError("manifest file entry missing path or sha256")
        if rel_path.startswith("/") or ".." in rel_path.split("/"):
            raise RuntimeError("unsafe manifest path: %s" % rel_path)
        path = os.path.join(target_dir, rel_path)
        if not os.path.isfile(path):
            raise RuntimeError("staged file missing: %s" % rel_path)
        actual_size = os.path.getsize(path)
        actual_sha = sha256_file(path)
        if expected_size is not None and int(expected_size) != actual_size:
            raise RuntimeError("size mismatch for %s" % rel_path)
        if expected_sha != actual_sha:
            raise RuntimeError("sha256 mismatch for %s" % rel_path)
        files.append({"path": rel_path, "size": actual_size, "sha256": actual_sha})
    actual_hash = hash_skill_files(files)
    manifest_hash = manifest.get("content_hash")
    if manifest_hash and actual_hash != manifest_hash:
        raise RuntimeError("manifest content_hash mismatch")
    if expected_hash and actual_hash != expected_hash:
        raise RuntimeError("index content_hash mismatch")
    return actual_hash


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", default="/home/admin/.cc-switch/settings.json")
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--skill-id", default="sync-probe")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args(argv)

    settings = load_webdav_settings(args.settings)
    if os.path.exists(args.out):
        shutil.rmtree(args.out)
    os.makedirs(args.out)
    cache_dir = os.path.join(args.out, "cache")
    stage_dir = os.path.join(args.out, "staged")
    os.makedirs(cache_dir)
    os.makedirs(stage_dir)

    index_bytes = request_bytes(settings, join_remote(args.prefix, "index.json"), args.timeout)
    index_path = os.path.join(cache_dir, "index.json")
    with open(index_path, "wb") as handle:
        handle.write(index_bytes)
    index = json.loads(index_bytes.decode("utf-8"))

    matches = [skill for skill in index.get("skills") or [] if skill.get("skill_id") == args.skill_id]
    if len(matches) != 1:
        raise RuntimeError("expected exactly one %s skill, found %s" % (args.skill_id, len(matches)))
    skill = matches[0]
    archive_rel = skill.get("archive")
    if not archive_rel:
        raise RuntimeError("skill has no archive path")
    archive_bytes = request_bytes(settings, join_remote(args.prefix, archive_rel), args.timeout)
    archive_path = os.path.join(cache_dir, archive_rel)
    archive_parent = os.path.dirname(archive_path)
    if archive_parent and not os.path.isdir(archive_parent):
        os.makedirs(archive_parent)
    with open(archive_path, "wb") as handle:
        handle.write(archive_bytes)

    target_dir = os.path.join(stage_dir, skill.get("source") or "unknown", skill.get("skill_id") or args.skill_id)
    os.makedirs(target_dir)
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read(".skill-sync/manifest.json").decode("utf-8"))
        safe_extract(archive, target_dir)
    actual_hash = validate_staged_skill(target_dir, manifest, skill.get("content_hash"))

    skill_md_path = os.path.join(target_dir, "SKILL.md")
    if not os.path.isfile(skill_md_path):
        raise RuntimeError("SKILL.md missing after stage")
    with open(skill_md_path, "r") as handle:
        skill_md_head = handle.read(512)

    report = {
        "ok": True,
        "prefix": args.prefix,
        "snapshot_id": index.get("snapshot_id"),
        "remote_total": index.get("total"),
        "skill_id": skill.get("skill_id"),
        "name": skill.get("name"),
        "description": skill.get("description"),
        "content_hash": skill.get("content_hash"),
        "actual_hash": actual_hash,
        "file_count": skill.get("file_count"),
        "staged_path": target_dir,
        "skill_md_has_frontmatter": skill_md_head.startswith("---\n"),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
