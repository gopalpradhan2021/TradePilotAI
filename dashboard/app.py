"""
Lightweight status dashboard for the trading bot.
Run: uvicorn dashboard.app:app --host 0.0.0.0 --port 8000
"""
import json
import logging
import os
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timezone

from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from dotenv import load_dotenv
from starlette.middleware.sessions import SessionMiddleware

from config.settings import load_settings
from core.db import optimization_repo, orders_repo, positions_repo, risk_repo
from core.market_hours import market_status_ist
from core.risk_manager import RiskManager
from core.status_writer import read_heartbeat

load_dotenv()

logger = logging.getLogger("groww_agent.dashboard")

app = FastAPI(title="Groww Agent Dashboard")

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY") or secrets.token_hex(32)
if not os.getenv("SESSION_SECRET_KEY"):
    # No TLS in front of this yet, so sessions don't need to be forgery-proof against a
    # network attacker to be a real improvement — but a secret that changes every process
    # restart means every operator gets logged out on each deploy. Warn rather than silently
    # degrade UX; still safe to run without it (a fresh random key per boot), just annoying.
    logger.warning(
        "SESSION_SECRET_KEY not set in .env — using a random key that changes on every "
        "restart, so operators will be logged out each deploy. Set SESSION_SECRET_KEY to a "
        "long random string to avoid that."
    )

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET_KEY,
    max_age=12 * 60 * 60,  # 12h session expiry
    same_site="lax",
    https_only=False,  # no TLS in front of this yet — flip to True once there is
)

# In-memory failed-login throttle: {ip: (fail_count, first_fail_at)}. Resets after
# _THROTTLE_WINDOW_SEC of no attempts. Deliberately not persisted — a process restart
# clearing it is an acceptable tradeoff for a single-operator dashboard with no DB migration
# needed for something this small.
_failed_logins: dict[str, tuple[int, float]] = {}
_THROTTLE_MAX_ATTEMPTS = 5
_THROTTLE_WINDOW_SEC = 300


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _is_throttled(ip: str) -> bool:
    count, first_fail = _failed_logins.get(ip, (0, 0.0))
    if time.time() - first_fail > _THROTTLE_WINDOW_SEC:
        return False
    return count >= _THROTTLE_MAX_ATTEMPTS


def _record_failed_login(ip: str):
    count, first_fail = _failed_logins.get(ip, (0, 0.0))
    now = time.time()
    if now - first_fail > _THROTTLE_WINDOW_SEC:
        count, first_fail = 0, now
    _failed_logins[ip] = (count + 1, first_fail)


def _clear_failed_logins(ip: str):
    _failed_logins.pop(ip, None)


def _is_authenticated(request: Request) -> bool:
    if not DASHBOARD_PASSWORD:
        return True
    return bool(request.session.get("authenticated"))


def _require_page_auth(request: Request) -> RedirectResponse | None:
    """For HTML page routes — callers do `if (r := _require_page_auth(request)): return r`
    so an unauthenticated visitor gets redirected to a real login page, not a raw 401."""
    if not _is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return None


def _require_api_auth(request: Request):
    """For JSON/API routes called via fetch() — a redirect would just be a confusing JSON
    parse error client-side, so this 401s instead."""
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Not logged in")


def _risk_manager() -> RiskManager:
    """Builds a RiskManager the same way scripts/halt_bot.py and scripts/resume_bot.py
    do — it writes straight to the shared daily_summary row, which the running bot's
    Orchestrator re-reads every cycle via RiskManager.refresh_halt_state(), so the
    dashboard needs no direct connection to the bot process."""
    settings = load_settings()
    return RiskManager(settings.risk, ntfy_topic=settings.ntfy_topic, mode=settings.mode)


