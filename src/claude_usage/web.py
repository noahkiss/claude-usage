"""HTTP server and embedded web dashboard for Claude usage tracking."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from claude_usage.aggregator import aggregate_usage, get_last_api_snapshot
from claude_usage.calibrator import compute_ratio
from claude_usage.db import TrackerDB
from claude_usage.scanner import backfill_conversation_boundaries
from claude_usage.history import (
    get_extra_usage_periods,
    get_plan_transitions,
    get_utilization_history,
)

log = logging.getLogger(__name__)

HISTORY_FILE = Path.home() / ".local" / "state" / "claude-usage" / "history.jsonl"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _window_params(qs: dict) -> tuple[str | None, str | None]:
    """Parse window=5h|7d from query string into (since, until) ISO strings."""
    now = _now_utc()
    window = qs.get("window", ["5h"])[0]
    if window == "7d":
        since = _iso(now - timedelta(days=7))
    else:
        since = _iso(now - timedelta(hours=5))
    return since, _iso(now)


def _make_handler(db: TrackerDB):
    """Create a request handler class with access to the DB."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            log.debug(format, *args)

        def _json_response(self, data: object, status: int = 200) -> None:
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _html_response(self, html: str) -> None:
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = parse_qs(parsed.query)

            routes = {
                "/": self._route_index,
                "/api/status": self._route_status,
                "/api/projects": self._route_projects,
                "/api/daily": self._route_daily,
                "/api/models": self._route_models,
                "/api/sessions": self._route_sessions,
                "/api/history": self._route_history,
                "/api/extra-usage": self._route_extra_usage,
                "/api/plan": self._route_plan,
            }

            handler = routes.get(path)
            if handler:
                try:
                    handler(qs)
                except Exception:
                    log.exception("Error handling %s", path)
                    self._json_response({"error": "internal server error"}, 500)
            else:
                self._json_response({"error": "not found"}, 404)

        def _route_index(self, qs: dict) -> None:
            self._html_response(DASHBOARD_HTML)

        def _route_status(self, qs: dict) -> None:
            api_snapshot = get_last_api_snapshot()
            report = aggregate_usage(db, api_snapshot)

            # Pacing calculation
            pacing = _compute_pacing(db, report)

            # Sub-windows and extra usage from API snapshot
            sub_windows = {}
            extra_usage = None
            if api_snapshot:
                for key in ("seven_day_sonnet", "seven_day_opus", "seven_day_cowork"):
                    val = api_snapshot.get(key)
                    if isinstance(val, dict) and val.get("utilization") is not None:
                        sub_windows[key] = {
                            "utilization": val["utilization"],
                            "resets_at": val.get("resets_at"),
                        }
                eu = api_snapshot.get("extra_usage")
                if isinstance(eu, dict):
                    extra_usage = {
                        "is_enabled": eu.get("is_enabled", False),
                        "monthly_limit": eu.get("monthly_limit"),
                        "used_credits": eu.get("used_credits"),
                        "utilization": eu.get("utilization"),
                    }

            self._json_response({
                "generated_at": report["generated_at"],
                "five_hour": {
                    "estimated_utilization": report["five_hour"].get("estimated_utilization"),
                    "api_utilization": report["five_hour"].get("api_utilization"),
                    "tokens": report["five_hour"]["tokens"],
                    "resets_at": report["five_hour"].get("api_resets_at"),
                    "window": report["five_hour"]["window"],
                },
                "seven_day": {
                    "estimated_utilization": report["seven_day"].get("estimated_utilization"),
                    "api_utilization": report["seven_day"].get("api_utilization"),
                    "tokens": report["seven_day"]["tokens"],
                    "resets_at": report["seven_day"].get("api_resets_at"),
                    "window": report["seven_day"]["window"],
                },
                "sub_windows": sub_windows,
                "extra_usage": extra_usage,
                "api_fetched_at": report.get("api_fetched_at"),
                "pacing": pacing,
            })

        def _route_projects(self, qs: dict) -> None:
            since, until = _window_params(qs)
            self._json_response(db.get_project_breakdown(since, until))

        def _route_daily(self, qs: dict) -> None:
            days = int(qs.get("days", ["30"])[0])
            self._json_response(db.get_daily_totals(min(days, 90)))

        def _route_models(self, qs: dict) -> None:
            since, until = _window_params(qs)
            raw = db.get_model_breakdown(since, until)
            # Convert to list format for easier frontend consumption
            grand_total = sum(v["total_tokens"] for v in raw.values()) or 1
            result = [
                {
                    "model": model,
                    "total_tokens": data["total_tokens"],
                    "input_tokens": data["input_tokens"],
                    "output_tokens": data["output_tokens"],
                    "pct": round(data["total_tokens"] / grand_total * 100, 1),
                }
                for model, data in sorted(raw.items(), key=lambda x: -x[1]["total_tokens"])
            ]
            self._json_response(result)

        def _route_sessions(self, qs: dict) -> None:
            limit = int(qs.get("limit", ["50"])[0])
            self._json_response(db.get_sessions_with_project(min(limit, 200)))

        def _route_history(self, qs: dict) -> None:
            hours = int(qs.get("hours", ["168"])[0])
            self._json_response(get_utilization_history(min(hours, 720)))

        def _route_extra_usage(self, qs: dict) -> None:
            self._json_response(get_extra_usage_periods())

        def _route_plan(self, qs: dict) -> None:
            transitions = get_plan_transitions()
            # Check current plan from latest API data
            api = get_last_api_snapshot()
            current_tier = None
            if api:
                has_opus = api.get("seven_day_opus") is not None
                has_extra = isinstance(api.get("extra_usage"), dict)
                current_tier = "max" if (has_opus or has_extra) else "pro"
            self._json_response({
                "current_tier": current_tier,
                "transitions": transitions,
            })

    return Handler


