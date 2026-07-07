from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.request import Request, urlopen


JsonDict = Dict[str, Any]


def fetch_summary(url: str, timeout_seconds: float = 20.0) -> JsonDict:
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - operator-provided URL
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("summary endpoint did not return a JSON object")
    return data


def build_fetch_error_report(exc: Exception) -> JsonDict:
    return {
        "ok": False,
        "record_type": "skill-sync-monitor-report",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "health": "red",
        "dashboard_health": "unknown",
        "blocked": None,
        "snapshot_id": None,
        "canonical_total": None,
        "alerts": [
            {
                "code": "summary_fetch_failed",
                "message": f"Could not read dashboard summary: {exc}",
                "action": "Check the gateway URL, network path, and container logs.",
            }
        ],
        "warnings": [],
        "info": [],
    }


def monitor_once(
    url: str,
    *,
    timeout_seconds: float = 20.0,
    stale_after_seconds: int = 30 * 60,
    min_canonical_total: int = 1,
) -> JsonDict:
    try:
        summary = fetch_summary(url, timeout_seconds=timeout_seconds)
    except Exception as exc:
        return build_fetch_error_report(exc)
    return build_monitor_report(
        summary,
        stale_after_seconds=stale_after_seconds,
        min_canonical_total=min_canonical_total,
    )


def write_monitor_report(report: JsonDict, out_dir: Path) -> JsonDict:
    out_dir = out_dir.expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "last-report.json"
    text_path = out_dir / "last-report.txt"
    events_path = out_dir / "events.jsonl"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    text_path.write_text(render_monitor_report(report) + "\n", encoding="utf-8")
    event = {
        "checked_at": report.get("checked_at"),
        "health": report.get("health"),
        "dashboard_health": report.get("dashboard_health"),
        "snapshot_id": report.get("snapshot_id"),
        "blocked": report.get("blocked"),
        "alerts": len(report.get("alerts") or []),
        "warnings": len(report.get("warnings") or []),
    }
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    return {
        "out_dir": str(out_dir),
        "json": str(json_path),
        "text": str(text_path),
        "events": str(events_path),
    }


def run_monitor_loop(
    url: str,
    out_dir: Path,
    *,
    interval_seconds: float = 300.0,
    timeout_seconds: float = 20.0,
    stale_after_seconds: int = 30 * 60,
    min_canonical_total: int = 1,
    max_iterations: Optional[int] = None,
    print_status: bool = True,
) -> JsonDict:
    iterations = 0
    last_report: JsonDict = {}
    while True:
        report = monitor_once(
            url,
            timeout_seconds=timeout_seconds,
            stale_after_seconds=stale_after_seconds,
            min_canonical_total=min_canonical_total,
        )
        paths = write_monitor_report(report, out_dir)
        report["paths"] = paths
        last_report = report
        iterations += 1
        if print_status:
            print(
                "monitor_iteration={} health={} alerts={} warnings={} snapshot={} out={}".format(
                    iterations,
                    report.get("health"),
                    len(report.get("alerts") or []),
                    len(report.get("warnings") or []),
                    report.get("snapshot_id"),
                    paths.get("json"),
                ),
                flush=True,
            )
        if max_iterations is not None and iterations >= max_iterations:
            return last_report
        time.sleep(max(1.0, interval_seconds))


