#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Install an OpenClaw-local peer-status publisher as a systemd timer.

Usage:
  scripts/install-openclaw-peer-status-systemd.sh [--dry-run|--yes]

This installer only adds/reloads the peer-status service and timer. It does not
restart OpenClaw gateway and does not modify the pull-only or dry-run sync-daemon
units.

Environment:
  OPENCLAW_RELEASE_ROOT      default: /opt/skill-sync-sidecar/releases/peer-status-v1
  OPENCLAW_PUBLISH_INTERVAL default: 300
  OPENCLAW_PYTHON           default: /opt/skill-sync-sidecar/venv-0.1.3/bin/python
EOF
}

mode="--dry-run"
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
elif [[ "${1:-}" == "--yes" || "${1:-}" == "--dry-run" ]]; then
  mode="$1"
elif [[ $# -gt 0 ]]; then
  usage >&2
  exit 2
fi

release_root="${OPENCLAW_RELEASE_ROOT:-/opt/skill-sync-sidecar/releases/peer-status-v1}"
python_bin="${OPENCLAW_PYTHON:-/opt/skill-sync-sidecar/venv-0.1.3/bin/python}"
interval="${OPENCLAW_PUBLISH_INTERVAL:-300}"
service_path="/etc/systemd/system/openclaw-skill-sync-peer-status.service"
timer_path="/etc/systemd/system/openclaw-skill-sync-peer-status.timer"

service_content="[Unit]
Description=Skill Sync OpenClaw Peer Status Publisher
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=admin
Environment=HOME=/home/admin
Environment=PYTHONPATH=${release_root}/src
Environment=OPENCLAW_PYTHON=${python_bin}
ExecStart=${release_root}/scripts/publish-openclaw-local-peer-status.sh
"

timer_content="[Unit]
Description=Run Skill Sync OpenClaw Peer Status Publisher

[Timer]
OnBootSec=30
OnUnitActiveSec=${interval}
Unit=openclaw-skill-sync-peer-status.service

[Install]
WantedBy=timers.target
"

if [[ "$mode" == "--dry-run" ]]; then
  echo "openclaw_peer_status_systemd_mode=dry-run"
  echo "service_path=$service_path"
  echo "timer_path=$timer_path"
  echo "interval=$interval"
  echo "--- service ---"
  printf '%s\n' "$service_content"
  echo "--- timer ---"
  printf '%s\n' "$timer_content"
  exit 0
fi

install -d -m 0755 "$(dirname "$service_path")"
printf '%s\n' "$service_content" > "$service_path"
printf '%s\n' "$timer_content" > "$timer_path"
chmod 0644 "$service_path" "$timer_path"
chmod +x "${release_root}/scripts/publish-openclaw-local-peer-status.sh"

systemctl daemon-reload
systemctl enable --now openclaw-skill-sync-peer-status.timer
systemctl start openclaw-skill-sync-peer-status.service

echo "openclaw_peer_status_systemd_ok=true"
echo "service=$service_path"
echo "timer=$timer_path"
systemctl --no-pager --full status openclaw-skill-sync-peer-status.service | sed -n '1,30p' || true
systemctl --no-pager --full list-timers openclaw-skill-sync-peer-status.timer || true