def _compute_pacing(db: TrackerDB, report: dict) -> dict:
    """Compute projected utilization at window reset."""
    pacing = {}
    for window_key, cal_window in [("five_hour", "5h"), ("seven_day", "7d")]:
        bucket = report[window_key]
        est = bucket.get("estimated_utilization") or bucket.get("api_utilization")
        resets_at = bucket.get("api_resets_at")
        if est is None or resets_at is None:
            continue

        try:
            reset_dt = datetime.fromisoformat(resets_at)
            now = _now_utc()
            hours_remaining = max(0, (reset_dt - now).total_seconds() / 3600)
        except (ValueError, TypeError):
            continue

        # Get tokens from last hour for rate
        one_hour_ago = _iso(now - timedelta(hours=1))
        hourly = db.get_hourly_totals(one_hour_ago)
        tokens_last_hour = sum(h["total_tokens"] for h in hourly)

        # Get calibration ratio
        cal = compute_ratio(db, cal_window)
        if cal and tokens_last_hour > 0:
            rate_per_hour = tokens_last_hour * cal.ratio
            projected = est + (rate_per_hour * hours_remaining)
            pacing[cal_window] = {
                "current": round(est, 1),
                "projected": round(min(100.0, projected), 1),
                "hours_remaining": round(hours_remaining, 1),
                "tokens_per_hour": tokens_last_hour,
            }

    return pacing


def start_background_refresh(db: TrackerDB, interval: int = 60) -> threading.Event:
    """Periodically refresh session_projects mapping."""
    stop_event = threading.Event()

    def _loop():
        while not stop_event.is_set():
            try:
                db.refresh_session_projects()
            except Exception:
                log.exception("Error refreshing session_projects")
            stop_event.wait(interval)

    t = threading.Thread(target=_loop, daemon=True, name="session-refresh")
    t.start()
    return stop_event