def build_monitor_report(
    summary: JsonDict,
    *,
    stale_after_seconds: int = 30 * 60,
    min_canonical_total: int = 1,
) -> JsonDict:
    dashboard = summary.get("dashboard") if isinstance(summary.get("dashboard"), dict) else {}
    remote_snapshot = summary.get("remote_snapshot") if isinstance(summary.get("remote_snapshot"), dict) else {}
    devices = dashboard.get("devices") if isinstance(dashboard.get("devices"), list) else []
    device_tools = dashboard.get("device_tools") if isinstance(dashboard.get("device_tools"), list) else []
    blocked_items = dashboard.get("blocked_items") if isinstance(dashboard.get("blocked_items"), list) else []
    summary_cache = summary.get("summary_cache") if isinstance(summary.get("summary_cache"), dict) else {}

    alerts: list[JsonDict] = []
    warnings: list[JsonDict] = []
    info: list[JsonDict] = []

    health = dashboard.get("health") or summary.get("health") or "unknown"
    if health == "red":
        alerts.append(_issue("dashboard_health", "Dashboard health is red.", health=str(health), action="Open the blocked queue and device status sections."))
    elif health == "yellow":
        warnings.append(_issue("dashboard_health", "Dashboard has pending review work.", health=str(health), action="Open the blocked queue; approved local changes may need explicit approved-push."))
    elif health != "green":
        warnings.append(_issue("dashboard_health", "Dashboard health is not green.", health=str(health), action="Refresh dashboard status and inspect device sections."))

    cache_state = summary_cache.get("state")
    if cache_state in {"miss", "empty"}:
        alerts.append(
            _issue(
                "summary_cache",
                "Dashboard summary cache has no usable successful payload.",
                state=cache_state,
                last_error=summary_cache.get("last_error"),
                action="Check gateway logs and WebDAV connectivity; /healthz should remain fast while /api/summary recovers.",
            )
        )
    elif cache_state == "stale":
        warnings.append(
            _issue(
                "summary_cache",
                "Dashboard summary is serving stale cached data.",
                state=cache_state,
                age_seconds=summary_cache.get("age_seconds"),
                last_error=summary_cache.get("last_error"),
                action="Inspect gateway refresh latency and WebDAV/peer-status reads.",
            )
        )

    blocked_count = _int_or_zero(dashboard.get("blocked"))
    if blocked_count:
        warnings.append(_issue("blocked_queue", f"{blocked_count} sync item(s) are waiting for explicit review.", count=blocked_count, action="Review each item and run approved-push/pull only after the producing work has settled."))
    for item in blocked_items:
        if not isinstance(item, dict):
            continue
        severity = _blocked_item_severity(item)
        target = warnings if severity == "warning" else alerts
        target.append(
            _issue(
                "blocked_item",
                f"{item.get('peer_name') or item.get('peer_id') or 'unknown device'} / {item.get('skill_id')} is waiting for review.",
                peer_id=item.get("peer_id"),
                skill_id=item.get("skill_id"),
                status_action=item.get("status_action"),
                category=item.get("category"),
                reason=item.get("reason"),
                recommendation=item.get("recommendation"),
                action=_action_for_blocked_item(item),
            )
        )

    canonical_snapshot = remote_snapshot.get("snapshot_id")
    canonical_total = remote_snapshot.get("total")
    if _int_or_zero(canonical_total) < min_canonical_total:
        alerts.append(_issue("canonical_total", "Canonical snapshot total is below the expected minimum.", total=canonical_total, minimum=min_canonical_total, action="Check WebDAV snapshot generation and gateway cache."))

    active_devices = [device for device in devices if isinstance(device, dict) and device.get("id") in {"gateway", "mac", "oc-vps", "openclaw"}]
    for device in active_devices:
        _inspect_device(device, canonical_snapshot, stale_after_seconds, alerts, warnings)

    for group in device_tools:
        if not isinstance(group, dict):
            continue
        device_id = str(group.get("device_id") or "")
        if device_id not in {"mac", "oc-vps", "openclaw"}:
            continue
        if group.get("reported") is not True:
            warnings.append(
                _issue(
                    "device_tools_missing",
                    f"{group.get('device_name') or device_id} has not reported tools[].",
                    device_id=device_id,
                    action="Publish peer-status v1 from that device agent.",
                )
            )

    if not alerts and not warnings:
        info.append(_issue("all_clear", "Skill Sync is green; no operator action required."))

    report_health = "red" if alerts else "yellow" if warnings else "green"
    return {
        "ok": not alerts,
        "record_type": "skill-sync-monitor-report",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "health": report_health,
        "dashboard_health": health,
        "blocked": blocked_count,
        "snapshot_id": canonical_snapshot,
        "canonical_total": canonical_total,
        "alerts": alerts,
        "warnings": warnings,
        "info": info,
    }


