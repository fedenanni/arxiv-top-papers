"""Tiny stdlib web front-end for browsing the citations database.

Serves a single self-contained HTML page plus two JSON endpoints. No external
dependencies and no build step — pick a month or a year and see the most-cited
papers ranked. Each request opens its own SQLite connection (connections are
not safe to share across threads).
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import db, report

MAX_TOP = 500
DEFAULT_TOP = 50

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>arXiv — top papers</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 15px/1.5 system-ui, sans-serif; margin: 0; padding: 1.5rem;
         max-width: 1100px; margin-inline: auto; }
  h1 { font-size: 1.3rem; margin: 0 0 1rem; }
  .search { display: flex; gap: .5rem; margin-bottom: 1rem; }
  .search input { flex: 1; font: inherit; padding: .5rem .6rem;
                  border: 1px solid #888; border-radius: 4px; }
  .search button { font: inherit; padding: .35rem .7rem; cursor: pointer;
                   border: 1px solid #888; border-radius: 4px;
                   background: transparent; }
  .controls { display: flex; gap: 1rem; align-items: end; flex-wrap: wrap;
              margin-bottom: 1rem; }
  .controls.dimmed { opacity: .4; pointer-events: none; }
  .controls label { display: flex; flex-direction: column; font-size: .8rem;
                    color: #777; gap: .25rem; }
  select, input, .toggle button { font: inherit; padding: .35rem .5rem; }
  .toggle button { border: 1px solid #888; background: transparent;
                   cursor: pointer; }
  .toggle button.active { background: #4060c0; color: #fff; border-color: #4060c0; }
  .toggle button:not(:first-child) { border-left: none; }
  .toggle button:first-child { border-radius: 4px 0 0 4px; }
  .toggle button:last-child { border-radius: 0 4px 4px 0; }
  .cats { display: flex; gap: .75rem; flex-wrap: wrap; align-items: center; }
  .cats label { flex-direction: row; align-items: center; gap: .3rem;
                color: inherit; font-size: .9rem; cursor: pointer; }
  .cats .count { color: #999; font-size: .8rem; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #3333; }
  th { font-size: .8rem; text-transform: uppercase; letter-spacing: .03em; color: #777; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  td.active-metric { font-weight: 600; }
  .authors { color: #888; font-size: .85rem; }
  .authors .more { color: #4060c0; cursor: pointer; white-space: nowrap; }
  .authors .more:hover { text-decoration: underline; }
  .badge { font-size: .7rem; background: #c08040; color: #fff; padding: .05rem .35rem;
           border-radius: 3px; margin-left: .4rem; vertical-align: middle; }
  .muted { color: #999; }
  #status { color: #999; margin: .5rem 0; }
</style>
</head>
<body>
<h1>arXiv — top papers</h1>
<div class="search">
  <input id="q" type="search" placeholder="Search by title or arXiv id / URL…"
         autocomplete="off">
  <button id="q-clear" hidden>Clear</button>
</div>
<div class="controls" id="controls">
  <label>Granularity
    <span class="toggle">
      <button id="g-all" class="active">All time</button><button id="g-year">Year</button><button id="g-month">Month</button><button id="g-recent">Recent</button>
    </span>
  </label>
  <label id="period-wrap">Period
    <select id="period"></select>
  </label>
  <label>Rank by
    <span class="toggle">
      <button id="m-citations" class="active">Citations</button><button id="m-social">Hacker News</button>
    </span>
  </label>
  <label>Top N
    <input id="top" type="number" min="1" max="500" value="50" style="width:6rem">
  </label>
  <label>Categories
    <span id="cats" class="cats"></span>
  </label>
</div>
<div id="status"></div>
<table>
  <thead><tr>
    <th>#</th><th>Cites</th><th>HN pts</th><th>Title</th><th>Category</th><th>Month</th><th>Updated</th>
  </tr></thead>
  <tbody id="rows"></tbody>
</table>
<script>
let granularity = "all";
let metric = "citations";
let periods = {months: [], years: []};
let categories = [];

// Rolling "recent" windows: [API period token, label, days]. The window is
// anchored server-side to the data frontier (newest paper), not today.
const RECENT_WINDOWS = [
  ["w", "Last week", 7],
  ["1m", "Last month", 30],
  ["2m", "Last 2 months", 60],
  ["3m", "Last 3 months", 90],
];
// Default Recent to "last month": HN attention needs a few days to accrue, so
// a 1-week window is sparse; ~a month is the informative sweet spot.
const RECENT_DEFAULT = "1m";

const $ = (id) => document.getElementById(id);

function fmtDate(iso) {
  if (!iso) return "—";
  return iso.slice(0, 10);
}

// Resolve a rolling window (N days) to its [start, end] dates, anchored to the
// data frontier (matches the server). Returns null if the frontier is unknown.
function windowRange(days) {
  if (!periods.latest || !days) return null;
  const end = new Date(periods.latest + "T00:00:00Z");
  const start = new Date(end);
  start.setUTCDate(start.getUTCDate() - days);
  return {start: start.toISOString().slice(0, 10), end: periods.latest};
}

function setGranularity(g) {
  granularity = g;
  $("g-all").classList.toggle("active", g === "all");
  $("g-year").classList.toggle("active", g === "year");
  $("g-month").classList.toggle("active", g === "month");
  $("g-recent").classList.toggle("active", g === "recent");
  $("period-wrap").style.display = g === "all" ? "none" : "";
  // Default the metric to the one that carries signal for this window, but
  // leave it overridable via the toggle below.
  setMetricUI(g === "recent" ? "social" : "citations");
  fillPeriods();
  load();
}

function setMetricUI(m) {
  metric = m;
  $("m-citations").classList.toggle("active", m === "citations");
  $("m-social").classList.toggle("active", m === "social");
}

function setMetric(m) {
  setMetricUI(m);
  refresh();
}

function fillPeriods() {
  if (granularity === "all") return;
  if (granularity === "recent") {
    $("period").innerHTML = RECENT_WINDOWS
      .map(([v, label]) => `<option value="${v}">${label}</option>`).join("");
    $("period").value = RECENT_DEFAULT;
    return;
  }
  const list = granularity === "month" ? periods.months : periods.years;
  $("period").innerHTML = list.map((p) => `<option>${p}</option>`).join("");
}

function fillCategories() {
  $("cats").innerHTML = categories.map((c) =>
    `<label><input type="checkbox" class="cat" value="${c.category}" checked>` +
    `${c.category} <span class="count">(${c.count.toLocaleString()})</span></label>`
  ).join("");
  document.querySelectorAll(".cat").forEach((cb) => { cb.onchange = load; });
}

function selectedCategories() {
  return [...document.querySelectorAll(".cat:checked")].map((cb) => cb.value);
}

async function load() {
  const top = $("top").value || 50;
  const period = granularity === "all" ? "all" : $("period").value;
  if (!period) { $("rows").innerHTML = ""; return; }
  const cats = selectedCategories();
  if (!cats.length) {
    $("rows").innerHTML = "";
    $("status").textContent = "Select at least one category.";
    return;
  }
  $("status").textContent = "Loading…";
  try {
    const url = `/api/rank?period=${encodeURIComponent(period)}&top=${top}` +
                `&cats=${encodeURIComponent(cats.join(","))}` +
                `&metric=${encodeURIComponent(metric)}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error(await res.text());
    const rows = await res.json();
    render(rows);
    const metricLabel = metric === "social" ? "Hacker News" : "citations";
    let periodLabel;
    if (granularity === "all") {
      periodLabel = "all years";
    } else if (granularity === "recent") {
      const win = RECENT_WINDOWS.find(([v]) => v === period) || [, period, 0];
      const range = windowRange(win[2]);
      periodLabel = range
        ? `${win[1]} (${range.start} → ${range.end})`
        : win[1];
    } else {
      periodLabel = period;
    }
    const through = periods.latest ? ` — data through ${periods.latest}` : "";
    $("status").textContent =
      `${rows.length} papers — ${periodLabel} — by ${metricLabel} — ${cats.join(", ")}${through}`;
  } catch (e) {
    $("status").textContent = "Error: " + e.message;
    $("rows").innerHTML = "";
  }
}

function inSearch() {
  return $("q").value.trim() !== "";
}

// Top N and metric apply in both modes; route to whichever view is active.
function refresh() {
  inSearch() ? runSearch() : load();
}

async function runSearch() {
  const q = $("q").value.trim();
  $("q-clear").hidden = !q;
  $("controls").classList.toggle("dimmed", !!q);
  if (!q) { load(); return; }
  const top = $("top").value || 50;
  $("status").textContent = "Searching…";
  try {
    const url = `/api/search?q=${encodeURIComponent(q)}&top=${top}` +
                `&metric=${encodeURIComponent(metric)}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error(await res.text());
    const rows = await res.json();
    render(rows);
    const metricLabel = metric === "social" ? "Hacker News" : "citations";
    $("status").textContent = rows.length
      ? `${rows.length} match${rows.length === 1 ? "" : "es"} for “${q}” — by ${metricLabel}`
      : `No matches for “${q}”.`;
  } catch (e) {
    $("status").textContent = "Error: " + e.message;
    $("rows").innerHTML = "";
  }
}

// Show only the first few authors; long lists (technical reports etc.) get a
// click-to-expand "+N more" so the table stays compact.
const AUTHOR_LIMIT = 3;
function authorsHtml(s) {
  if (!s) return "";
  const list = s.split("; ");
  if (list.length <= AUTHOR_LIMIT) return `<div class="authors">${s}</div>`;
  const short = list.slice(0, AUTHOR_LIMIT).join("; ");
  const rest = list.length - AUTHOR_LIMIT;
  return `<div class="authors">`
    + `<span class="auth-short">${short}; `
    + `<a class="more" data-act="expand">+${rest} more</a></span>`
    + `<span class="auth-full" hidden>${s} `
    + `<a class="more" data-act="collapse">show less</a></span>`
    + `</div>`;
}

function render(rows) {
  // The "provisional" caveat is about citations not having accrued, so it only
  // applies when ranking by citations.
  $("rows").innerHTML = rows.map((r) => {
    const cites = r.citation_count == null
      ? '<span class="muted">—</span>' : r.citation_count;
    // For the Hacker News metric, link the points to the busiest discussion
    // thread (the most-upvoted story behind the score) when we have its id.
    let score;
    if (r.score == null) {
      score = '<span class="muted">—</span>';
    } else if (metric === "social" && r.top_story_id) {
      score = `<a href="https://news.ycombinator.com/item?id=${r.top_story_id}"`
        + ` target="_blank" rel="noopener" title="top HN discussion">${r.score}</a>`;
    } else {
      score = r.score;
    }
    const authors = authorsHtml(r.authors);
    const citeCls = metric === "citations" ? "num active-metric" : "num";
    const scoreCls = metric === "social" ? "num active-metric" : "num";
    return `<tr>
      <td class="num">${r.rank}</td>
      <td class="${citeCls}">${cites}</td>
      <td class="${scoreCls}">${score}</td>
      <td><a href="${r.url}" target="_blank" rel="noopener">${r.title}</a>${authors}</td>
      <td>${r.primary_category || ""}</td>
      <td>${r.month}</td>
      <td>${fmtDate(r.fetched_at)}</td>
    </tr>`;
  }).join("");
}

$("g-all").onclick = () => setGranularity("all");
$("g-year").onclick = () => setGranularity("year");
$("g-month").onclick = () => setGranularity("month");
$("g-recent").onclick = () => setGranularity("recent");
$("m-citations").onclick = () => setMetric("citations");
$("m-social").onclick = () => setMetric("social");
$("period").onchange = load;
$("top").onchange = refresh;

// Expand / collapse long author lists (delegated; #rows is rebuilt each render).
$("rows").addEventListener("click", (e) => {
  const a = e.target.closest("a.more");
  if (!a) return;
  e.preventDefault();
  const wrap = a.closest(".authors");
  const expand = a.dataset.act === "expand";
  wrap.querySelector(".auth-short").hidden = expand;
  wrap.querySelector(".auth-full").hidden = !expand;
});

// Debounced live search; changing the search box overrides the ranking view.
let searchTimer;
$("q").oninput = () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 200);
};
$("q-clear").onclick = () => {
  $("q").value = "";
  runSearch();
  $("q").focus();
};

(async () => {
  [periods, categories] = await Promise.all([
    (await fetch("/api/periods")).json(),
    (await fetch("/api/categories")).json(),
  ]);
  fillCategories();
  setGranularity("all");
})();
</script>
</body>
</html>
"""