def run_server(
    db: TrackerDB,
    host: str = "0.0.0.0",
    port: int = 2725,
) -> None:
    """Start the web dashboard server."""
    # Initial refresh
    db.refresh_session_projects()
    backfill_conversation_boundaries(db)

    # Background refresh
    stop_event = start_background_refresh(db)

    handler = _make_handler(db)
    server = ThreadingHTTPServer((host, port), handler)
    log.info("Dashboard server listening on http://%s:%d", host, port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()
        log.info("Dashboard server stopped")


# ---------------------------------------------------------------------------
# Embedded HTML dashboard
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Usage</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
:root {
  --base: #1e1e2e;
  --mantle: #181825;
  --crust: #11111b;
  --surface0: #313244;
  --surface1: #45475a;
  --surface2: #585b70;
  --overlay0: #6c7086;
  --overlay1: #7f849c;
  --text: #cdd6f4;
  --subtext0: #a6adc8;
  --subtext1: #bac2de;
  --blue: #89b4fa;
  --green: #a6e3a1;
  --yellow: #f9e2af;
  --red: #f38ba8;
  --peach: #fab387;
  --mauve: #cba6f7;
  --teal: #94e2d5;
  --pink: #f5c2e7;
  --sapphire: #74c7ec;
  --lavender: #b4befe;
  --flamingo: #f2cdcd;
  --rosewater: #f5e0dc;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  background: var(--base);
  color: var(--text);
  line-height: 1.5;
  padding: 1.5rem;
  max-width: 1200px;
  margin: 0 auto;
}
h1 { font-size: 1.4rem; font-weight: 600; }
h2 { font-size: 1rem; font-weight: 600; color: var(--subtext1); margin-bottom: 0.75rem; }
a { color: var(--blue); text-decoration: none; }

/* Header */
.header {
  display: flex;
  align-items: center;
  gap: 1rem;
  margin-bottom: 1.5rem;
  padding-bottom: 1rem;
  border-bottom: 1px solid var(--surface0);
}
.header .meta {
  margin-left: auto;
  font-size: 0.8rem;
  color: var(--overlay1);
  text-align: right;
}
.badge {
  display: inline-block;
  padding: 0.15rem 0.5rem;
  border-radius: 4px;
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  background: var(--surface0);
  color: var(--mauve);
}

/* Cards grid */
.cards {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
  margin-bottom: 1.5rem;
}
.card {
  background: var(--mantle);
  border: 1px solid var(--surface0);
  border-radius: 8px;
  padding: 1.25rem;
}
.card-label {
  font-size: 0.8rem;
  color: var(--overlay1);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 0.25rem;
}
.card-value {
  font-size: 2.5rem;
  font-weight: 700;
  line-height: 1.1;
}
.card-sub {
  font-size: 0.8rem;
  color: var(--overlay1);
  margin-top: 0.25rem;
}
.card-value.green { color: var(--green); }
.card-value.yellow { color: var(--yellow); }
.card-value.red { color: var(--red); }

/* Pacing strip */
.pacing {
  background: var(--mantle);
  border: 1px solid var(--surface0);
  border-radius: 8px;
  padding: 0.75rem 1.25rem;
  margin-bottom: 1.5rem;
  font-size: 0.85rem;
  color: var(--subtext0);
  display: flex;
  gap: 2rem;
}
.pacing span { color: var(--text); font-weight: 600; }

/* Stat row */
.stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 1rem;
  margin-bottom: 1.5rem;
}
.stat {
  background: var(--mantle);
  border: 1px solid var(--surface0);
  border-radius: 8px;
  padding: 0.75rem 1rem;
}
.stat-label { font-size: 0.7rem; color: var(--overlay1); text-transform: uppercase; letter-spacing: 0.05em; }
.stat-value { font-size: 1.1rem; font-weight: 600; margin-top: 0.15rem; }

/* Sections */
.section {
  background: var(--mantle);
  border: 1px solid var(--surface0);
  border-radius: 8px;
  padding: 1.25rem;
  margin-bottom: 1.5rem;
}
.section canvas { max-height: 300px; }

/* Toggle buttons */
.toggle {
  display: inline-flex;
  gap: 0;
  margin-bottom: 0.75rem;
}
.toggle button {
  background: var(--surface0);
  border: 1px solid var(--surface1);
  color: var(--subtext0);
  padding: 0.25rem 0.75rem;
  font-size: 0.75rem;
  cursor: pointer;
  transition: all 0.15s;
}
.toggle button:first-child { border-radius: 4px 0 0 4px; }
.toggle button:last-child { border-radius: 0 4px 4px 0; }
.toggle button.active {
  background: var(--blue);
  color: var(--crust);
  border-color: var(--blue);
}

