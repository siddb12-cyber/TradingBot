"""
analytics/generate_dashboard.py
================================
Generates a self-contained HTML dashboard from all trade and decision logs.

Reads:
  trade_logs/trade_log_YYYY-MM-DD.xlsx       — all trade records
  trade_logs/YYYY-MM-DD/decisions_YYYY-MM-DD.xlsx — all decision journals

Outputs:
  docs/index.html  — GitHub Pages deployable dashboard
  dashboard.html   — local copy in project root

Run manually:   python analytics/generate_dashboard.py
Auto-runs:      Called by trading_engine.py at EOD (15:30 IST)
"""

import json
import os
import math
from datetime import datetime
from pathlib import Path

import pandas as pd

BASE_DIR   = Path(__file__).parent.parent
LOGS_DIR   = BASE_DIR / "trade_logs"
DOCS_DIR   = BASE_DIR / "docs"
DOCS_DIR.mkdir(exist_ok=True)


def _safe(val):
    if val is None: return None
    if isinstance(val, float) and math.isnan(val): return None
    if hasattr(val, 'item'): return val.item()
    return val


def load_all_trades():
    rows = []
    for f in sorted(LOGS_DIR.glob("trade_log_*.xlsx")):
        try:
            df = pd.read_excel(f)
            df["_file"] = f.name
            rows.append(df)
        except Exception:
            pass
    if not rows:
        return pd.DataFrame()
    df = pd.concat(rows, ignore_index=True)
    # Normalise date column
    if "Date" not in df.columns and "_file" in df.columns:
        df["Date"] = df["_file"].str.extract(r"(\d{4}-\d{2}-\d{2})")
    return df


def load_all_decisions():
    rows = []
    for day_dir in sorted(LOGS_DIR.iterdir()):
        if not day_dir.is_dir():
            continue
        for f in day_dir.glob("decisions_*.xlsx"):
            try:
                df = pd.read_excel(f)
                df["_date"] = day_dir.name
                rows.append(df)
            except Exception:
                pass
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def compute_stats(trades):
    if trades.empty:
        return {}
    closed = trades[trades.get("Trade Status", pd.Series(dtype=str)) == "CLOSED"] if "Trade Status" in trades.columns else trades
    total  = len(closed)
    if total == 0:
        return {}
    wins   = len(closed[closed.get("Outcome", pd.Series(dtype=str)).isin(["TARGET 1 HIT","TARGET 2 HIT","TARGET 3 HIT","T1_HIT","T2_HIT","T3_HIT","WIN"])])
    losses = len(closed[closed.get("Outcome", pd.Series(dtype=str)).isin(["SL_HIT","LOSS"])])
    pts_col = "Points Result" if "Points Result" in closed.columns else None
    total_pts = float(closed[pts_col].sum()) if pts_col else 0
    best      = float(closed[pts_col].max()) if pts_col else 0
    worst     = float(closed[pts_col].min()) if pts_col else 0
    win_rate  = round(wins / total * 100, 1) if total else 0
    avg_conf  = float(closed["Confidence Score"].mean()) if "Confidence Score" in closed.columns else 0
    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pts": round(total_pts, 2),
        "best_trade": round(best, 2),
        "worst_trade": round(worst, 2),
        "avg_confidence": round(avg_conf, 1),
    }


def df_to_json(df, cols=None):
    if df.empty:
        return []
    if cols:
        df = df[[c for c in cols if c in df.columns]]
    records = []
    for _, row in df.iterrows():
        records.append({k: _safe(v) for k, v in row.items()})
    return records