def _make_handler(db_path: str):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            parsed = urlparse(self.path)
            path = parsed.path

            if path == "/":
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                return

            if path == "/api/periods":
                conn = db.connect(db_path)
                try:
                    self._json(200, report.available_periods(conn))
                finally:
                    conn.close()
                return

            if path == "/api/categories":
                conn = db.connect(db_path)
                try:
                    self._json(200, report.available_categories(conn))
                finally:
                    conn.close()
                return

            if path == "/api/rank":
                qs = parse_qs(parsed.query)
                period = (qs.get("period") or [""])[0]
                try:
                    top = int((qs.get("top") or [str(DEFAULT_TOP)])[0])
                except ValueError:
                    self._json(400, {"error": "top must be an integer"})
                    return
                top = max(1, min(top, MAX_TOP))
                cats_raw = (qs.get("cats") or [""])[0]
                cats = [c for c in cats_raw.split(",") if c] or None
                metric = (qs.get("metric") or [report.METRIC_CITATIONS])[0]
                if metric not in (report.METRIC_CITATIONS, report.METRIC_SOCIAL):
                    self._json(400, {"error": f"invalid metric: {metric!r}"})
                    return
                conn = db.connect(db_path)
                try:
                    rows = report.rank_period(
                        conn, period=period, top=top, categories=cats,
                        metric=metric,
                    )
                except ValueError as e:
                    self._json(400, {"error": str(e)})
                    return
                finally:
                    conn.close()
                self._json(200, rows)
                return

            if path == "/api/search":
                qs = parse_qs(parsed.query)
                query = (qs.get("q") or [""])[0]
                try:
                    top = int((qs.get("top") or [str(DEFAULT_TOP)])[0])
                except ValueError:
                    self._json(400, {"error": "top must be an integer"})
                    return
                top = max(1, min(top, MAX_TOP))
                metric = (qs.get("metric") or [report.METRIC_CITATIONS])[0]
                if metric not in (report.METRIC_CITATIONS, report.METRIC_SOCIAL):
                    self._json(400, {"error": f"invalid metric: {metric!r}"})
                    return
                conn = db.connect(db_path)
                try:
                    rows = report.search_papers(
                        conn, query=query, top=top, metric=metric,
                    )
                finally:
                    conn.close()
                self._json(200, rows)
                return

            self._json(404, {"error": "not found"})

        def log_message(self, *args) -> None:  # quieter console
            pass

    return Handler


def serve(db_path: str, *, host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the front-end server until interrupted (Ctrl-C)."""
    server = ThreadingHTTPServer((host, port), _make_handler(db_path))
    print(f"serving on http://{host}:{port}  (db: {db_path}, Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        server.server_close()
