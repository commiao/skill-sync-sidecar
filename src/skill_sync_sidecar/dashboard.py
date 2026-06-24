from __future__ import annotations

import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, Optional

from .ops_status import build_ops_status
from .scanner import scan_roots


@dataclass(frozen=True)
class DashboardConfig:
    local_root: Path
    remote_snapshot: Path
    base_record: Optional[Path] = None
    state_file: Optional[Path] = None
    blocked_report: Optional[Path] = None
    openclaw_reconcile_report: Optional[Path] = None
    openclaw_reconcile_root: Optional[Path] = None
    allow_new: bool = False
    allow_delete: bool = False
    writer_policy: str = "push-pull"
    peer_status_files: Optional[Dict[str, Path]] = None


def build_dashboard_status(config: DashboardConfig) -> dict:
    status = build_ops_status(
        config.local_root,
        config.remote_snapshot,
        base_record=config.base_record,
        state_file=config.state_file,
        blocked_report=config.blocked_report,
        openclaw_reconcile_report=config.openclaw_reconcile_report,
        openclaw_reconcile_root=config.openclaw_reconcile_root,
        allow_new=config.allow_new,
        allow_delete=config.allow_delete,
        writer_policy=config.writer_policy,
    )
    peers = _load_peer_status_files(config.peer_status_files or {})
    devices = _device_overview(status, peers)
    blocked_items = _blocked_items(status, peers)
    status["dashboard"] = {
        "health": _aggregate_health([status.get("health")] + [device.get("health") for device in devices]),
        "blocked": len(blocked_items),
        "blocked_items": blocked_items,
        "devices": devices,
        "tools": _tool_overview(),
    }
    return status


def _load_peer_status_files(peer_status_files: Dict[str, Path]) -> Dict[str, dict]:
    peers = {}
    for peer_id, path in peer_status_files.items():
        try:
            data = json.loads(path.expanduser().read_text(encoding="utf-8"))
        except Exception as exc:
            peers[peer_id] = {
                "id": peer_id,
                "health": "red",
                "error": str(exc),
            }
            continue
        if isinstance(data, dict):
            peers[peer_id] = data
    return peers


def _device_overview(status: dict, peers: Optional[Dict[str, dict]] = None) -> list[dict]:
    peers = peers or {}
    sync_plan = status.get("sync_plan") if isinstance(status.get("sync_plan"), dict) else {}
    blocked = sync_plan.get("blocked")
    local_overrides = sync_plan.get("local_overrides") if isinstance(sync_plan.get("local_overrides"), dict) else {}
    current_note = "同步正常，无待处理项" if status.get("health") == "green" else "需要查看待处理队列"
    openclaw = peers.get("oc-vps") or peers.get("openclaw")
    openclaw_device = _peer_device(
        "oc-vps",
        "oc-vps / OpenClaw",
        "已接入设备",
        openclaw,
        fallback_policy="pull-only + local-only",
        fallback_note="OpenClaw 已部署 sidecar；本机 dashboard 尚未读取到 peer status 文件",
        fallback_local_policy=["disk-cleanup", "lark-cli-adapter"],
    )
    return [
        {
            "id": "mac",
            "name": "Mac 本机",
            "kind": "当前设备",
            "health": status.get("health", "unknown"),
            "skills": status.get("remote_snapshot", {}).get("total"),
            "blocked": blocked,
            "policy": status.get("writer_policy"),
            "note": current_note,
            "local_policy": local_overrides.get("skills", []),
        },
        openclaw_device,
        {
            "id": "win",
            "name": "Windows",
            "kind": "计划设备",
            "health": "not_configured",
            "skills": None,
            "blocked": None,
            "policy": "未接入",
            "note": "等待安装 sidecar 后纳入同一面板",
            "local_policy": [],
        },
    ]