def generate():
    print("[Dashboard] Loading trade logs...")
    trades    = load_all_trades()
    decisions = load_all_decisions()
    stats     = compute_stats(trades)

    # Equity curve data
    equity_data = []
    if not trades.empty and "Points Result" in trades.columns and "Date" in trades.columns:
        t = trades.dropna(subset=["Points Result","Date"]).sort_values("Date")
        cumulative = 0
        for _, row in t.iterrows():
            cumulative += float(row["Points Result"])
            equity_data.append({"date": str(row["Date"])[:10], "pts": round(cumulative, 2)})

    # Daily P&L
    daily_pnl = []
    if not trades.empty and "Points Result" in trades.columns and "Date" in trades.columns:
        grp = trades.dropna(subset=["Points Result","Date"]).groupby("Date")["Points Result"].sum().reset_index()
        for _, row in grp.iterrows():
            daily_pnl.append({"date": str(row["Date"])[:10], "pts": round(float(row["Points Result"]), 2)})

    trades_json    = df_to_json(trades)
    decisions_json = df_to_json(decisions)

    html = _build_html(stats, trades_json, decisions_json, equity_data, daily_pnl)

    out1 = DOCS_DIR / "index.html"
    out2 = BASE_DIR / "dashboard.html"
    for out in [out1, out2]:
        out.write_text(html, encoding="utf-8")
        print(f"[Dashboard] Saved: {out}")

    print(f"[Dashboard] Done. Trades: {len(trades_json)} | Decisions: {len(decisions_json)}")
    return out2


def _build_html(stats, trades, decisions, equity, daily_pnl):
    now = datetime.now().strftime("%d %b %Y, %H:%M")
    s = stats
    total   = s.get("total_trades", 0)
    wins    = s.get("wins", 0)
    losses  = s.get("losses", 0)
    wr      = s.get("win_rate", 0)
    tpts    = s.get("total_pts", 0)
    best    = s.get("best_trade", 0)
    worst   = s.get("worst_trade", 0)
    avgconf = s.get("avg_confidence", 0)

    pnl_color = "#22c55e" if tpts >= 0 else "#ef4444"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TradingBot Dashboard — Haus & Kinder</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root {{
    --bg: #0f1117; --surface: #1a1d27; --surface2: #22263a;
    --border: #2d3148; --text: #e8eaf6; --muted: #7986cb;
    --green: #22c55e; --red: #ef4444; --blue: #6366f1;
    --yellow: #f59e0b; --purple: #a855f7;
    --font: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: var(--font); font-size: 14px; min-height: 100vh; }}
  .header {{ background: var(--surface); border-bottom: 1px solid var(--border); padding: 18px 28px; display: flex; align-items: center; justify-content: space-between; }}
  .header h1 {{ font-size: 18px; font-weight: 700; letter-spacing: -0.3px; }}
  .header .meta {{ font-size: 12px; color: var(--muted); }}
  .badge {{ background: #1e3a5f; color: #60a5fa; border-radius: 6px; padding: 3px 10px; font-size: 11px; font-weight: 600; }}
  .main {{ padding: 24px 28px; max-width: 1400px; margin: 0 auto; }}
  .kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px; margin-bottom: 24px; }}
  .kpi {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px; }}
  .kpi .label {{ font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 8px; }}
  .kpi .value {{ font-size: 26px; font-weight: 700; line-height: 1; }}
  .kpi .sub {{ font-size: 11px; color: var(--muted); margin-top: 4px; }}
  .green {{ color: var(--green); }} .red {{ color: var(--red); }} .blue {{ color: var(--blue); }}
  .yellow {{ color: var(--yellow); }} .purple {{ color: var(--purple); }}
  .charts {{ display: grid; grid-template-columns: 2fr 1fr; gap: 16px; margin-bottom: 24px; }}
  .chart-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px; }}
  .chart-card h3 {{ font-size: 13px; font-weight: 600; color: var(--muted); margin-bottom: 16px; text-transform: uppercase; letter-spacing: 0.6px; }}
  .section {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 16px; }}
  .section h3 {{ font-size: 13px; font-weight: 600; color: var(--muted); margin-bottom: 16px; text-transform: uppercase; letter-spacing: 0.6px; display: flex; align-items: center; gap: 8px; }}
  .tabs {{ display: flex; gap: 4px; margin-bottom: 20px; border-bottom: 1px solid var(--border); padding-bottom: 0; }}
  .tab {{ padding: 8px 18px; border-radius: 8px 8px 0 0; cursor: pointer; font-size: 13px; font-weight: 500; color: var(--muted); border: 1px solid transparent; border-bottom: none; transition: all 0.15s; }}
  .tab.active {{ background: var(--surface); border-color: var(--border); color: var(--text); margin-bottom: -1px; }}
  .tab-content {{ display: none; }} .tab-content.active {{ display: block; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  th {{ text-align: left; padding: 8px 12px; color: var(--muted); font-weight: 500; text-transform: uppercase; font-size: 10px; letter-spacing: 0.8px; border-bottom: 1px solid var(--border); white-space: nowrap; }}
  td {{ padding: 10px 12px; border-bottom: 1px solid rgba(45,49,72,0.6); vertical-align: middle; }}
  tr:hover td {{ background: var(--surface2); }}
  .pill {{ display: inline-block; padding: 2px 8px; border-radius: 100px; font-size: 10px; font-weight: 600; }}
  .pill-green {{ background: rgba(34,197,94,0.15); color: var(--green); }}
  .pill-red {{ background: rgba(239,68,68,0.15); color: var(--red); }}
  .pill-blue {{ background: rgba(99,102,241,0.15); color: var(--blue); }}
  .pill-yellow {{ background: rgba(245,158,11,0.15); color: var(--yellow); }}
  .pill-gray {{ background: rgba(121,134,203,0.15); color: var(--muted); }}
  .search {{ background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; padding: 8px 14px; color: var(--text); font-size: 13px; width: 240px; outline: none; }}
  .search:focus {{ border-color: var(--blue); }}
  .section-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }}
  .empty {{ text-align: center; padding: 40px; color: var(--muted); font-size: 13px; }}
  .conf-bar {{ height: 4px; border-radius: 2px; background: var(--surface2); overflow: hidden; width: 80px; display: inline-block; vertical-align: middle; }}
  .conf-fill {{ height: 100%; border-radius: 2px; background: var(--blue); }}
  @media(max-width:768px) {{ .charts{{ grid-template-columns:1fr; }} .main{{ padding:16px; }} }}
