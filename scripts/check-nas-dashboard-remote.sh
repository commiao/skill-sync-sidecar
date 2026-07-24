#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON:-python3}"
webdav_path="${SKILL_SYNC_NAS_DASHBOARD_WEBDAV_PATH:-skill-sync-sidecar-dashboard/status.json}"
webdav_http_base="${SKILL_SYNC_NAS_WEBDAV_HTTP_BASE:-http://100.123.208.32:5005/public-sync}"
webdav_http_path="${SKILL_SYNC_NAS_WEBDAV_HTTP_PATH:-skill-sync-sidecar-dashboard/index.html}"
http_base="${SKILL_SYNC_NAS_HTTP_BASE:-http://100.123.208.32}"
http_path="${SKILL_SYNC_NAS_DASHBOARD_HTTP_PATH:-/skill-sync-sidecar-dashboard/index.html}"

PYTHONPATH="$repo_root/src" \
WEBDAV_PATH="$webdav_path" \
WEBDAV_HTTP_BASE="$webdav_http_base" \
WEBDAV_HTTP_PATH="$webdav_http_path" \
"$python_bin" - <<'PY'
import json
import os

from skill_sync_sidecar.config import load_cc_switch_webdav_settings
from skill_sync_sidecar.remote import RemoteError
from skill_sync_sidecar.remote import WebDavRemote
from skill_sync_sidecar.config import ConfigError

def report_failure(reason: str, detail: str) -> None:
    print("check_remote_ok=false")
    print(f"check_error={reason}")
    print(f"check_error_detail={detail}")
    raise SystemExit(2)

try:
    settings = load_cc_switch_webdav_settings()
except (ConfigError, RuntimeError) as exc:
    report_failure("config", str(exc))

remote = WebDavRemote(settings.base_url, settings.username, settings.password, timeout=15, retries=1)
try:
    raw = remote.get_bytes(os.environ["WEBDAV_PATH"])
except RemoteError as exc:
    report_failure("webdav", str(exc))
data = json.loads(raw.decode("utf-8"))
print("webdav_ok=true")
print(f"webdav_path={os.environ['WEBDAV_PATH']}")
print(f"webdav_bytes={len(raw)}")
print(f"dashboard_health={data.get('dashboard', {}).get('health')}")
print(f"blocked={data.get('dashboard', {}).get('blocked')}")
print(f"snapshot={data.get('remote_snapshot', {}).get('snapshot_id')}")
print(f"exported_at={data.get('exported_at')}")

local_remote = WebDavRemote(os.environ["WEBDAV_HTTP_BASE"], settings.username, settings.password, timeout=8, retries=0)
try:
    html = local_remote.get_bytes(os.environ["WEBDAV_HTTP_PATH"])
except RemoteError as exc:
    print("webdav_http_ok=false")
    print(f"webdav_http_error={exc}")
else:
    print(f"webdav_http_ok={str(b'Skill Sync Observer' in html).lower()}")
    print(f"webdav_http_url={os.environ['WEBDAV_HTTP_BASE'].rstrip('/')}/{os.environ['WEBDAV_HTTP_PATH'].lstrip('/')}")
    print(f"webdav_http_bytes={len(html)}")
PY

if command -v curl >/dev/null 2>&1; then
  http_url="${http_base%/}$http_path"
  tmp_file="$(mktemp)"
  trap 'rm -f "$tmp_file"' EXIT
  status="$(curl -sS -L --max-time 8 -o "$tmp_file" -w '%{http_code}' "$http_url" || true)"
  bytes="$(wc -c < "$tmp_file" | tr -d ' ')"
  if grep -q "Skill Sync Observer" "$tmp_file"; then
    echo "http_static_ok=true"
  else
    echo "http_static_ok=false"
  fi
  echo "http_url=$http_url"
  echo "http_status=$status"
  echo "http_bytes=$bytes"
fi
