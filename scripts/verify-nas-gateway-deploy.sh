#!/usr/bin/env bash
set -euo pipefail

nas_host="${SKILL_SYNC_NAS_HOST:-100.123.208.32}"
ssh_user="${SKILL_SYNC_NAS_SSH_USER:-commiao}"
deploy_dir="${SKILL_SYNC_NAS_DEPLOY_DIR:-/volume1/docker/skill-sync-gateway}"
docker_bin="${SKILL_SYNC_NAS_DOCKER_BIN:-/var/packages/ContainerManager/target/usr/bin/docker}"
gateway_url="${SKILL_SYNC_NAS_GATEWAY_URL:-http://${nas_host}:8765}"
portal_url="${SKILL_SYNC_NAS_PORTAL_URL:-http://${nas_host}:17172/portal}"
timeout_seconds="${SKILL_SYNC_NAS_VERIFY_TIMEOUT_SECONDS:-10}"
expected_commit="${1:-${SKILL_SYNC_EXPECTED_COMMIT:-}}"
html_checks="${SKILL_SYNC_NAS_HTML_CHECKS:-普通待审,.simple-action-panel,.simple-action-panel.yellow,管理本机 skill,这里是当前设备的 skill 工作区}"
monitor_check="${SKILL_SYNC_NAS_CHECK_MONITOR:-1}"
monitor_check_required="${SKILL_SYNC_NAS_MONITOR_REPORT_REQUIRED:-1}"
ssh_target="${ssh_user}@${nas_host}"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

echo "nas_host=${nas_host}"
echo "gateway_url=${gateway_url}"
echo "portal_url=${portal_url}"

ssh_common=(
  ssh
  -o BatchMode=yes
  -o ConnectTimeout="${timeout_seconds}"
  "${ssh_target}"
)

if ! "${ssh_common[@]}" "cd '${deploy_dir}' && printf 'deployed_commit=' && cat deployed-commit.txt && printf '\n' && sudo -n '${docker_bin}' ps --format 'container={{.Names}} status={{.Status}}' | grep skill-sync" >"${tmp_dir}/ssh.txt" 2>"${tmp_dir}/ssh.err"; then
  echo "nas_reachable=false"
  sed 's/^/ssh_error=/' "${tmp_dir}/ssh.err"
  exit 2
fi

echo "nas_reachable=true"
cat "${tmp_dir}/ssh.txt"

deployed_commit="$(sed -n 's/^deployed_commit=//p' "${tmp_dir}/ssh.txt" | head -n 1)"
if [ -n "${expected_commit}" ]; then
  if [ "${deployed_commit}" != "${expected_commit}" ]; then
    echo "commit_match=false"
    echo "expected_commit=${expected_commit}"
    exit 3
  fi
  echo "commit_match=true"
fi

health_file="${tmp_dir}/healthz.json"
overview_file="${tmp_dir}/overview.json"
html_file="${tmp_dir}/dashboard.html"
portal_head="${tmp_dir}/portal.head"

curl --noproxy '*' -fsS --max-time "${timeout_seconds}" "${gateway_url%/}/healthz" >"${health_file}"
curl --noproxy '*' -fsS --max-time "${timeout_seconds}" "${gateway_url%/}/api/overview" >"${overview_file}"
curl --noproxy '*' -fsS --max-time "${timeout_seconds}" "${gateway_url%/}/" >"${html_file}"
curl --noproxy '*' -sS -I --max-time "${timeout_seconds}" "${portal_url}" >"${portal_head}" || true

PYTHONPATH="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src" \
HEALTH_FILE="${health_file}" \
OVERVIEW_FILE="${overview_file}" \
HTML_FILE="${html_file}" \
HTML_CHECKS="${html_checks}" \
python3 - <<'PY'
import json
import os
import sys
from pathlib import Path

health = json.loads(Path(os.environ["HEALTH_FILE"]).read_text(encoding="utf-8"))
overview = json.loads(Path(os.environ["OVERVIEW_FILE"]).read_text(encoding="utf-8"))
html = Path(os.environ["HTML_FILE"]).read_text(encoding="utf-8")
dashboard = overview.get("dashboard") or {}
operator = dashboard.get("operator") or {}
checks = [item.strip() for item in os.environ.get("HTML_CHECKS", "").split(",") if item.strip()]
missing = [item for item in checks if item not in html]

print(f"healthz_ok={str(bool(health.get('ok'))).lower()}")
print(f"dashboard_health={dashboard.get('health')}")
print(f"dashboard_blocked={dashboard.get('blocked')}")
print(f"operator_headline={operator.get('headline')}")
print(f"operator_next={operator.get('next_action')}")
print(f"html_checks_ok={str(not missing).lower()}")
if missing:
    print("missing_html_checks=" + ",".join(missing))
    sys.exit(4)
PY

echo "portal_head=$(head -n 1 "${portal_head}" || true)"

if [ "${monitor_check}" != "0" ]; then
  if "${ssh_common[@]}" "sudo -n '${docker_bin}' exec skill-sync-monitor python -c 'import json; d=json.load(open(\"/cache/monitor/last-report.json\")); print(\"monitor_health=%s dashboard=%s alerts=%d warnings=%d info=%d\" % (d.get(\"health\"), d.get(\"dashboard_health\"), len(d.get(\"alerts\") or []), len(d.get(\"warnings\") or []), len(d.get(\"info\") or [])))'" >"${tmp_dir}/monitor.txt" 2>"${tmp_dir}/monitor.err"; then
    cat "${tmp_dir}/monitor.txt"
  else
    echo "monitor_report_ok=false"
    sed 's/^/monitor_error=/' "${tmp_dir}/monitor.err"
    if [ "${monitor_check_required}" = "1" ]; then
      exit 5
    fi
  fi
else
  echo "monitor_report_ok=skipped"
fi