</style>
</head>
<body>

<div class="header">
  <div style="display:flex;align-items:center;gap:12px;">
    <div>
      <h1>TradingBot Dashboard</h1>
      <div class="meta">Haus &amp; Kinder · Samara Retail India · Paper Trading</div>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:10px;">
    <span class="badge">PAPER MODE</span>
    <span class="meta">Updated: {now}</span>
  </div>
</div>

<div class="main">

  <!-- KPI CARDS -->
  <div class="kpi-grid">
    <div class="kpi">
      <div class="label">Total Trades</div>
      <div class="value blue">{total}</div>
      <div class="sub">{wins}W / {losses}L</div>
    </div>
    <div class="kpi">
      <div class="label">Win Rate</div>
      <div class="value {'green' if wr >= 55 else 'yellow' if wr >= 40 else 'red'}">{wr}%</div>
      <div class="sub">Target: 55%+</div>
    </div>
    <div class="kpi">
      <div class="label">Total P&amp;L</div>
      <div class="value" style="color:{pnl_color}">{tpts:+.1f} pts</div>
      <div class="sub">Cumulative NIFTY points</div>
    </div>
    <div class="kpi">
      <div class="label">Best Trade</div>
      <div class="value green">{best:+.1f} pts</div>
      <div class="sub">Single trade high</div>
    </div>
    <div class="kpi">
      <div class="label">Worst Trade</div>
      <div class="value red">{worst:+.1f} pts</div>
      <div class="sub">Single trade low</div>
    </div>
    <div class="kpi">
      <div class="label">Avg Confidence</div>
      <div class="value purple">{avgconf:.0f}/100</div>
      <div class="sub">Signal strength avg</div>
    </div>
  </div>

  <!-- CHARTS ROW -->
  <div class="charts">
    <div class="chart-card">
      <h3>Equity Curve — Cumulative NIFTY Points</h3>
      <canvas id="equityChart" height="120"></canvas>
    </div>
    <div class="chart-card">
      <h3>Daily P&amp;L</h3>
      <canvas id="dailyChart" height="120"></canvas>
    </div>
  </div>

  <!-- TABS: TRADES / DECISIONS -->
  <div class="section">
    <div class="tabs">
      <div class="tab active" onclick="switchTab('trades')">Trade Journal ({len(trades)})</div>
      <div class="tab" onclick="switchTab('decisions')">Decision Log ({len(decisions)})</div>
    </div>

    <!-- TRADE TABLE -->
    <div id="tab-trades" class="tab-content active">
      <div class="section-header">
        <div style="font-size:12px;color:var(--muted)">Every trade, every decision recorded.</div>
        <input class="search" id="tradeSearch" oninput="filterTable('tradeTable','tradeSearch')" placeholder="Search trades...">
      </div>
      <div style="overflow-x:auto;">
      <table id="tradeTable">
        <thead><tr>
          <th>Date</th><th>Time</th><th>Signal</th><th>Direction</th>
          <th>Entry</th><th>Exit</th><th>P&L (pts)</th><th>Outcome</th>
          <th>Conf Score</th><th>Conf Level</th><th>MTF</th><th>Lots</th><th>Trade ID</th>
        </tr></thead>
        <tbody id="tradeBody"></tbody>
      </table>
      </div>
    </div>

    <!-- DECISION TABLE -->
    <div id="tab-decisions" class="tab-content">
      <div class="section-header">
        <div style="font-size:12px;color:var(--muted)">Every scan cycle logged — not just trades.</div>
        <input class="search" id="decSearch" oninput="filterTable('decTable','decSearch')" placeholder="Search decisions...">
      </div>
      <div style="overflow-x:auto;">
      <table id="decTable">
        <thead><tr>
          <th>Date</th><th>Time</th><th>Scan#</th><th>Decision</th>
          <th>5m</th><th>15m</th><th>1h</th><th>Price</th><th>vs VWAP</th>
          <th>Base</th><th>OI Adj</th><th>Sent Adj</th><th>Final</th><th>Confidence</th><th>Reason</th>
        </tr></thead>
        <tbody id="decBody"></tbody>
      </table>
      </div>
    </div>
  </div>