def _render_login_page(error: str | None = None) -> str:
    error_html = f'<p style="color:#e74c3c; font-size:0.9rem;">{error}</p>' if error else ""
    return f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Login — Groww Agent Dashboard</title>
    <style>
        body {{ font-family: -apple-system, sans-serif; background: #0d1117; color: #e6edf3;
                max-width: 360px; margin: 100px auto; padding: 0 20px; }}
        .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 24px; }}
        h1 {{ font-size: 1.2rem; margin-top: 0; }}
        label {{ display: block; margin-top: 10px; color: #8b949e; font-size: 0.85rem; }}
        input {{ background: #0d1117; color: #e6edf3; border: 1px solid #30363d; border-radius: 4px;
                  padding: 8px; margin-top: 4px; width: 100%; box-sizing: border-box; }}
        button {{ margin-top: 16px; padding: 8px 20px; border-radius: 6px; border: none; width: 100%;
                   background: #58a6ff; color: #0d1117; font-weight: 600; cursor: pointer; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>Groww Agent Dashboard</h1>
        {error_html}
        <form method="post" action="/login">
            <label>Password
                <input type="password" name="password" autofocus>
            </label>
            <button type="submit">Log in</button>
        </form>
    </div>
</body>
</html>
"""


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return _render_login_page()


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, password: str = Form(...)):
    ip = _client_ip(request)
    if _is_throttled(ip):
        return _render_login_page(
            error=f"Too many failed attempts. Try again in a few minutes."
        )
    if not DASHBOARD_PASSWORD or not secrets.compare_digest(password, DASHBOARD_PASSWORD):
        _record_failed_login(ip)
        return _render_login_page(error="Incorrect password.")
    _clear_failed_logins(ip)
    request.session["authenticated"] = True
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def _run_backtest_subprocess(symbols: list[str], start: str, end: str, interval: str) -> dict:
    """Runs scripts/backtest.py as a SEPARATE process rather than importing and calling its
    logic in-process. This is deliberate: run_backtest() (core/backtest_engine.py) points the
    process at a scratch DATABASE_PATH by mutating os.environ — safe for a short-lived CLI
    process, but the dashboard is a long-running server handling concurrent requests against the
    REAL database on every other endpoint, so mutating that env var in-process here would risk a
    concurrent /api/status or /api/bot/stop call reading/writing the wrong database."""
    fd, out_path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        cmd = [
            sys.executable, "-m", "scripts.backtest",
            "--symbols", *symbols, "--start", start, "--end", end,
            "--interval", interval, "--out", out_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            tail = (result.stdout + result.stderr).strip().splitlines()
            raise RuntimeError("\n".join(tail[-15:]) or "backtest process failed with no output")
        with open(out_path) as f:
            return json.load(f)
    finally:
        if os.path.exists(out_path):
            os.remove(out_path)


def _render(status: dict) -> str:
    updated = status.get("updated_at") or "never"
    if updated != "never":
        try:
            dt = datetime.fromisoformat(updated)
            age_sec = (datetime.now(timezone.utc) - dt).total_seconds()
            stale = age_sec > 60
        except Exception:
            stale = False
    else:
        stale = True

    halted = status.get("halted", False)
    halt_source = status.get("halt_source", "AUTO")
    status_color = "#e74c3c" if halted else ("#f39c12" if stale else "#2ecc71")
    status_text = "HALTED" if halted else ("STALE — bot may be stopped" if stale else "RUNNING")
    pnl_color = "#e74c3c" if status.get("realized_pnl_today", 0) < 0 else "#2ecc71"

    if halted and halt_source == "MANUAL":
        switch_button = '<button id="switch-btn" class="switch-btn resume" onclick="doStart()">Start</button>'
    elif halted:
        switch_button = ('<button class="switch-btn resume" disabled '
                          'title="Automatic halts (daily loss limit, circuit breaker, '
                          'reconciliation mismatch) can only clear on the next trading day, '
                          'not from here.">Start (auto halt)</button>')
    else:
        switch_button = '<button id="switch-btn" class="switch-btn halt" onclick="doStop()">Stop</button>'

    proc_status = status.get("bot_process_status", "unknown")
    proc_color = {"active": "#2ecc71", "inactive": "#8b949e", "failed": "#e74c3c"}.get(proc_status, "#8b949e")
    proc_badge = f'<span class="proc-badge" style="color:{proc_color};" title="groww-bot.service">process: {proc_status}</span>'

    mkt_state, mkt_label, now_ist = market_status_ist()
    mkt_color = {"OPEN": "#2ecc71", "PRE-OPEN": "#f39c12",
                 "CLOSING": "#f39c12", "CLOSED": "#8b949e"}[mkt_state]
    clock_str = now_ist.strftime("%H:%M:%S")

    ltp_rows = "".join(
        f"<div class='ltp-row'><span>{sym}</span>"
        f"<span class='mono num'>{price if price is not None else '—'}</span></div>"
        for sym, price in status.get("last_ltp", {}).items()
    ) or "<div class='dim'>No symbols yet</div>"

    def _signal_dot_and_label(d: dict) -> tuple[str, str]:
        if not d.get("warmed_up"):
            return "#58a6ff", "WARMUP"
        if d.get("in_position"):
            return "#2ecc71", "IN POSITION"
        if d.get("cooldown_remaining_sec"):
            return "#d29922", "COOLDOWN"
        return "#8b949e", "WATCHING"

    strategy_rows = ""
    for sym, d in status.get("strategy_debug", {}).items():
        if not d:
            strategy_rows += (
                f"<div class='sig-row'><span class='sig-dot' style='background:#8b949e;'></span>"
                f"<span class='sig-symbol'>{sym}</span><span class='dim'>NO DATA</span>"
                f"<span class='dim'>—</span><span></span><span></span></div>"
            )
            continue
        dot_color, label = _signal_dot_and_label(d)
        if not d.get("warmed_up"):
            detail = f"{d.get('prices_collected', 0)}/{d.get('prices_needed', '?')} bars collected"
        elif d.get("in_position"):
            entry = d.get("entry_price")
            detail = f"entry ₹{entry:.2f}" if entry is not None else "—"
        elif d.get("cooldown_remaining_sec"):
            detail = f"{d['cooldown_remaining_sec']}s remaining"
        elif d.get("short_ma") is not None and d.get("long_ma") is not None:
            detail = f"MA {d['short_ma']:.2f}/{d['long_ma']:.2f}"
        else:
            detail = "—"

        gap_pct, min_gap = d.get("gap_pct"), d.get("min_gap_pct")
        if gap_pct is None or label != "WATCHING":
            gap_cell = ""
        else:
            gap_color = "#2ecc71" if gap_pct >= min_gap else "#d29922" if gap_pct >= min_gap * 0.5 else "#8b949e"
            gap_cell = f"<span style='color:{gap_color};'>GAP {gap_pct:.4f}%</span> <span class='dim'>(need {min_gap:.4f}%)</span>"

        rsi, band = d.get("rsi"), d.get("rsi_entry_band")
        if rsi is None or band is None or label != "WATCHING":
            rsi_cell = ""
        else:
            rsi_cell = f"<span class='dim'>RSI {rsi:.1f} [{band[0]}-{band[1]}]</span>"

        strategy_rows += (
            f"<div class='sig-row'><span class='sig-dot' style='background:{dot_color};'></span>"
            f"<span class='sig-symbol'>{sym}</span><span style='color:{dot_color};'>{label}</span>"
            f"<span class='dim'>{detail}</span><span>{gap_cell}</span><span>{rsi_cell}</span></div>"
        )
    strategy_rows = strategy_rows or "<div class='dim'>No symbols yet</div>"

    def _status_chip(order_status: str) -> str:
        fg, bg = {
            "FILLED": ("#2ecc71", "rgba(46,204,113,0.15)"),
            "PENDING": ("#d29922", "rgba(210,153,34,0.15)"),
        }.get(order_status, ("#e74c3c", "rgba(231,76,60,0.15)"))  # BLOCKED/ERROR/other
        return (f"<span style='display:inline-block; padding:2px 9px; border-radius:3px; "
                f"background:{bg}; color:{fg}; font-size:0.72rem; font-weight:700; "
                f"letter-spacing:0.03em;'>{order_status}</span>")

    def _side_cell(side: str) -> str:
        color = "#2ecc71" if side == "BUY" else "#e74c3c" if side == "SELL" else "#e6edf3"
        return f"<span style='color:{color}; font-weight:600;'>{side}</span>"

    order_rows = "".join(
        f"<tr><td class='mono dim'>{o.get('created_at', '')[:19]}</td><td>{o.get('symbol')}</td>"
        f"<td>{o.get('segment', 'CASH')}</td>"
        f"<td>{_side_cell(o.get('side', ''))}</td><td class='mono num'>{o.get('qty')}</td>"
        f"<td>{_status_chip(o.get('status', ''))}</td><td class='dim'>{o.get('reason', '')}</td></tr>"
        for o in reversed(status.get("recent_orders", []))
    ) or "<tr><td colspan='7'>No orders yet</td></tr>"

    def _fmt_pnl(v):
        if v is None:
            return "—"
        color = "#e74c3c" if v < 0 else "#2ecc71"
        return f"<span style='color:{color};'>₹{v:,.2f}</span>"

    position_rows = "".join(
        f"<tr><td>{p['symbol']}</td><td class='mono num'>{p['qty']}</td>"
        f"<td class='mono num'>₹{p['entry_price']:.2f}</td>"
        f"<td class='mono num'>{'₹' + format(p['current_price'], '.2f') if p['current_price'] is not None else '—'}</td>"
        f"<td class='mono num'>{_fmt_pnl(p['unrealized_pnl'])}</td></tr>"
        for p in status.get("open_positions", [])
    ) or "<tr><td colspan='5'>No open positions</td></tr>"

    win_count = status.get("win_count", 0)
    loss_count = status.get("loss_count", 0)
    win_rate = status.get("win_rate")
    win_rate_str = f"{win_rate:.0f}%" if win_rate is not None else "—"

    return f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="10">
    <title>Groww Agent Dashboard</title>
    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&display=swap">
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: -apple-system, sans-serif; background: #0d1117; color: #e6edf3;
                max-width: 1180px; margin: 40px auto; padding: 0 20px 60px; }}
        h1 {{ font-size: 1.3rem; margin: 0; }}
        .mono {{ font-family: 'IBM Plex Mono', 'SF Mono', Consolas, monospace; }}
        .num {{ text-align: right; }}
        .dim {{ color: #8b949e; }}
        .badge {{ display: inline-block; padding: 4px 12px; border-radius: 4px;
                  color: #0d1117; font-weight: 700; font-size: 0.78rem; letter-spacing: 0.03em;
                  background: {status_color}; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 4px; }}
        th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #21262d;
                   font-size: 0.85rem; }}
        th {{ color: #7d8590; font-weight: 500; font-size: 0.72rem; text-transform: uppercase;
              letter-spacing: 0.04em; }}
        th.num, td.num {{ text-align: right; }}
        tr:last-child td {{ border-bottom: none; }}
        .scroll-table {{ max-height: 340px; overflow-y: auto; }}
        .scroll-table th {{ position: sticky; top: 0; background: #161b22; }}
        .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px;
                  padding: 16px 18px; margin-bottom: 18px; }}
        .stat {{ display: flex; flex-direction: column; gap: 3px; margin-right: 30px; }}
        .stat .label {{ color: #7d8590; font-size: 0.72rem; text-transform: uppercase;
                         letter-spacing: 0.04em; }}
        .stat .value {{ font-size: 1.2rem; font-weight: 600; }}
        .stat-row {{ display: flex; flex-wrap: wrap; }}
        .dashboard-grid {{ display: grid; grid-template-columns: 1fr 320px; gap: 20px;
                            margin-top: 18px; align-items: start; }}
        @media (max-width: 900px) {{ .dashboard-grid {{ grid-template-columns: 1fr; }} }}
        .ltp-grid {{ display: grid; grid-template-columns: 1fr; }}
        .ltp-row {{ display: flex; justify-content: space-between; padding: 8px 0;
                    border-bottom: 1px solid #21262d; font-size: 0.85rem; }}
        .sig-terminal {{ font-size: 0.8rem; }}
        .sig-row {{ display: grid; grid-template-columns: 10px 110px 100px 1fr auto auto;
                    gap: 14px; align-items: center; padding: 5px 8px; border-radius: 3px; }}
        .sig-row:nth-child(odd) {{ background: rgba(255,255,255,0.03); }}
        .sig-dot {{ width: 6px; height: 6px; border-radius: 50%; display: inline-block; }}
        .sig-symbol {{ font-weight: 600; }}
        @media (max-width: 700px) {{
            .sig-row {{ grid-template-columns: 10px 90px 84px 1fr; }}
            .sig-row span:nth-child(5), .sig-row span:nth-child(6) {{ display: none; }}
        }}
        .market-bar {{ display: flex; align-items: center; gap: 10px; margin-top: 10px;
                       font-family: 'IBM Plex Mono', 'SF Mono', Consolas, monospace; font-size: 0.9rem; }}
        .mkt-dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; }}
        .mkt-state {{ font-weight: 700; letter-spacing: 0.5px; }}
        .mkt-label {{ color: #8b949e; }}
        .ist-clock {{ margin-left: auto; color: #58a6ff; font-weight: 600; }}
        .switch-btn {{ margin-left: 16px; padding: 6px 16px; border-radius: 5px; border: none;
                        font-weight: 600; cursor: pointer; font-size: 0.82rem; }}
        .switch-btn.halt {{ background: #e74c3c; color: #fff; }}
        .switch-btn.resume {{ background: #2ecc71; color: #0d1117; }}
        .switch-btn:disabled {{ background: #30363d; color: #8b949e; cursor: not-allowed; }}
        .proc-badge {{ margin-left: 8px; font-family: 'IBM Plex Mono', 'SF Mono', Consolas, monospace;
                        font-size: 0.78rem; }}
    </style>
    <script>
        function tickClock() {{
            const el = document.getElementById('ist-clock');
            if (!el) return;
            const now = new Date().toLocaleTimeString('en-GB', {{
                timeZone: 'Asia/Kolkata', hour12: false
            }});
            el.textContent = now + ' IST';
        }}
        setInterval(tickClock, 1000);

        async function postBotAction(path, reason) {{
            const url = `${{path}}?reason=${{encodeURIComponent(reason)}}`;
            const btn = document.getElementById('switch-btn');
            if (btn) btn.disabled = true;
            try {{
                const res = await fetch(url, {{ method: 'POST' }});
                const body = await res.json().catch(() => ({{}}));
                if (!res.ok) {{
                    alert('Failed: ' + (body.detail || res.statusText));
                }} else if (body.process_error) {{
                    alert('Trading action succeeded, but the bot PROCESS could not be ' +
                          'controlled (needs the one-time sudo setup): ' + body.process_error);
                }} else if (body.trade_error) {{
                    alert('Bot process action succeeded, but trading could not be resumed: ' +
                          body.trade_error);
                }}
            }} catch (e) {{
                alert('Request failed: ' + e);
            }} finally {{
                window.location.reload();
            }}
        }}

        function doStop() {{
            if (!confirm('Stop the bot now? No new orders will be placed, and the process ' +
                         'will be stopped too if the dashboard has permission to do so.')) return;
            const reason = prompt('Reason for stopping?', 'manual stop (dashboard)');
            if (reason === null) return;
            postBotAction('/api/bot/stop', reason);
        }}

        function doStart() {{
            if (!confirm('Start the bot now?')) return;
            const reason = prompt('Reason for starting?', 'manual start (dashboard)');
            if (reason === null) return;
            postBotAction('/api/bot/start', reason);
        }}
    </script>
</head>
<body>
    <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
        <h1>Groww Trading Agent</h1>
        <span class="badge">{status_text}</span>{switch_button} {proc_badge}
        <div style="margin-left:auto; display:flex; align-items:center; gap:16px; font-size:0.85rem;">
            <a href="/backtest" style="color:#58a6ff;">Backtest &amp; market replay →</a>
            <a href="/logout" style="color:#8b949e;">Log out</a>
        </div>
    </div>
    <div class="market-bar">
        <span class="mkt-dot" style="background:{mkt_color};"></span>
        <span class="mkt-state">NSE: {mkt_state}</span>
        <span class="mkt-label">— {mkt_label}</span>
        <span id="ist-clock" class="ist-clock">{clock_str} IST</span>
    </div>
    <p style="color:#8b949e; font-size:0.8rem; margin:6px 0 0;">Last updated: {updated} · auto-refreshes every 10s</p>

    <div class="dashboard-grid">
        <div class="col-main">
            <div class="card">
                <h3 style="margin-top:0;">Strategy signals</h3>
                <p style="color:#8b949e; font-size:0.85rem; margin-top:-8px;">
                    What the strategy is currently seeing per symbol — how close it is to a real
                    crossover signal, not just the raw price.
                </p>
                <div class="sig-terminal mono">{strategy_rows}</div>
            </div>

            <div class="card">
                <h3 style="margin-top:0;">Open positions</h3>
                <table><tr><th>Symbol</th><th class="num">Qty</th><th class="num">Entry price</th><th class="num">Current price</th><th class="num">Unrealized P&amp;L</th></tr>{position_rows}</table>
            </div>

            <div class="card">
                <h3 style="margin-top:0;">All orders</h3>
                <div class="scroll-table">
                    <table><tr><th>Time</th><th>Symbol</th><th>Segment</th><th>Side</th><th class="num">Qty</th><th>Status</th><th>Reason</th></tr>{order_rows}</table>
                </div>
            </div>
        </div>

        <div class="col-side">
            <div class="card">
                <div class="stat-row">
                    <div class="stat"><div class="label">Mode</div><div class="value">{status.get('mode', 'UNKNOWN')}</div></div>
                    <div class="stat"><div class="label">Trades today</div><div class="value">{status.get('trades_today', 0)}</div></div>
                </div>
                <div class="stat" style="margin-top:10px;"><div class="label">Symbols watched</div><div class="value">{len(status.get('symbols', []))}</div></div>
                {"<div class='stat' style='margin-top:10px;'><div class='label'>Halt reason</div><div class='value' style='color:#e74c3c; font-size:0.9rem;'>" + status.get('halt_reason','') + "</div></div>" if halted else ""}
            </div>

            <div class="card">
                <h3 style="margin-top:0;">Capital &amp; risk</h3>
                <div style="display:flex; flex-direction:column; gap:10px;">
                    <div style="display:flex; justify-content:space-between; border-bottom:1px solid #21262d; padding-bottom:8px;">
                        <span class="dim" style="font-size:0.78rem;">Deployed capital</span>
                        <span class="mono" style="font-weight:600;">₹{status.get('deployed_capital', 0):,.2f}</span>
                    </div>
                    <div style="display:flex; justify-content:space-between; border-bottom:1px solid #21262d; padding-bottom:8px;">
                        <span class="dim" style="font-size:0.78rem;">Total capital cap</span>
                        <span class="mono" style="font-weight:600;">₹{status.get('total_capital_cap', 0):,.2f}</span>
                    </div>
                    <div style="display:flex; justify-content:space-between; border-bottom:1px solid #21262d; padding-bottom:8px;">
                        <span class="dim" style="font-size:0.78rem;">Realized P&amp;L today</span>
                        <span class="mono" style="font-weight:600; color:{pnl_color};">₹{status.get('realized_pnl_today', 0):,.2f}</span>
                    </div>
                    <div style="display:flex; justify-content:space-between; border-bottom:1px solid #21262d; padding-bottom:8px;">
                        <span class="dim" style="font-size:0.78rem;">Daily loss limit</span>
                        <span class="mono" style="font-weight:600;">₹{status.get('max_daily_loss', 0):,.2f}</span>
                    </div>
                    <div style="display:flex; justify-content:space-between;">
                        <span class="dim" style="font-size:0.78rem;">F&amp;O trading</span>
                        <span style="font-weight:600; color:{'#2ecc71' if status.get('allow_fno') else '#8b949e'};">{'Enabled' if status.get('allow_fno') else 'Disabled'}</span>
                    </div>
                </div>
            </div>

            <div class="card">
                <h3 style="margin-top:0;">Latest prices</h3>
                <div class="ltp-grid">{ltp_rows}</div>
            </div>

            <div class="card">
                <h3 style="margin-top:0;">Performance</h3>
                <div class="stat-row">
                    <div class="stat"><div class="label">Wins</div><div class="value" style="color:#2ecc71;">{win_count}</div></div>
                    <div class="stat"><div class="label">Losses</div><div class="value" style="color:#e74c3c;">{loss_count}</div></div>
                    <div class="stat"><div class="label">Win rate</div><div class="value">{win_rate_str}</div></div>
                </div>
            </div>
        </div>
    </div>
</body>
</html>
"""


def _build_status_view() -> dict:
    heartbeat = read_heartbeat()
    settings = load_settings()
    daily = risk_repo.get_or_create_daily_summary(date.today().isoformat())
    last_ltp = heartbeat.get("last_ltp", {})

    open_positions = []
    for pos in positions_repo.get_open_positions():
        current_price = last_ltp.get(pos["symbol"])
        unrealized = (
            (current_price - pos["entry_price"]) * pos["qty"]
            if current_price is not None else None
        )
        open_positions.append({**pos, "current_price": current_price, "unrealized_pnl": unrealized})

    win_count, loss_count = positions_repo.get_win_loss_counts()
    total_trades = win_count + loss_count
    win_rate = (win_count / total_trades * 100) if total_trades > 0 else None

    recent_orders = [
        {
            "created_at": o["created_at"],
            "symbol": o["symbol"],
            "segment": o["segment"],
            "side": o["side"],
            "qty": o["qty"],
            "status": o["status"],
            "reason": o["message"] if o["status"] == "BLOCKED" else o["reason"],
        }
        for o in reversed(orders_repo.get_recent_orders(limit=None))
    ]

    return {
        "updated_at": heartbeat.get("updated_at"),
        "mode": heartbeat.get("mode", "UNKNOWN"),
        "strategy_debug": heartbeat.get("strategy_debug", {}),
        "bot_process_status": _bot_process_status(),
        "halted": bool(daily["halted"]),
        "halt_reason": daily["halt_reason"],
        "halt_source": daily.get("halt_source", "AUTO"),
        "trades_today": daily["trades_count"],
        "symbols": heartbeat.get("symbols", []),
        "last_ltp": last_ltp,
        "recent_orders": recent_orders,
        "deployed_capital": positions_repo.get_deployed_capital(),
        "total_capital_cap": settings.risk.total_capital_inr,
        "realized_pnl_today": daily["realized_pnl"],
        "max_daily_loss": settings.risk.max_daily_loss_inr,
        "allow_fno": settings.risk.allow_fno,
        "open_positions": open_positions,
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate": win_rate,
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    if (r := _require_page_auth(request)):
        return r
    status = _build_status_view()
    return _render(status)


@app.get("/api/status")
def api_status(request: Request):
    _require_api_auth(request)
    return _build_status_view()


def _bot_process_status() -> str:
    """Read-only — `systemctl is-active` needs no special privilege, unlike start/stop."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "groww-bot.service"],
            capture_output=True, text=True, timeout=5,
        )
        return (result.stdout or "unknown").strip()
    except Exception as e:
        logger.error("Failed to check groww-bot.service status: %s", e)
        return "unknown"


def _control_bot_process(action: str) -> tuple[bool, str | None]:
    """action: "start" or "stop". Requires the dashboard's OS user to have a one-time,
    narrowly-scoped sudoers rule for exactly these two systemctl commands — see the operator
    setup notes. Without it, this fails cleanly and callers still apply the trading
    halt/resume half of the action, which needs no special privilege at all."""
    try:
        result = subprocess.run(
            ["sudo", "-n", "systemctl", action, "groww-bot.service"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False, (result.stderr or result.stdout or "unknown error").strip()
        return True, None
    except Exception as e:
        return False, str(e)


@app.post("/api/bot/stop")
def api_bot_stop(request: Request, reason: str = "manual stop (dashboard)"):
    _require_api_auth(request)
    rm = _risk_manager()
    rm.manual_halt(reason)
    process_ok, process_err = _control_bot_process("stop")
    return JSONResponse({
        "halted": True, "halt_source": "MANUAL", "halt_reason": reason,
        "process_stopped": process_ok, "process_error": process_err,
    })


@app.post("/api/bot/start")
def api_bot_start(request: Request, reason: str = "manual start (dashboard)"):
    _require_api_auth(request)
    process_ok, process_err = _control_bot_process("start")

    rm = _risk_manager()
    trade_error = None
    if rm.halted:
        try:
            rm.resume(reason)
        except RuntimeError as e:
            trade_error = str(e)

    return JSONResponse({
        "halted": rm.halted, "process_started": process_ok, "process_error": process_err,
        "trade_error": trade_error,
    })


def _render_backtest_page(form: dict, result: dict | None, error: str | None) -> str:
    from core.db import optimization_repo

    if error:
        result_html = f'<div class="card" style="border-color:#e74c3c;"><h3 style="margin-top:0;color:#e74c3c;">Backtest failed</h3><pre style="white-space:pre-wrap;font-size:0.85rem;color:#e6edf3;">{error}</pre></div>'
    elif result:
        def _fmt(v):
            return "—" if v is None else v
        result_html = f"""
        <div class="card">
            <h3 style="margin-top:0;">Result</h3>
            <div class="stat"><div class="label">Bars processed</div><div class="value">{result.get('bars_processed')}</div></div>
            <div class="stat"><div class="label">Trades</div><div class="value">{result.get('total_trades')}</div></div>
            <div class="stat"><div class="label">Win rate</div><div class="value">{_fmt(result.get('win_rate_pct'))}{'%' if result.get('win_rate_pct') is not None else ''}</div></div>
            <div class="stat"><div class="label">Net P&amp;L</div><div class="value">₹{result.get('net_pnl', 0):,.2f}</div></div>
            <div class="stat"><div class="label">Max drawdown</div><div class="value">₹{result.get('max_drawdown', 0):,.2f}</div></div>
        </div>
        """
    else:
        result_html = ""

    runs = optimization_repo.get_recent_runs(limit=10)
    opt_rows = ""
    for run in runs:
        best = run["candidates"][0] if run["candidates"] else None
        baseline_out = run["baseline"]["out_sample"]
        best_out = best["out_sample"] if best else None
        flag = " (insufficient sample)" if best and best.get("insufficient_sample") else ""
        opt_rows += (
            f"<tr><td>{run['run_at'][:19]}</td><td>{run['symbol']}</td>"
            f"<td>₹{baseline_out['net_pnl']:.2f} ({baseline_out['total_trades']} trades)</td>"
            f"<td>{'₹%.2f (%d trades)%s' % (best_out['net_pnl'], best_out['total_trades'], flag) if best_out else '—'}</td>"
            f"<td>{run['combinations_tried']}</td></tr>"
        )
    opt_rows = opt_rows or "<tr><td colspan='5'>No nightly optimization runs yet.</td></tr>"

    return f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Backtest &amp; Market Replay — Groww Agent</title>
    <style>
        body {{ font-family: -apple-system, sans-serif; background: #0d1117; color: #e6edf3;
                max-width: 900px; margin: 40px auto; padding: 0 20px; }}
        h1 {{ font-size: 1.4rem; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
        th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid #30363d;
                   font-size: 0.9rem; }}
        th {{ color: #8b949e; font-weight: 500; }}
        .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px;
                  padding: 16px; margin-bottom: 20px; }}
        .stat {{ display: inline-block; margin-right: 30px; }}
        .stat .label {{ color: #8b949e; font-size: 0.8rem; }}
        .stat .value {{ font-size: 1.3rem; font-weight: 600; }}
        label {{ display: block; margin-top: 10px; color: #8b949e; font-size: 0.85rem; }}
        input, select {{ background: #0d1117; color: #e6edf3; border: 1px solid #30363d;
                          border-radius: 4px; padding: 6px 8px; margin-top: 4px; width: 200px; }}
        button.run-btn {{ margin-top: 16px; padding: 8px 20px; border-radius: 6px; border: none;
                           background: #58a6ff; color: #0d1117; font-weight: 600; cursor: pointer; }}
    </style>
</head>
<body>
    <h1>Backtest &amp; Market Replay</h1>
    <p><a href="/" style="color:#58a6ff;">← Back to dashboard</a></p>
    <p style="color:#8b949e; font-size:0.85rem;">
        Runs the real strategy against historical Groww candle data via a paper broker — never
        places a real order. Groww caps query windows: 30 days max for intraday intervals
        (1minute/5minute/15minute/1hour), 180 days max for 1day.
    </p>

    <div class="card">
        <form method="post" action="/backtest">
            <label>Symbols (comma-separated)
                <input type="text" name="symbols" value="{form.get('symbols', 'RELIANCE')}">
            </label>
            <label>Start
                <input type="text" name="start" value="{form.get('start', '')}" placeholder="YYYY-MM-DD">
            </label>
            <label>End
                <input type="text" name="end" value="{form.get('end', '')}" placeholder="YYYY-MM-DD">
            </label>
            <label>Interval
                <select name="interval">
                    {"".join(f'<option value="{iv}" {"selected" if form.get("interval") == iv else ""}>{iv}</option>' for iv in ["5minute", "1minute", "15minute", "1hour", "1day"])}
                </select>
            </label>
            <button type="submit" class="run-btn">Run backtest</button>
        </form>
    </div>

    {result_html}

    <div class="card">
        <h3 style="margin-top:0;">Nightly optimization history</h3>
        <p style="color:#8b949e; font-size:0.85rem; margin-top:-8px;">
            Automated off-hours parameter sweeps (see scripts/nightly_optimize.py). Report only —
            nothing here is ever applied automatically; adopting a candidate requires manually
            editing strategies/ma_rsi_strategy.py and redeploying.
        </p>
        <table>
            <tr><th>Run</th><th>Symbol</th><th>Baseline (out-of-sample)</th><th>Best candidate (out-of-sample)</th><th>Combos tried</th></tr>
            {opt_rows}
        </table>
    </div>
</body>
</html>
"""


@app.get("/backtest", response_class=HTMLResponse)
def backtest_page(request: Request):
    if (r := _require_page_auth(request)):
        return r
    return _render_backtest_page(form={"interval": "5minute"}, result=None, error=None)


@app.post("/backtest", response_class=HTMLResponse)
def backtest_run(request: Request, symbols: str = Form("RELIANCE"), start: str = Form(""),
                  end: str = Form(""), interval: str = Form("5minute")):
    if (r := _require_page_auth(request)):
        return r

    form = {"symbols": symbols, "start": start, "end": end, "interval": interval}
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    result, error = None, None
    if not symbol_list or not start or not end:
        error = "Symbols, start, and end are all required."
    else:
        try:
            result = _run_backtest_subprocess(symbol_list, start, end, interval)
        except Exception as e:
            error = str(e)

    return _render_backtest_page(form=form, result=result, error=error)