def render_monitor_report(report: JsonDict) -> str:
    lines = [
        "Skill Sync Monitor",
        f"health: {report.get('health')}",
        f"dashboard_health: {report.get('dashboard_health')}",
        f"snapshot: {report.get('snapshot_id')} total={report.get('canonical_total')}",
        f"blocked: {report.get('blocked')}",
        f"alerts: {len(report.get('alerts') or [])}",
        f"warnings: {len(report.get('warnings') or [])}",
    ]
    for title, items in (("Alerts", report.get("alerts") or []), ("Warnings", report.get("warnings") or []), ("Info", report.get("info") or [])):
        if not items:
            continue
        lines.append("")
        lines.append(title)
        for item in items:
            if not isinstance(item, dict):
                continue
            lines.append(f"- [{item.get('code')}] {item.get('message')}")
            for key in ("device_id", "peer_id", "skill_id", "status_action", "category", "reason", "recommendation", "action"):
                if item.get(key):
                    lines.append(f"  {key}: {item.get(key)}")
    return "\n".join(lines)


def _inspect_device(device: JsonDict, canonical_snapshot: Optional[str], stale_after_seconds: int, alerts: list[JsonDict], warnings: list[JsonDict]) -> None:
    device_id = str(device.get("id") or "")
    health = device.get("health")
    if health == "red":
        alerts.append(_issue("device_health", f"{device.get('name') or device_id} health is {health}.", device_id=device_id, action="Check peer-status publisher and sidecar logs for that device."))
    elif health == "yellow":
        warnings.append(_issue("device_health", f"{device.get('name') or device_id} has pending review work.", device_id=device_id, action="Check blocked queue; this usually means pull-only is protecting a local change."))
    elif health not in {"green", "not_configured"}:
        warnings.append(_issue("device_health", f"{device.get('name') or device_id} health is {health}.", device_id=device_id, action="Check peer-status publisher and sidecar logs for that device."))
    freshness = device.get("freshness") if isinstance(device.get("freshness"), dict) else {}
    age_seconds = freshness.get("age_seconds")
    stale = freshness.get("state") == "stale" or (isinstance(age_seconds, int) and age_seconds > stale_after_seconds)
    if stale:
        alerts.append(_issue("device_stale", f"{device.get('name') or device_id} status is stale.", device_id=device_id, age_seconds=age_seconds, action="Run the device peer-status publisher or inspect its scheduler."))
    snapshot_id = device.get("snapshot_id")
    if canonical_snapshot and snapshot_id and snapshot_id != canonical_snapshot:
        alerts.append(
            _issue(
                "snapshot_mismatch",
                f"{device.get('name') or device_id} reports a different snapshot.",
                device_id=device_id,
                device_snapshot=snapshot_id,
                canonical_snapshot=canonical_snapshot,
                action="Run the device sync cycle, adopt base if clean, then publish peer status.",
            )
        )
    if device_id in {"mac", "oc-vps", "openclaw"} and health == "not_configured":
        warnings.append(_issue("device_not_configured", f"{device.get('name') or device_id} is not configured.", device_id=device_id, action="Install and publish peer status for this device."))


def _action_for_blocked_item(item: JsonDict) -> str:
    category = item.get("category")
    status_action = item.get("status_action")
    if category == "writer_policy" and status_action in {"push", "push_new"}:
        return "Review the local device change, then run approved-push for this skill if it should publish."
    if category == "writer_policy" and status_action in {"pull", "pull_new"}:
        return "Review the remote change, then run an explicit approved pull/apply path if it should install."
    if category == "conflict":
        return "Generate a conflict package and resolve local/remote/base manually."
    if category == "new_skill_review":
        return "Review the new skill metadata, targets, and safety before allowing it."
    return "Inspect the item and choose explicit approve, defer, or private override."


def _blocked_item_severity(item: JsonDict) -> str:
    category = item.get("category")
    if category in {"conflict", "delete_review"}:
        return "alert"
    return "warning"


def _issue(code: str, message: str, **details: object) -> JsonDict:
    item: JsonDict = {"code": code, "message": message}
    item.update({key: value for key, value in details.items() if value is not None})
    return item


def _int_or_zero(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