</div>

<script>
const TRADES    = {json.dumps(trades)};
const DECISIONS = {json.dumps(decisions)};
const EQUITY    = {json.dumps(equity)};
const DAILY     = {json.dumps(daily_pnl)};

// ---- OUTCOME PILLS ----
function outcomePill(v) {{
  if (!v) return '<span class="pill pill-gray">OPEN</span>';
  v = String(v).toUpperCase();
  if (v.includes('TARGET 3') || v.includes('T3')) return '<span class="pill pill-green">T3 HIT</span>';
  if (v.includes('TARGET 2') || v.includes('T2')) return '<span class="pill pill-green">T2 HIT</span>';
  if (v.includes('TARGET 1') || v.includes('T1')) return '<span class="pill pill-blue">T1 HIT</span>';
  if (v.includes('SL') || v.includes('LOSS'))     return '<span class="pill pill-red">SL HIT</span>';
  if (v.includes('EOD'))                           return '<span class="pill pill-yellow">EOD</span>';
  if (v.includes('WIN'))                           return '<span class="pill pill-green">WIN</span>';
  return `<span class="pill pill-gray">${{v}}</span>`;
}}

function decisionPill(v) {{
  if (!v) return '';
  v = String(v).toUpperCase();
  if (v === 'TRADE SIGNAL')   return '<span class="pill pill-green">SIGNAL</span>';
  if (v === 'ACTIVE TRADE')   return '<span class="pill pill-blue">ACTIVE</span>';
  if (v === 'SIDEWAYS')       return '<span class="pill pill-yellow">SIDEWAYS</span>';
  if (v === 'LOW CONFIDENCE') return '<span class="pill pill-yellow">LOW CONF</span>';
  if (v === 'BLOCKED')        return '<span class="pill pill-red">BLOCKED</span>';
  if (v === 'OCR ERROR')      return '<span class="pill pill-gray">ERROR</span>';
  return `<span class="pill pill-gray">${{v}}</span>`;
}}

