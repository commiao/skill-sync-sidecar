#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/openclaw-conflict-package.sh [SKILL_ID...]

Generate read-only conflict review packages on OpenClaw for selected skills.
This does not write WebDAV, does not change /home/admin/clawd/skills, and does
not restart OpenClaw.

Environment overrides:
  OPENCLAW_SSH_TARGET       default: root@100.79.177.102
  OPENCLAW_CONNECT_TIMEOUT  default: 20
  OPENCLAW_RELEASE          default: peer-status-v1
  OPENCLAW_PYTHON           default: /opt/skill-sync-sidecar/venv-0.1.3/bin/python
  SKILL_SYNC_PREFIX         default: skill-sync-sidecar-dev/current-mac
USAGE
}

skill_ids=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --*)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      skill_ids+=("$1")
      ;;
  esac
  shift
done

OPENCLAW_SSH_TARGET="${OPENCLAW_SSH_TARGET:-root@100.79.177.102}"
OPENCLAW_CONNECT_TIMEOUT="${OPENCLAW_CONNECT_TIMEOUT:-20}"
OPENCLAW_RELEASE="${OPENCLAW_RELEASE:-peer-status-v1}"
OPENCLAW_PYTHON="${OPENCLAW_PYTHON:-/opt/skill-sync-sidecar/venv-0.1.3/bin/python}"
SKILL_SYNC_PREFIX="${SKILL_SYNC_PREFIX:-skill-sync-sidecar-dev/current-mac}"

remote_env=(
  "PYTHONPATH=/opt/skill-sync-sidecar/releases/${OPENCLAW_RELEASE}/src"
)
python_cmd=("${remote_env[@]}" "$OPENCLAW_PYTHON" -m skill_sync_sidecar)
ssh_cmd=(ssh -o BatchMode=yes -o ConnectTimeout="$OPENCLAW_CONNECT_TIMEOUT" "$OPENCLAW_SSH_TARGET")
admin_prefix=(sudo -iu admin env)

run_remote() {
  local quoted=""
  local arg
  for arg in "$@"; do
    printf -v quoted '%s%q ' "$quoted" "$arg"
  done
  "${ssh_cmd[@]}" "${admin_prefix[*]} ${quoted}"
}

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
out_dir="/opt/skill-sync-sidecar/work/current-mac-pullonly/conflicts-${timestamp}"
cache_dir="/opt/skill-sync-sidecar/cache/current-mac-pullonly"
local_result_json="$(mktemp "${TMPDIR:-/tmp}/skill-sync-openclaw-conflicts.XXXXXX")"
trap 'rm -f "$local_result_json"' EXIT

echo "openclaw_conflict_package_mode=read_only"
echo "skills=${skill_ids[*]:-all}"
echo "out=${out_dir}"

run_remote "${python_cmd[@]}" pull-cache \
  --cc-switch-webdav \
  --prefix "$SKILL_SYNC_PREFIX" \
  --out "$cache_dir" \
  --json

run_remote "${python_cmd[@]}" conflict-package \
  --local-root /home/admin/clawd/skills \
  --remote-snapshot "$cache_dir" \
  --last-applied-record /opt/skill-sync-sidecar/state/openclaw-base-record.json \
  --out "$out_dir" \
  --json > "$local_result_json"

run_remote "${remote_env[@]}" "$OPENCLAW_PYTHON" - "$out_dir" "${skill_ids[@]}" <<'PY'
import json
from pathlib import Path
import sys

out_dir = Path(sys.argv[1])
requested = [item for item in sys.argv[2:] if item]
payload = json.loads((out_dir / "conflict-index.json").read_text(encoding="utf-8"))
packages = payload.get("packages", [])
if requested:
    wanted = set(requested)
    packages = [item for item in packages if item.get("skill_id") in wanted]


def summarize_material(path):
    path = Path(path)
    if not path.exists():
        return {"state": "absent", "title": "缺失", "description": "", "file_count": 0, "files": []}
    files = sorted(
        str(item.relative_to(path))
        for item in path.rglob("*")
        if item.is_file() and "__pycache__" not in item.parts
    )
    title = path.name
    description = ""
    skill_md = path / "SKILL.md"
    if skill_md.exists():
        title, description = summarize_skill_md(skill_md, path.name)
    return {
        "state": "present",
        "title": title,
        "description": description,
        "file_count": len(files),
        "files": files[:12],
        "has_more_files": len(files) > 12,
        "skill_md": str(skill_md) if skill_md.exists() else None,
    }


def summarize_skill_md(path, fallback):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return fallback, ""
    metadata = frontmatter(text)
    title = metadata.get("name") or first_heading(text) or fallback
    description = metadata.get("description") or first_paragraph(text)
    return title.strip() or fallback, description.strip()


def frontmatter(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    result = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip() in {"name", "description"}:
            result[key.strip()] = value.strip().strip("\"'")
    return result


def first_heading(text):
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def first_paragraph(text):
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


enriched = []
for package in packages:
    item = dict(package)
    package_path = Path(str(item.get("path") or ""))
    item["review"] = {
        "local_label": "OpenClaw 版",
        "remote_label": "中央仓库版",
        "base_label": "共同基线",
        "local": summarize_material(package_path / "local"),
        "remote": summarize_material(package_path / "remote"),
        "base": summarize_material(package_path / "base"),
        "decision_hint": "先比较 OpenClaw 版和中央仓库版；确定哪边正确后，再选择写入动作。",
    }
    enriched.append(item)
print(json.dumps({
    "ok": True,
    "record_type": "skill-sync-openclaw-conflict-package",
    "mode": "conflict_package",
    "read_only": True,
    "skill_ids": requested,
    "total_conflicts": len(enriched),
    "out": payload.get("out"),
    "packages": enriched,
}, ensure_ascii=False, indent=2))
PY