/* Tables */
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.8rem;
}
th {
  text-align: left;
  padding: 0.5rem;
  border-bottom: 1px solid var(--surface0);
  color: var(--overlay1);
  font-weight: 500;
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
td {
  padding: 0.4rem 0.5rem;
  border-bottom: 1px solid var(--surface0);
}
tr:last-child td { border-bottom: none; }
.mono { font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.75rem; }
.right { text-align: right; }
.muted { color: var(--overlay1); }

/* Bar inline */
.bar-bg {
  background: var(--surface0);
  border-radius: 3px;
  height: 6px;
  width: 100%;
  min-width: 60px;
}
.bar-fill {
  height: 100%;
  border-radius: 3px;
  background: var(--blue);
  transition: width 0.3s;
}

/* Layout helpers */
.row { display: flex; gap: 1rem; margin-bottom: 1.5rem; }
.row > * { flex: 1; }

@media (max-width: 768px) {
  .cards { grid-template-columns: 1fr; }
  .stats { grid-template-columns: repeat(2, 1fr); }
  .row { flex-direction: column; }
}
</style>
</head>
<body>

<div class="header">
  <h1>Claude Usage</h1>
  <span class="badge" id="plan-badge">—</span>
  <div class="meta">
    <div>API: <span id="api-time">—</span></div>
    <div>Updated: <span id="update-time">—</span></div>
  </div>
</div>

<div class="cards">
  <div class="card">
    <div class="card-label">5-Hour Window</div>
    <div class="card-value" id="util-5h">—</div>
    <div class="card-sub" id="util-5h-sub"></div>
  </div>
  <div class="card">
    <div class="card-label">7-Day Window</div>
    <div class="card-value" id="util-7d">—</div>
    <div class="card-sub" id="util-7d-sub"></div>
  </div>
</div>

<div class="cards">
  <div class="card">
    <div class="card-label">5h Pacing</div>
    <div class="card-value" id="pace-5h">—</div>
    <div class="card-sub" id="pace-5h-sub"></div>
  </div>
  <div class="card">
    <div class="card-label">7d Pacing</div>
    <div class="card-value" id="pace-7d">—</div>
    <div class="card-sub" id="pace-7d-sub"></div>
  </div>
</div>

<div class="stats">
  <div class="stat">
    <div class="stat-label">Tokens Today</div>
    <div class="stat-value" id="stat-tokens">—</div>
  </div>
  <div class="stat">
    <div class="stat-label">Activity Today</div>
    <div class="stat-value" id="stat-sessions">—</div>
  </div>
  <div class="stat">
    <div class="stat-label">Top Project (5h)</div>
    <div class="stat-value" id="stat-project">—</div>
  </div>
  <div class="stat">
    <div class="stat-label">Top Model (5h)</div>
    <div class="stat-value" id="stat-model">—</div>
  </div>
</div>

<div class="section">
  <h2>Daily Token Usage (30 days)</h2>
  <canvas id="daily-chart"></canvas>
</div>

<div class="row">
  <div class="section">
    <h2>Project Breakdown</h2>
    <div class="toggle" id="project-toggle">
      <button class="active" data-window="5h">5h</button>
      <button data-window="7d">7d</button>
    </div>
    <canvas id="project-chart" height="200"></canvas>
    <table id="project-table"><tbody></tbody></table>
  </div>
  <div class="section">
    <h2>Model Breakdown</h2>
    <div class="toggle" id="model-toggle">
      <button class="active" data-window="5h">5h</button>
      <button data-window="7d">7d</button>
    </div>
    <canvas id="model-chart" height="200"></canvas>
  </div>
</div>

<div class="section">
  <h2>Recent Sessions</h2>
  <table id="session-table">
    <thead>
      <tr>
        <th>Project</th>
        <th>Session</th>
        <th>Model</th>
        <th class="right">Convos</th>
        <th class="right">Messages</th>
        <th class="right">Tokens</th>
        <th>Last Active</th>
      </tr>
    </thead>
    <tbody></tbody>
  </table>
</div>

<div class="section">
  <h2>Utilization History (7 days)</h2>
  <canvas id="history-chart"></canvas>
</div>

<div class="section" id="sub-limits-section" style="display:none">
  <h2>Per-Model Limits & Extra Usage</h2>
  <table id="sub-limits-table">
    <thead><tr><th>Limit</th><th class="right">Utilization</th><th>Resets</th></tr></thead>
    <tbody></tbody>
  </table>
</div>

<div class="section" id="extra-usage-section" style="display:none">
  <h2>Extra Usage History</h2>
  <table id="extra-usage-table">
    <thead><tr><th>Start</th><th>End</th><th class="right">Monthly Limit</th><th class="right">Used</th></tr></thead>
    <tbody></tbody>
  </table>
</div>

<script>
// --- Helpers ---
const fmt = n => {
  if (n >= 1e9) return (n/1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n/1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n/1e3).toFixed(1) + 'k';
  return String(n);
};
const pct = n => n != null ? n.toFixed(0) + '%' : '—';
const utilColor = n => n == null ? '' : n < 50 ? 'green' : n < 80 ? 'yellow' : 'red';
const shortTime = s => {
  if (!s) return '—';
  const d = new Date(s);
  return d.toLocaleString(undefined, {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
};
const timeAgo = s => {
  if (!s) return '—';
  const sec = (Date.now() - new Date(s).getTime()) / 1000;
  if (sec < 60) return Math.round(sec) + 's ago';
  if (sec < 3600) return Math.round(sec/60) + 'm ago';
  if (sec < 86400) return Math.round(sec/3600) + 'h ago';
  return Math.round(sec/86400) + 'd ago';
};
const countdown = s => {
  if (!s) return '';
  const sec = (new Date(s).getTime() - Date.now()) / 1000;
  if (sec <= 0) return 'resetting...';
  const h = Math.floor(sec/3600);
  const m = Math.floor((sec%3600)/60);
  if (h > 30) { const d = Math.floor(h/24); return d + 'd ' + (h%24) + 'h'; }
  return h > 0 ? h + 'h ' + m + 'm' : m + 'm';
};

const C = {
  blue: '#89b4fa', green: '#a6e3a1', yellow: '#f9e2af', red: '#f38ba8',
  peach: '#fab387', mauve: '#cba6f7', teal: '#94e2d5', pink: '#f5c2e7',
  sapphire: '#74c7ec', lavender: '#b4befe', flamingo: '#f2cdcd', rosewater: '#f5e0dc',
  surface0: '#313244', overlay0: '#6c7086', text: '#cdd6f4', subtext0: '#a6adc8',
};
const MODEL_COLORS = [C.blue, C.mauve, C.peach, C.teal, C.pink, C.sapphire, C.lavender, C.flamingo, C.rosewater];
// Stable color assignment by model family
const MODEL_COLOR_MAP = {
  'opus': C.mauve, 'sonnet': C.blue, 'haiku': C.teal,
};
function modelColor(name) {
  const lower = (name || '').toLowerCase();
  for (const [k, v] of Object.entries(MODEL_COLOR_MAP)) {
    if (lower.includes(k)) return v;
  }
  // Hash to index for unknowns
  let h = 0;
  for (let i = 0; i < lower.length; i++) h = (h * 31 + lower.charCodeAt(i)) & 0xffffff;
  return MODEL_COLORS[h % MODEL_COLORS.length];
}

Chart.defaults.color = C.subtext0;
Chart.defaults.borderColor = C.surface0;
Chart.defaults.font.family = '-apple-system, BlinkMacSystemFont, system-ui, sans-serif';
Chart.defaults.font.size = 11;

// --- Chart instances ---
let dailyChart, projectChart, modelChart, historyChart;

// --- State ---
let projectWindow = '5h', modelWindow = '5h';

// --- API fetchers ---
async function fetchJSON(url) {
  const r = await fetch(url);
  return r.json();
}

// --- Status (5s) ---
async function refreshStatus() {
  const d = await fetchJSON('/api/status');
  // Utilization cards
  for (const [key, elId] of [['five_hour','5h'],['seven_day','7d']]) {
    const b = d[key];
    const est = b.estimated_utilization;
    const api = b.api_utilization;
    const val = est ?? api;
    const el = document.getElementById('util-' + elId);
    el.textContent = pct(val);
    el.className = 'card-value ' + utilColor(val);
    const sub = document.getElementById('util-' + elId + '-sub');
    const parts = [];
    if (api != null && est != null) parts.push('API: ' + pct(api));
    if (b.resets_at) parts.push('Resets in ' + countdown(b.resets_at));
    sub.textContent = parts.join(' · ');
  }
  // Meta
  document.getElementById('api-time').textContent = timeAgo(d.api_fetched_at);
  document.getElementById('update-time').textContent = shortTime(d.generated_at);
  // Pacing cards
  for (const [w, elId] of [['5h','5h'],['7d','7d']]) {
    const p = d.pacing?.[w];
    const el = document.getElementById('pace-' + elId);
    const sub = document.getElementById('pace-' + elId + '-sub');
    if (p) {
      el.textContent = p.projected + '%';
      el.className = 'card-value ' + utilColor(p.projected);
      sub.textContent = p.hours_remaining + 'h left · ' + fmt(p.tokens_per_hour) + ' tok/hr';
    } else {
      el.textContent = '—';
      el.className = 'card-value';
      sub.textContent = 'Insufficient data';
    }
  }

  // Sub-limits table (Sonnet, Opus, Extra Usage) — bottom section
  const limitsSection = document.getElementById('sub-limits-section');
  const limitsRows = [];
  const subLabels = {seven_day_sonnet: 'Sonnet only (7d)', seven_day_opus: 'Opus only (7d)'};
  for (const [key, label] of Object.entries(subLabels)) {
    const sw = d.sub_windows?.[key];
    if (sw) {
      limitsRows.push(`<tr><td>${label}</td><td class="right">${pct(sw.utilization)}</td><td class="muted">${sw.resets_at ? 'Resets in ' + countdown(sw.resets_at) : '—'}</td></tr>`);
    }
  }
  const eu = d.extra_usage;
  if (eu && (eu.is_enabled || (eu.used_credits != null && eu.used_credits > 0))) {
    const status = eu.is_enabled ? 'On' : 'Off';
    const spent = eu.used_credits != null ? '$' + eu.used_credits.toFixed(2) : '—';
    const limit = eu.monthly_limit != null ? '$' + eu.monthly_limit : '—';
    limitsRows.push(`<tr><td>Extra usage (${status})</td><td class="right">${spent} / ${limit}</td><td class="muted">${eu.utilization != null ? eu.utilization + '% of limit' : ''}</td></tr>`);
  }
  if (limitsRows.length) {
    limitsSection.style.display = '';
    document.querySelector('#sub-limits-table tbody').innerHTML = limitsRows.join('');
  } else {
    limitsSection.style.display = 'none';
  }
}

// --- Stats (30s) ---
async function refreshStats() {
  const [projects, models, sessions, daily] = await Promise.all([
    fetchJSON('/api/projects?window=5h'),
    fetchJSON('/api/models?window=5h'),
    fetchJSON('/api/sessions?limit=200'),
    fetchJSON('/api/daily?days=1'),
  ]);
  const today = daily[daily.length - 1];
  document.getElementById('stat-tokens').textContent = today ? fmt(today.total_tokens) : '0';
  // Count sessions and conversations active today
  const todayStr = new Date().toISOString().slice(0, 10);
  const todaySessions = sessions.filter(s => s.last_seen && s.last_seen.startsWith(todayStr));
  const todayConvos = todaySessions.reduce((sum, s) => sum + (s.conversation_count || 1), 0);
  const sessionCount = todaySessions.length;
  const activityText = todayConvos > sessionCount
    ? `${todayConvos} convos / ${sessionCount} sess`
    : `${sessionCount} sess`;
  document.getElementById('stat-sessions').textContent = activityText;
  document.getElementById('stat-project').textContent = projects[0]?.project_name || '—';
  document.getElementById('stat-model').textContent = models[0]?.model?.split('/').pop()?.replace('claude-','') || '—';
}

// --- Daily chart (60s) ---
async function refreshDaily() {
  const data = await fetchJSON('/api/daily?days=30');
  const labels = data.map(d => {
    const dt = new Date(d.date + 'T00:00:00');
    return dt.toLocaleDateString(undefined, {month:'short',day:'numeric'});
  });
  const totals = data.map(d => d.total_tokens);
  const outputs = data.map(d => d.output_tokens);
  const cacheCreation = data.map(d => d.cache_creation_tokens);

  if (dailyChart) {
    dailyChart.data.labels = labels;
    dailyChart.data.datasets[0].data = totals;
    dailyChart.data.datasets[1].data = outputs;
    dailyChart.data.datasets[2].data = cacheCreation;
    dailyChart.update('none');
  } else {
    dailyChart = new Chart(document.getElementById('daily-chart'), {
      type: 'bar',
      data: {
        labels,
        datasets: [
          { label: 'Total', data: totals, backgroundColor: C.blue + '80', borderColor: C.blue, borderWidth: 1 },
          { label: 'Output', data: outputs, backgroundColor: C.peach + '80', borderColor: C.peach, borderWidth: 1 },
          { label: 'Cache Create', data: cacheCreation, backgroundColor: C.teal + '60', borderColor: C.teal, borderWidth: 1 },
        ],
      },
      options: {
        responsive: true,
        plugins: { legend: { position: 'top', labels: { boxWidth: 12 } } },
        scales: {
          y: { ticks: { callback: v => fmt(v) }, grid: { color: C.surface0 } },
          x: { grid: { display: false } },
        },
      },
    });
  }
}

// --- Project breakdown (30s) ---
async function refreshProjects() {
  const data = await fetchJSON('/api/projects?window=' + projectWindow);
  const top = data.slice(0, 10);
  const labels = top.map(d => d.project_name);
  const values = top.map(d => d.total_tokens);

  if (projectChart) {
    projectChart.data.labels = labels;
    projectChart.data.datasets[0].data = values;
    projectChart.update('none');
  } else {
    projectChart = new Chart(document.getElementById('project-chart'), {
      type: 'bar',
      data: {
        labels,
        datasets: [{ data: values, backgroundColor: C.blue + '80', borderColor: C.blue, borderWidth: 1 }],
      },
      options: {
        indexAxis: 'y',
        responsive: true,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { callback: v => fmt(v) }, grid: { color: C.surface0 } },
          y: { grid: { display: false } },
        },
      },
    });
  }
  // Table
  const tbody = document.querySelector('#project-table tbody');
  tbody.innerHTML = top.map(d =>
    `<tr><td>${d.project_name}</td><td class="right mono">${fmt(d.total_tokens)}</td><td class="right">${d.pct}%</td><td><div class="bar-bg"><div class="bar-fill" style="width:${d.pct}%"></div></div></td><td class="right muted">${(d.conversation_count || d.session_count) > d.session_count ? d.conversation_count + ' convos / ' + d.session_count + ' sess' : d.session_count + ' sess'}</td></tr>`
  ).join('');
}

// --- Model breakdown (30s) ---
async function refreshModels() {
  const raw = await fetchJSON('/api/models?window=' + modelWindow);
  const data = raw.filter(d => d.model !== '<synthetic>' && d.model !== 'unknown');
  const labels = data.map(d => (d.model || 'unknown').split('/').pop().replace('claude-',''));
  const values = data.map(d => d.total_tokens);
  const colors = data.map(d => modelColor(d.model || ''));

  if (modelChart) {
    modelChart.data.labels = labels;
    modelChart.data.datasets[0].data = values;
    modelChart.data.datasets[0].backgroundColor = colors;
    modelChart.update('none');
  } else {
    modelChart = new Chart(document.getElementById('model-chart'), {
      type: 'doughnut',
      data: {
        labels,
        datasets: [{ data: values, backgroundColor: colors, borderWidth: 0 }],
      },
      options: {
        responsive: true,
        plugins: {
          legend: { position: 'bottom', labels: { boxWidth: 12, padding: 12 } },
          tooltip: { callbacks: { label: ctx => ctx.label + ': ' + fmt(ctx.raw) + ' (' + data[ctx.dataIndex]?.pct + '%)' } },
        },
      },
    });
  }
}

// --- Sessions (30s) ---
async function refreshSessions() {
  const data = await fetchJSON('/api/sessions?limit=50');
  const tbody = document.querySelector('#session-table tbody');
  tbody.innerHTML = data.map(d => {
    const model = (d.model || '').split('/').pop().replace('claude-','');
    const convos = d.conversation_count || 1;
    return `<tr>
      <td>${d.project_name}</td>
      <td class="mono muted">${d.session_id.slice(0,12)}...</td>
      <td class="muted">${model}</td>
      <td class="right">${convos > 1 ? convos : ''}</td>
      <td class="right">${d.message_count}</td>
      <td class="right mono">${fmt(d.total_tokens)}</td>
      <td class="muted">${shortTime(d.last_seen)}</td>
    </tr>`;
  }).join('');
}

// --- Utilization history (60s) ---
async function refreshHistory() {
  const data = await fetchJSON('/api/history?hours=168');
  if (!data.length) return;
  const labels = data.map(d => {
    const dt = new Date(d.fetched_at);
    return dt.toLocaleDateString(undefined, {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
  });

  if (historyChart) {
    historyChart.data.labels = labels;
    historyChart.data.datasets[0].data = data.map(d => d.util_5h);
    historyChart.data.datasets[1].data = data.map(d => d.util_7d);
    historyChart.update('none');
  } else {
    historyChart = new Chart(document.getElementById('history-chart'), {
      type: 'line',
      data: {
        labels,
        datasets: [
          { label: '5h %', data: data.map(d => d.util_5h), borderColor: C.blue, backgroundColor: C.blue + '20', fill: true, tension: 0.3, pointRadius: 0 },
          { label: '7d %', data: data.map(d => d.util_7d), borderColor: C.mauve, backgroundColor: C.mauve + '20', fill: true, tension: 0.3, pointRadius: 0 },
        ],
      },
      options: {
        responsive: true,
        interaction: { intersect: false, mode: 'index' },
        plugins: { legend: { position: 'top', labels: { boxWidth: 12 } } },
        scales: {
          y: { min: 0, max: 100, ticks: { callback: v => v + '%' }, grid: { color: C.surface0 } },
          x: { grid: { display: false }, ticks: { maxTicksLimit: 12 } },
        },
      },
    });
  }
}

// --- Extra usage (300s) ---
async function refreshExtraUsage() {
  const data = await fetchJSON('/api/extra-usage');
  const sec = document.getElementById('extra-usage-section');
  if (!data.length) { sec.style.display = 'none'; return; }
  sec.style.display = '';
  const tbody = document.querySelector('#extra-usage-table tbody');
  tbody.innerHTML = data.map(d =>
    `<tr><td>${shortTime(d.start)}</td><td>${shortTime(d.end)}</td><td class="right mono">${d.monthly_limit ?? '—'}</td><td class="right mono">${d.used_credits ?? '—'}</td></tr>`
  ).join('');
}

// --- Plan badge (300s) ---
async function refreshPlan() {
  const data = await fetchJSON('/api/plan');
  const badge = document.getElementById('plan-badge');
  badge.textContent = data.current_tier?.toUpperCase() || '—';
  badge.style.color = data.current_tier === 'max' ? C.mauve : C.blue;
}

// --- Toggle wiring ---
function wireToggle(id, getter, setter, refresh) {
  document.getElementById(id).addEventListener('click', e => {
    if (e.target.tagName !== 'BUTTON') return;
    document.querySelectorAll('#' + id + ' button').forEach(b => b.classList.remove('active'));
    e.target.classList.add('active');
    setter(e.target.dataset.window);
    refresh();
  });
}
wireToggle('project-toggle', () => projectWindow, v => { projectWindow = v; }, refreshProjects);
wireToggle('model-toggle', () => modelWindow, v => { modelWindow = v; }, refreshModels);

// --- Refresh loops ---
async function init() {
  await Promise.all([refreshStatus(), refreshStats(), refreshDaily(), refreshProjects(), refreshModels(), refreshSessions(), refreshHistory(), refreshExtraUsage(), refreshPlan()]);
}
init();
setInterval(refreshStatus, 5000);
setInterval(() => { refreshStats(); refreshProjects(); refreshModels(); refreshSessions(); }, 30000);
setInterval(() => { refreshDaily(); refreshHistory(); }, 60000);
setInterval(() => { refreshExtraUsage(); refreshPlan(); }, 300000);
</script>
</body>
</html>
"""