function dirPill(v) {{
  if (!v) return '';
  v = String(v).toUpperCase();
  if (v === 'BULLISH') return '<span class="pill pill-green">BULL</span>';
  if (v === 'BEARISH') return '<span class="pill pill-red">BEAR</span>';
  return '<span class="pill pill-gray">SIDE</span>';
}}

function confBar(score) {{
  if (!score) return '';
  const pct = Math.min(100, Math.max(0, score));
  const col = pct >= 70 ? '#22c55e' : pct >= 45 ? '#f59e0b' : '#ef4444';
  return `<div class="conf-bar"><div class="conf-fill" style="width:${{pct}}%;background:${{col}}"></div></div> ${{score}}`;
}}

function pnlCell(v) {{
  if (v === null || v === undefined) return '-';
  const n = parseFloat(v);
  const col = n > 0 ? 'var(--green)' : n < 0 ? 'var(--red)' : 'var(--muted)';
  return `<span style="color:${{col}};font-weight:600">${{n >= 0 ? '+' : ''}}${{n.toFixed(2)}}</span>`;
}}

// ---- RENDER TRADES ----
function renderTrades() {{
  const tbody = document.getElementById('tradeBody');
  if (!TRADES.length) {{ tbody.innerHTML = '<tr><td colspan="13"><div class="empty">No trades yet. Signals will appear here when the market opens.</div></td></tr>'; return; }}
  tbody.innerHTML = TRADES.map(t => `<tr>
    <td>${{t.Date || ''}}</td>
    <td>${{t.Time || ''}}</td>
    <td style="font-weight:600;white-space:nowrap">${{t['Trade Signal'] || ''}}</td>
    <td>${{dirPill(t.Trend || t.direction)}}</td>
    <td>${{t['Current Price'] ? parseFloat(t['Current Price']).toFixed(2) : '-'}}</td>
    <td>${{t['Exit Price'] ? parseFloat(t['Exit Price']).toFixed(2) : '-'}}</td>
    <td>${{pnlCell(t['Points Result'])}}</td>
    <td>${{outcomePill(t.Outcome)}}</td>
    <td>${{confBar(t['Confidence Score'])}}</td>
    <td>${{t['Confidence Level'] || '-'}}</td>
    <td style="font-size:11px">${{t['MTF Alignment'] || ''}}</td>
    <td>${{t.Lots || 1}}</td>
    <td style="font-size:10px;color:var(--muted)">${{(t['Trade ID'] || '').toString().slice(-12)}}</td>
  </tr>`).join('');
}}