def _peer_device(
    peer_id: str,
    name: str,
    kind: str,
    status: Optional[dict],
    fallback_policy: str,
    fallback_note: str,
    fallback_local_policy: list[str],
) -> dict:
    if not status:
        return {
            "id": peer_id,
            "name": name,
            "kind": kind,
            "health": "not_connected",
            "skills": None,
            "blocked": None,
            "policy": fallback_policy,
            "note": fallback_note,
            "local_policy": fallback_local_policy,
        }
    sync_plan = status.get("sync_plan") if isinstance(status.get("sync_plan"), dict) else {}
    remote_snapshot = status.get("remote_snapshot") if isinstance(status.get("remote_snapshot"), dict) else {}
    local_overrides = sync_plan.get("local_overrides") if isinstance(sync_plan.get("local_overrides"), dict) else {}
    health = status.get("health", "unknown")
    blocked = sync_plan.get("blocked")
    if status.get("error"):
        note = f"读取 peer status 失败：{status.get('error')}"
    elif health == "green":
        note = "远端同步正常，无待处理项"
    elif health == "yellow":
        note = "远端有待审批或待处理项"
    elif health == "red":
        note = "远端状态异常，需要检查 sidecar"
    else:
        note = "远端状态未知"
    return {
        "id": peer_id,
        "name": name,
        "kind": kind,
        "health": health,
        "skills": remote_snapshot.get("total"),
        "blocked": blocked,
        "policy": sync_plan.get("writer_policy") or status.get("writer_policy") or fallback_policy,
        "note": note,
        "local_policy": local_overrides.get("skills", []),
    }


def _aggregate_health(values: list[Optional[str]]) -> str:
    ranked = {"red": 3, "yellow": 2, "green": 1}
    worst = "green"
    for value in values:
        if value in {"not_configured", "not_connected", None}:
            continue
        if ranked.get(str(value), 0) > ranked.get(worst, 0):
            worst = str(value)
    return worst


def _blocked_items(status: dict, peers: Dict[str, dict]) -> list[dict]:
    items: list[dict] = []
    items.extend(_blocked_report_items("mac", "Mac 本机", status))
    for peer_id, peer_status in peers.items():
        peer_name = "oc-vps / OpenClaw" if peer_id in {"oc-vps", "openclaw"} else peer_id
        items.extend(_blocked_report_items(peer_id, peer_name, peer_status))
    return items


def _blocked_report_items(peer_id: str, peer_name: str, status: dict) -> list[dict]:
    report = status.get("blocked_report") if isinstance(status.get("blocked_report"), dict) else {}
    raw_items = report.get("items") if isinstance(report.get("items"), list) else []
    items = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        copied = dict(item)
        copied["peer_id"] = peer_id
        copied["peer_name"] = peer_name
        items.append(copied)
    return items


def _tool_overview() -> list[dict]:
    home = Path.home()
    roots = [
        ("cc-switch", "cc-switch", [home / ".cc-switch" / "skills"], "主同步目录"),
        ("skillshub", "skillshub", [home / ".skillshub"], "工具技能目录"),
        ("codex", "Codex", [home / ".codex" / "skills", home / ".agents" / "skills"], "Codex 可发现目录"),
        ("cursor", "Cursor", [home / ".cursor" / "skills-cursor"], "Cursor 技能目录"),
        ("claude-code", "Claude Code", [home / ".claude" / "skills"], "Claude Code 技能目录"),
    ]
    tools = []
    for tool_id, name, paths, role in roots:
        installed_paths = [path for path in paths if path.exists()]
        installed = bool(installed_paths)
        count = 0
        risk = {"ok": 0, "warning": 0, "error": 0}
        if installed:
            try:
                for index, path in enumerate(installed_paths):
                    data = scan_roots([f"{tool_id}-{index}={path}"]).to_dict()
                    count += int(data.get("total", 0))
                    by_risk = dict(data.get("by_risk", {}))
                    for key in risk:
                        risk[key] += int(by_risk.get(key, 0))
            except Exception as exc:  # pragma: no cover - inventory should not break dashboard
                tools.append(
                    {
                        "id": tool_id,
                        "name": name,
                        "path": ", ".join(str(path) for path in paths),
                        "role": role,
                        "installed": True,
                        "state": "error",
                        "skills": 0,
                        "risk": {},
                        "note": str(exc),
                    }
                )
                continue
        tools.append(
            {
                "id": tool_id,
                "name": name,
                "path": ", ".join(str(path) for path in paths),
                "role": role,
                "installed": installed,
                "state": "active" if installed else "not_found",
                "skills": count,
                "risk": risk,
                "note": "已检测到目录" if installed else "未检测到目录",
            }
        )
    return tools


