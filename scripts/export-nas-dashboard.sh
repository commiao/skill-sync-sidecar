#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON:-python3}"
out_dir="${SKILL_SYNC_NAS_DASHBOARD_OUT:-$HOME/public-sync/skill-sync-sidecar-dashboard}"
local_root="${SKILL_SYNC_LOCAL_ROOT:-$HOME/.cc-switch/skills}"
remote_snapshot="${SKILL_SYNC_REMOTE_SNAPSHOT:-$HOME/public-sync/skill-sync-sidecar-dev/current-mac}"
app_dir="$HOME/Library/Application Support/skill-sync-sidecar"
base_record="${SKILL_SYNC_BASE_RECORD:-$app_dir/base-record.json}"
state_file="${SKILL_SYNC_STATE_FILE:-$app_dir/state.json}"
peer_status="${SKILL_SYNC_OPENCLAW_PEER_STATUS:-$app_dir/peers/openclaw-status.json}"
writer_policy="${SKILL_SYNC_WRITER_POLICY:-push-pull}"
allow_new="${SKILL_SYNC_ALLOW_NEW:-1}"
nas_webdav_http_base="${SKILL_SYNC_NAS_WEBDAV_HTTP_BASE:-http://100.123.208.32:5005/public-sync}"
nas_static_http_base="${SKILL_SYNC_NAS_HTTP_BASE:-http://100.123.208.32}"

case "$out_dir" in
  ""|"/"|"$HOME"|"$HOME/"|"$HOME/public-sync"|"$HOME/public-sync/")
    echo "refusing unsafe dashboard output directory: $out_dir" >&2
    exit 2
    ;;
esac

case "$writer_policy" in
  push-pull|pull-only|push-only|no-writes) ;;
  *)
    echo "SKILL_SYNC_WRITER_POLICY must be push-pull, pull-only, push-only, or no-writes" >&2
    exit 2
    ;;
esac

case "$allow_new" in
  0|1) ;;
  *)
    echo "SKILL_SYNC_ALLOW_NEW must be 0 or 1" >&2
    exit 2
    ;;
esac

if [ ! -d "$local_root" ]; then
  echo "local skill root not found: $local_root" >&2
  exit 2
fi

if [ ! -f "$remote_snapshot/index.json" ]; then
  echo "remote snapshot index not found: $remote_snapshot/index.json" >&2
  exit 2
fi

if [ ! -f "$base_record" ]; then
  echo "base record not found: $base_record" >&2
  exit 2
fi

mkdir -p "$out_dir"
tmp_dir="$(mktemp -d "${out_dir}.tmp.XXXXXX")"
trap 'rm -rf "$tmp_dir"' EXIT

cp "$repo_root/examples/nas-dashboard/index.html" "$tmp_dir/index.html"

PYTHONPATH="$repo_root/src" \
LOCAL_ROOT="$local_root" \
REMOTE_SNAPSHOT="$remote_snapshot" \
BASE_RECORD="$base_record" \
STATE_FILE="$state_file" \
PEER_STATUS="$peer_status" \
WRITER_POLICY="$writer_policy" \
ALLOW_NEW="$allow_new" \
OUT_DIR="$tmp_dir" \
NAS_WEBDAV_HTTP_BASE="$nas_webdav_http_base" \
NAS_STATIC_HTTP_BASE="$nas_static_http_base" \
"$python_bin" - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from skill_sync_sidecar.config import load_cc_switch_webdav_settings
from skill_sync_sidecar.dashboard import DashboardConfig, build_dashboard_status

peer = Path(os.environ["PEER_STATUS"]).expanduser()
peer_files = {"openclaw": peer} if peer.exists() else {}
config = DashboardConfig(
    local_root=Path(os.environ["LOCAL_ROOT"]).expanduser(),
    remote_snapshot=Path(os.environ["REMOTE_SNAPSHOT"]).expanduser(),
    base_record=Path(os.environ["BASE_RECORD"]).expanduser(),
    state_file=Path(os.environ["STATE_FILE"]).expanduser(),
    allow_new=os.environ["ALLOW_NEW"] == "1",
    writer_policy=os.environ["WRITER_POLICY"],
    peer_status_files=peer_files,
)
status = build_dashboard_status(config)
status["exported_at"] = datetime.now(timezone.utc).isoformat()
status["export_mode"] = "nas-static-readonly"
status["export_note"] = "Static NAS observer. This artifact is read-only and performs no sync actions."

out = Path(os.environ["OUT_DIR"])
try:
    settings = load_cc_switch_webdav_settings()
    base_url = settings.base_url.rstrip("/")
    access = {
        "webdav_index_url": f"{base_url}/skill-sync-sidecar-dashboard/index.html",
        "webdav_status_url": f"{base_url}/skill-sync-sidecar-dashboard/status.json",
        "nas_webdav_http_index_url": f"{os.environ['NAS_WEBDAV_HTTP_BASE'].rstrip('/')}/skill-sync-sidecar-dashboard/index.html",
        "nas_webdav_http_status_url": f"{os.environ['NAS_WEBDAV_HTTP_BASE'].rstrip('/')}/skill-sync-sidecar-dashboard/status.json",
        "nas_static_http_index_url": f"{os.environ['NAS_STATIC_HTTP_BASE'].rstrip('/')}/skill-sync-sidecar-dashboard/index.html",
        "nas_webdav_http_requires_auth": True,
        "http_static_mapping_required": True,
        "note": "WebDAV URLs require the configured WebDAV account. NAS static HTTP works only after Web Station or nginx maps the dashboard folder.",
    }
except Exception as exc:
    access = {
        "error": str(exc),
        "note": "Could not derive WebDAV access URLs from cc-switch settings.",
    }

(out / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
(out / "devices.json").write_text(json.dumps(status.get("dashboard", {}).get("devices", []), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
(out / "blocked-items.json").write_text(json.dumps(status.get("dashboard", {}).get("blocked_items", []), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
(out / "tools.json").write_text(json.dumps(status.get("dashboard", {}).get("tools", []), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
(out / "access.json").write_text(json.dumps(access, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
(out / "generated-at.txt").write_text(status["exported_at"] + "\n", encoding="utf-8")
PY

cat > "$tmp_dir/README.txt" <<'EOF'
Skill Sync NAS Observer

Open index.html from a NAS static file service or WebDAV web UI.
This directory is read-only status output. It does not apply sync plans,
upload skills, or write into any tool skill root.
EOF

for name in index.html status.json devices.json blocked-items.json tools.json access.json generated-at.txt README.txt; do
  mv "$tmp_dir/$name" "$out_dir/$name"
done

echo "nas_dashboard_export_ok=true"
echo "out_dir=$out_dir"
echo "index=$out_dir/index.html"
echo "status=$out_dir/status.json"
cat "$out_dir/generated-at.txt" | sed 's/^/exported_at=/'