// ---- RENDER DECISIONS ----
function renderDecisions() {{
  const tbody = document.getElementById('decBody');
  if (!DECISIONS.length) {{ tbody.innerHTML = '<tr><td colspan="15"><div class="empty">No decisions logged yet.</div></td></tr>'; return; }}
  tbody.innerHTML = DECISIONS.map(d => `<tr>
    <td style="font-size:11px">${{d._date || ''}}</td>
    <td>${{d.Time || ''}}</td>
    <td style="color:var(--muted)">${{d['Scan #'] || ''}}</td>
    <td>${{decisionPill(d.Decision)}}</td>
    <td>${{dirPill(d['5m Dir'])}}</td>
    <td>${{dirPill(d['15m Dir'])}}</td>
    <td>${{dirPill(d['1h Dir'])}}</td>
    <td>${{d['Price 5m'] ? parseFloat(d['Price 5m']).toFixed(2) : '-'}}</td>
    <td>${{d['vs VWAP'] !== null && d['vs VWAP'] !== undefined ? (parseFloat(d['vs VWAP']) >= 0 ? '+' : '') + parseFloat(d['vs VWAP']).toFixed(2) : '-'}}</td>
    <td>${{d['Base Score'] || '-'}}</td>
    <td>${{d['OI Adj'] !== null && d['OI Adj'] !== undefined ? (parseFloat(d['OI Adj']) >= 0 ? '+' : '') + d['OI Adj'] : '-'}}</td>
    <td>${{d['Sent Adj'] !== null && d['Sent Adj'] !== undefined ? (parseFloat(d['Sent Adj']) >= 0 ? '+' : '') + d['Sent Adj'] : '-'}}</td>
    <td style="font-weight:600">${{d['Final Score'] || '-'}}</td>
    <td>${{d.Confidence || '-'}}</td>
    <td style="font-size:11px;color:var(--muted);max-width:220px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${{d.Reason || ''}}">${{d.Reason || ''}}</td>
  </tr>`).join('');
}}

// ---- CHARTS ----
function buildCharts() {{
  // Equity curve
  new Chart(document.getElementById('equityChart'), {{
    type: 'line',
    data: {{
      labels: EQUITY.map(e => e.date),
      datasets: [{{
        label: 'Cumulative P&L (pts)',
        data: EQUITY.map(e => e.pts),
        borderColor: '#6366f1',
        backgroundColor: 'rgba(99,102,241,0.08)',
        borderWidth: 2,
        pointRadius: 4,
        pointBackgroundColor: EQUITY.map(e => e.pts >= 0 ? '#22c55e' : '#ef4444'),
        fill: true, tension: 0.3,
      }}]
    }},
    options: {{
      responsive: true, plugins: {{ legend: {{ display: false }} }},
      scales: {{
        x: {{ grid: {{ color: 'rgba(45,49,72,0.6)' }}, ticks: {{ color: '#7986cb', maxTicksLimit: 8 }} }},
        y: {{ grid: {{ color: 'rgba(45,49,72,0.6)' }}, ticks: {{ color: '#7986cb' }}, beginAtZero: false }}
      }}
    }}
  }});

  // Daily bar
  new Chart(document.getElementById('dailyChart'), {{
    type: 'bar',
    data: {{
      labels: DAILY.map(d => d.date.slice(5)),
      datasets: [{{
        label: 'Daily P&L',
        data: DAILY.map(d => d.pts),
        backgroundColor: DAILY.map(d => d.pts >= 0 ? 'rgba(34,197,94,0.7)' : 'rgba(239,68,68,0.7)'),
        borderRadius: 4,
      }}]
    }},
    options: {{
      responsive: true, plugins: {{ legend: {{ display: false }} }},
      scales: {{
        x: {{ grid: {{ color: 'rgba(45,49,72,0.6)' }}, ticks: {{ color: '#7986cb' }} }},
        y: {{ grid: {{ color: 'rgba(45,49,72,0.6)' }}, ticks: {{ color: '#7986cb' }} }}
      }}
    }}
  }});
}}

// ---- TABS ----
function switchTab(name) {{
  document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', ['trades','decisions'][i] === name));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
}}

// ---- SEARCH FILTER ----
function filterTable(tableId, searchId) {{
  const q = document.getElementById(searchId).value.toLowerCase();
  document.querySelectorAll('#' + tableId + ' tbody tr').forEach(row => {{
    row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none';
  }});
}}

// ---- INIT ----
renderTrades();
renderDecisions();
buildCharts();
</script>
</body>
</html>"""


if __name__ == "__main__":
    generate()