def serve_dashboard(host: str, port: int, config: DashboardConfig) -> None:
    status_provider = lambda: build_dashboard_status(config)
    handler = _handler_factory(status_provider)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"skill-sync dashboard: http://{host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _handler_factory(status_provider: Callable[[], dict]):
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "SkillSyncDashboard/0"

        def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
            path = self.path.split("?", 1)[0]
            if path in {"", "/"}:
                self._send(200, "text/html; charset=utf-8", DASHBOARD_HTML.encode("utf-8"))
                return
            if path == "/api/status":
                try:
                    payload = status_provider()
                    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
                    self._send(200, "application/json; charset=utf-8", body)
                except Exception as exc:  # pragma: no cover - defensive server boundary
                    body = json.dumps({"ok": False, "health": "red", "error": str(exc)}, ensure_ascii=False).encode("utf-8")
                    self._send(500, "application/json; charset=utf-8", body)
                return
            if path == "/healthz":
                self._send(200, "application/json; charset=utf-8", b'{"ok":true}\n')
                return
            self._send(404, "text/plain; charset=utf-8", b"not found\n")

        def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib hook signature
            return

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return DashboardHandler


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Skill Sync Sidecar</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --ink: #1d2433;
      --muted: #647084;
      --line: #d8dde6;
      --green: #147d50;
      --yellow: #9a6700;
      --red: #c63232;
      --blue: #2557a7;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: #fff;
    }
    h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 650;
      letter-spacing: 0;
    }
    main {
      max-width: 1180px;
      margin: 0 auto;
      padding: 20px 24px 32px;
    }
    .toolbar {
      display: flex;
      align-items: center;
      gap: 12px;
      color: var(--muted);
      min-width: 0;
    }
    button {
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
      border-radius: 6px;
      padding: 7px 11px;
      font: inherit;
      cursor: pointer;
    }
    button:hover { border-color: #aeb7c6; }
    .status-band {
      display: grid;
      grid-template-columns: minmax(220px, 1.2fr) repeat(4, minmax(120px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .panel, .metric {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      min-width: 0;
    }
    .metric-label {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 6px;
    }
    .metric-value {
      font-size: 22px;
      font-weight: 680;
      letter-spacing: 0;
      overflow-wrap: anywhere;
    }
    .health {
      display: flex;
      align-items: center;
      gap: 10px;
      min-height: 62px;
    }
    .dot {
      width: 14px;
      height: 14px;
      border-radius: 50%;
      background: var(--muted);
      flex: 0 0 auto;
    }
    .green .dot { background: var(--green); }
    .yellow .dot { background: var(--yellow); }
    .red .dot { background: var(--red); }
    .health-title {
      font-size: 24px;
      font-weight: 720;
      text-transform: capitalize;
    }
    .health-subtitle {
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    .grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(320px, .8fr);
      gap: 16px;
    }
    .section-title {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      margin: 18px 0 10px;
    }
    .section-title h2 { margin: 0; }
    .section-help {
      color: var(--muted);
      font-size: 12px;
    }
    .cards {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }
    .device-card, .tool-card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      min-width: 0;
    }
    .card-head {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: flex-start;
      margin-bottom: 10px;
    }
    .card-name {
      font-size: 16px;
      font-weight: 700;
      overflow-wrap: anywhere;
    }
    .card-kind {
      color: var(--muted);
      font-size: 12px;
      margin-top: 2px;
    }
    .card-note {
      color: var(--muted);
      min-height: 38px;
      overflow-wrap: anywhere;
    }
    .card-stats {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-top: 12px;
    }
    .mini-stat {
      border-top: 1px solid var(--line);
      padding-top: 8px;
    }
    .mini-label {
      color: var(--muted);
      font-size: 12px;
    }
    .mini-value {
      font-weight: 700;
      overflow-wrap: anywhere;
    }
    h2 {
      font-size: 14px;
      margin: 0 0 10px;
      letter-spacing: 0;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }
    th, td {
      padding: 9px 8px;
      border-top: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      overflow-wrap: anywhere;
    }
    th {
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
    }
    .empty {
      color: var(--muted);
      padding: 12px 0 2px;
    }
    .stack {
      display: grid;
      gap: 16px;
    }
    .kv {
      display: grid;
      grid-template-columns: 132px minmax(0, 1fr);
      gap: 8px 12px;
      padding-top: 4px;
    }
    .key { color: var(--muted); }
    .value { overflow-wrap: anywhere; }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      border-radius: 999px;
      padding: 2px 8px;
      border: 1px solid var(--line);
      background: #fafbfc;
      color: var(--ink);
      font-size: 12px;
      max-width: 100%;
      overflow-wrap: anywhere;
    }
    .pill.green { border-color: #b8d8c8; color: var(--green); background: #f1faf5; }
    .pill.yellow { border-color: #e8d29c; color: var(--yellow); background: #fff8e6; }
    .pill.red { border-color: #efb8b8; color: var(--red); background: #fff1f1; }
    .mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
    }
    .error {
      color: var(--red);
      background: #fff1f1;
      border: 1px solid #efb8b8;
      border-radius: 8px;
      padding: 12px;
      margin-bottom: 16px;
      display: none;
    }
    @media (max-width: 860px) {
      header { align-items: flex-start; flex-direction: column; }
      .status-band { grid-template-columns: 1fr 1fr; }
      .status-band .panel { grid-column: 1 / -1; }
      .cards { grid-template-columns: 1fr; }
      .grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 560px) {
      main { padding: 14px; }
      .status-band { grid-template-columns: 1fr; }
      .kv { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Skill Sync Sidecar</h1>
    <div class="toolbar">
      <span id="updated">Loading</span>
      <button id="refresh" type="button" title="Refresh status">Refresh</button>
    </div>
  </header>
  <main>
    <div id="error" class="error"></div>
    <section class="status-band">
      <div id="health-card" class="panel health">
        <span class="dot"></span>
        <div>
          <div id="health" class="health-title">Unknown</div>
          <div id="next-action" class="health-subtitle">Waiting for status</div>
        </div>
      </div>
      <div class="metric">
        <div class="metric-label">Blocked</div>
        <div id="blocked" class="metric-value">-</div>
      </div>
      <div class="metric">
        <div class="metric-label">Allowed</div>
        <div id="allowed" class="metric-value">-</div>
      </div>
      <div class="metric">
        <div class="metric-label">Remote Skills</div>
        <div id="remote-total" class="metric-value">-</div>
      </div>
      <div class="metric">
        <div class="metric-label">Cycles</div>
        <div id="cycles" class="metric-value">-</div>
      </div>
    </section>
    <div class="section-title">
      <h2>设备</h2>
      <span class="section-help">区分 Mac、OpenClaw、Windows 的接入状态</span>
    </div>
    <section id="devices" class="cards"></section>
    <div class="section-title">
      <h2>工具</h2>
      <span class="section-help">区分 cc-switch、skillshub、Codex、Cursor、Claude Code 的本机目录</span>
    </div>
    <section id="tools" class="cards"></section>
    <section class="grid">
      <div class="stack">
        <div class="panel">
          <h2>Blocked Queue</h2>
          <div id="blocked-empty" class="empty">No blocked items.</div>
          <table id="blocked-table" hidden>
            <thead><tr><th>Skill</th><th>Status</th><th>Category</th><th>Reason</th></tr></thead>
            <tbody id="blocked-body"></tbody>
          </table>
        </div>
        <div class="panel">
          <h2>Sync Summary</h2>
          <div id="summary" class="kv"></div>
        </div>
      </div>
      <div class="stack">
        <div class="panel">
          <h2>Daemon</h2>
          <div id="daemon" class="kv"></div>
        </div>
        <div class="panel">
          <h2>Peer Local Policy</h2>
          <div id="overrides" class="kv"></div>
        </div>
        <div class="panel">
          <h2>Artifacts</h2>
          <div id="artifacts" class="kv"></div>
        </div>
      </div>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const text = (value) => value === undefined || value === null || value === "" ? "-" : String(value);
    const pretty = (value) => {
      if (value === undefined || value === null) return "-";
      if (typeof value === "object") return JSON.stringify(value);
      return String(value);
    };
    const row = (key, value) => `<div class="key">${key}</div><div class="value mono">${escapeHtml(pretty(value))}</div>`;
    const pill = (label, kind) => `<span class="pill ${kind || ""}">${escapeHtml(text(label))}</span>`;
    const escapeHtml = (value) => String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");

    function nextAction(status) {
      if (status.health === "green") return "No review-required sync work.";
      if (status.health === "yellow") return "Review blocked queue or OpenClaw gate before publishing.";
      if (status.health === "red") return "Fix unreadable artifacts or status errors.";
      return "Status unavailable.";
    }

    function render(status) {
      $("error").style.display = "none";
      const dashboard = status.dashboard || {};
      const health = dashboard.health || status.health || "unknown";
      $("health-card").className = `panel health ${health}`;
      $("health").textContent = health;
      $("next-action").textContent = nextAction({ ...status, health });
      const plan = status.sync_plan || {};
      const snapshot = status.remote_snapshot || {};
      const daemon = status.daemon_state || {};
      const blockedReport = status.blocked_report || {};
      $("blocked").textContent = text(dashboard.blocked ?? plan.blocked ?? blockedReport.total);
      $("allowed").textContent = text(plan.allowed);
      $("remote-total").textContent = text(snapshot.total);
      $("cycles").textContent = text(daemon.cycles_run);
      $("updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
      renderDevices(Array.isArray(dashboard.devices) ? dashboard.devices : []);
      renderTools(Array.isArray(dashboard.tools) ? dashboard.tools : []);

      const blockedItems = Array.isArray(dashboard.blocked_items) ? dashboard.blocked_items : (Array.isArray(blockedReport.items) ? blockedReport.items : []);
      $("blocked-empty").hidden = blockedItems.length > 0;
      $("blocked-table").hidden = blockedItems.length === 0;
      $("blocked-body").innerHTML = blockedItems.map((item) => `
        <tr>
          <td class="mono">${escapeHtml(text(item.peer_name || item.peer_id))} / ${escapeHtml(text(item.skill_id))}</td>
          <td>${escapeHtml(text(item.status_action))}</td>
          <td>${escapeHtml(text(item.category))}</td>
          <td>${escapeHtml(text(item.reason))}</td>
        </tr>
      `).join("");

      $("summary").innerHTML = [
        row("writer_policy", plan.writer_policy || status.writer_policy),
        row("safe_to_apply", plan.safe_to_apply),
        row("sync_summary", plan.summary),
        row("status_summary", plan.status_summary),
      ].join("");
      $("daemon").innerHTML = [
        row("status", daemon.daemon_status || daemon.status),
        row("updated_at", daemon.updated_at),
        row("last_cycle", daemon.last_cycle),
        row("state_file", daemon.path),
      ].join("");
      const localOverrides = plan.local_overrides || {};
      $("overrides").innerHTML = [
        row("total", localOverrides.total),
        row("skills", localOverrides.skills),
      ].join("");
      $("artifacts").innerHTML = [
        row("local_root", status.local_root),
        row("snapshot", snapshot.snapshot_id),
        row("snapshot_path", snapshot.path),
        row("base_record", (status.base_record || {}).path),
        row("blocked_report", blockedReport.path),
      ].join("");
    }

    function deviceKind(health) {
      if (health === "green") return "green";
      if (health === "yellow") return "yellow";
      if (health === "red") return "red";
      return "";
    }

    function renderDevices(devices) {
      $("devices").innerHTML = devices.map((device) => `
        <article class="device-card">
          <div class="card-head">
            <div>
              <div class="card-name">${escapeHtml(device.name)}</div>
              <div class="card-kind">${escapeHtml(device.kind)}</div>
            </div>
            ${pill(device.health, deviceKind(device.health))}
          </div>
          <div class="card-note">${escapeHtml(device.note)}</div>
          <div class="card-stats">
            <div class="mini-stat"><div class="mini-label">技能数</div><div class="mini-value">${escapeHtml(text(device.skills))}</div></div>
            <div class="mini-stat"><div class="mini-label">待处理</div><div class="mini-value">${escapeHtml(text(device.blocked))}</div></div>
            <div class="mini-stat"><div class="mini-label">策略</div><div class="mini-value">${escapeHtml(text(device.policy))}</div></div>
            <div class="mini-stat"><div class="mini-label">本机例外</div><div class="mini-value">${escapeHtml(pretty(device.local_policy || []))}</div></div>
          </div>
        </article>
      `).join("");
    }

    function renderTools(tools) {
      $("tools").innerHTML = tools.map((tool) => `
        <article class="tool-card">
          <div class="card-head">
            <div>
              <div class="card-name">${escapeHtml(tool.name)}</div>
              <div class="card-kind">${escapeHtml(tool.role)}</div>
            </div>
            ${pill(tool.installed ? "detected" : "not found", tool.installed ? "green" : "")}
          </div>
          <div class="card-note mono">${escapeHtml(tool.path)}</div>
          <div class="card-stats">
            <div class="mini-stat"><div class="mini-label">技能数</div><div class="mini-value">${escapeHtml(text(tool.skills))}</div></div>
            <div class="mini-stat"><div class="mini-label">风险</div><div class="mini-value">${escapeHtml(pretty(tool.risk))}</div></div>
          </div>
        </article>
      `).join("");
    }

    async function refresh() {
      try {
        const response = await fetch("/api/status", { cache: "no-store" });
        const status = await response.json();
        if (!response.ok) throw new Error(status.error || `HTTP ${response.status}`);
        render(status);
      } catch (error) {
        $("error").textContent = error.message;
        $("error").style.display = "block";
        $("updated").textContent = "Update failed";
      }
    }

    $("refresh").addEventListener("click", refresh);
    refresh();
    setInterval(refresh, 30000);
  </script>
</body>
</html>
"""
