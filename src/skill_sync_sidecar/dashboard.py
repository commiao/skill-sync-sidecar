from __future__ import annotations

import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

from .ops_status import build_ops_status


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


def build_dashboard_status(config: DashboardConfig) -> dict:
    return build_ops_status(
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
      const health = status.health || "unknown";
      $("health-card").className = `panel health ${health}`;
      $("health").textContent = health;
      $("next-action").textContent = nextAction(status);
      const plan = status.sync_plan || {};
      const snapshot = status.remote_snapshot || {};
      const daemon = status.daemon_state || {};
      const blockedReport = status.blocked_report || {};
      $("blocked").textContent = text(plan.blocked ?? blockedReport.total);
      $("allowed").textContent = text(plan.allowed);
      $("remote-total").textContent = text(snapshot.total);
      $("cycles").textContent = text(daemon.cycles_run);
      $("updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;

      const blockedItems = Array.isArray(blockedReport.items) ? blockedReport.items : [];
      $("blocked-empty").hidden = blockedItems.length > 0;
      $("blocked-table").hidden = blockedItems.length === 0;
      $("blocked-body").innerHTML = blockedItems.map((item) => `
        <tr>
          <td class="mono">${escapeHtml(text(item.skill_id))}</td>
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
