#!/usr/bin/env bash
set -euo pipefail

url="${SKILL_SYNC_MONITOR_URL:-http://100.123.208.32:8765/api/summary}"
timeout_seconds="${SKILL_SYNC_MONITOR_TIMEOUT_SECONDS:-60}"
summary_file="${SKILL_SYNC_BLOCKED_QUEUE_SUMMARY_FILE:-}"

python3 - "$url" "$timeout_seconds" "$summary_file" <<'PY'
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen

url = sys.argv[1]
timeout_seconds = float(sys.argv[2])
summary_file = sys.argv[3]

if summary_file:
    summary = json.loads(Path(summary_file).expanduser().read_text(encoding="utf-8"))
else:
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - operator-provided URL
        summary = json.loads(response.read().decode("utf-8"))

dashboard = summary.get("dashboard") if isinstance(summary.get("dashboard"), dict) else {}
items = dashboard.get("blocked_items") if isinstance(dashboard.get("blocked_items"), list) else []
snapshot = summary.get("remote_snapshot") if isinstance(summary.get("remote_snapshot"), dict) else {}

print("Skill Sync Blocked Queue")
print(f"dashboard_health: {dashboard.get('health') or summary.get('health') or 'unknown'}")
print(f"snapshot: {snapshot.get('snapshot_id')} total={snapshot.get('total')}")
print(f"blocked: {len(items)}")

if not items:
    print("status: clear")
    raise SystemExit(0)

for index, item in enumerate(items, 1):
    if not isinstance(item, dict):
        continue
    print()
    print(f"{index}. {item.get('peer_name') or item.get('peer_id') or 'unknown'} / {item.get('skill_id')}")
    print(f"   status_action: {item.get('status_action')}")
    print(f"   plan_action: {item.get('plan_action')}")
    print(f"   category: {item.get('category')}")
    print(f"   reason: {item.get('reason')}")
    if item.get("base_hash") or item.get("local_hash") or item.get("remote_hash"):
        print(f"   base_hash: {item.get('base_hash')}")
        print(f"   local_hash: {item.get('local_hash')}")
        print(f"   remote_hash: {item.get('remote_hash')}")
    if item.get("recommendation"):
        print(f"   recommendation: {item.get('recommendation')}")
    if item.get("source"):
        print(f"   source: {item.get('source')}")
PY
