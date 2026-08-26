"""
Lightweight status dashboard for the trading bot.
Run: uvicorn dashboard.app:app --host 0.0.0.0 --port 8000
"""
import os
from datetime import date, datetime, timezone

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from dotenv import load_dotenv

from config.settings import load_settings
from core.db import orders_repo, positions_repo, risk_repo
from core.risk_manager import RiskManager
from core.status_writer import read_heartbeat

load_dotenv()

app = FastAPI(title="Groww Agent Dashboard")

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")


def _check_auth(request: Request):
    if not DASHBOARD_PASSWORD:
        return
    supplied = request.query_params.get("key", "")
    if supplied != DASHBOARD_PASSWORD:
        raise HTTPException(status_code=401, detail="Missing or incorrect ?key=")


def _risk_manager() -> RiskManager:
    """Builds a RiskManager the same way scripts/halt_bot.py and scripts/resume_bot.py
    do — it writes straight to the shared daily_summary row, which the running bot's
    Orchestrator re-reads every cycle via RiskManager.refresh_halt_state(), so the
    dashboard needs no direct connection to the bot process."""
    settings = load_settings()
    return RiskManager(settings.risk, ntfy_topic=settings.ntfy_topic)


def _market_status_ist():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    weekday = now_ist.weekday()
    t = now_ist.time()
    from datetime import time as dtime
    if weekday >= 5:
        return "CLOSED", "Weekend", now_ist
    if dtime(9, 0) <= t < dtime(9, 15):
        return "PRE-OPEN", "Pre-open session", now_ist
    if dtime(9, 15) <= t < dtime(15, 30):
        return "OPEN", "Regular trading session", now_ist
    if dtime(15, 30) <= t < dtime(16, 0):
        return "CLOSING", "Closing/post-close session", now_ist
    return "CLOSED", "Outside trading hours", now_ist


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
        switch_button = '<button id="switch-btn" class="switch-btn resume" onclick="doResume()">Resume trading</button>'
    elif halted:
        switch_button = ('<button class="switch-btn resume" disabled '
                          'title="Automatic halts (daily loss limit, circuit breaker, '
                          'reconciliation mismatch) can only clear on the next trading day, '
                          'not from here.">Resume trading (auto halt)</button>')
    else:
        switch_button = '<button id="switch-btn" class="switch-btn halt" onclick="doHalt()">Halt trading</button>'

    mkt_state, mkt_label, now_ist = _market_status_ist()
    mkt_color = {"OPEN": "#2ecc71", "PRE-OPEN": "#f39c12",
                 "CLOSING": "#f39c12", "CLOSED": "#8b949e"}[mkt_state]
    clock_str = now_ist.strftime("%H:%M:%S")

    ltp_rows = "".join(
        f"<tr><td>{sym}</td><td>{price if price is not None else '—'}</td></tr>"
        for sym, price in status.get("last_ltp", {}).items()
    ) or "<tr><td colspan='2'>No symbols yet</td></tr>"

    order_rows = "".join(
        f"<tr><td>{o.get('created_at', '')[:19]}</td><td>{o.get('symbol')}</td>"
        f"<td>{o.get('segment', 'CASH')}</td>"
        f"<td>{o.get('side')}</td><td>{o.get('qty')}</td>"
        f"<td>{o.get('status')}</td><td>{o.get('reason', '')}</td></tr>"
        for o in reversed(status.get("recent_orders", []))
    ) or "<tr><td colspan='7'>No orders yet</td></tr>"

    def _fmt_pnl(v):
        if v is None:
            return "—"
        color = "#e74c3c" if v < 0 else "#2ecc71"
        return f"<span style='color:{color};'>₹{v:,.2f}</span>"

    position_rows = "".join(
        f"<tr><td>{p['symbol']}</td><td>{p['qty']}</td>"
        f"<td>₹{p['entry_price']:.2f}</td>"
        f"<td>{'₹' + format(p['current_price'], '.2f') if p['current_price'] is not None else '—'}</td>"
        f"<td>{_fmt_pnl(p['unrealized_pnl'])}</td></tr>"
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
    <style>
        body {{ font-family: -apple-system, sans-serif; background: #0d1117; color: #e6edf3;
                max-width: 900px; margin: 40px auto; padding: 0 20px; }}
        h1 {{ font-size: 1.4rem; }}
        .badge {{ display: inline-block; padding: 4px 12px; border-radius: 12px;
                  color: #0d1117; font-weight: 600; background: {status_color}; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
        th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid #30363d;
                   font-size: 0.9rem; }}
        th {{ color: #8b949e; font-weight: 500; }}
        .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px;
                  padding: 16px; margin-bottom: 20px; }}
        .stat {{ display: inline-block; margin-right: 30px; }}
        .stat .label {{ color: #8b949e; font-size: 0.8rem; }}
        .stat .value {{ font-size: 1.3rem; font-weight: 600; }}
        .market-bar {{ display: flex; align-items: center; gap: 10px; margin-top: 10px;
                       font-family: 'SF Mono', Consolas, monospace; font-size: 0.95rem; }}
        .mkt-dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; }}
        .mkt-state {{ font-weight: 700; letter-spacing: 0.5px; }}
        .mkt-label {{ color: #8b949e; }}
        .ist-clock {{ margin-left: auto; color: #58a6ff; font-weight: 600; }}
        .switch-btn {{ margin-left: 16px; padding: 6px 16px; border-radius: 6px; border: none;
                        font-weight: 600; cursor: pointer; font-size: 0.85rem; }}
        .switch-btn.halt {{ background: #e74c3c; color: #fff; }}
        .switch-btn.resume {{ background: #2ecc71; color: #0d1117; }}
        .switch-btn:disabled {{ background: #30363d; color: #8b949e; cursor: not-allowed; }}
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

        function dashboardKey() {{
            return new URLSearchParams(window.location.search).get('key') || '';
        }}

        async function postAction(path, reason) {{
            const key = dashboardKey();
            const url = `${{path}}?reason=${{encodeURIComponent(reason)}}` +
                        (key ? `&key=${{encodeURIComponent(key)}}` : '');
            const btn = document.getElementById('switch-btn');
            if (btn) btn.disabled = true;
            try {{
                const res = await fetch(url, {{ method: 'POST' }});
                if (!res.ok) {{
                    const body = await res.json().catch(() => ({{}}));
                    alert('Failed: ' + (body.detail || res.statusText));
                }}
            }} catch (e) {{
                alert('Request failed: ' + e);
            }} finally {{
                window.location.reload();
            }}
        }}

        function doHalt() {{
            const reason = prompt('Reason for halting the bot?', 'manual kill switch (dashboard)');
            if (reason === null) return;
            if (!confirm('Halt live trading now? No new orders will be placed until resumed.')) return;
            postAction('/api/halt', reason);
        }}

        function doResume() {{
            const reason = prompt('Reason for resuming the bot?', 'manual resume (dashboard)');
            if (reason === null) return;
            if (!confirm('Resume trading now?')) return;
            postAction('/api/resume', reason);
        }}
    </script>
</head>
<body>
    <h1>Groww Trading Agent <span class="badge">{status_text}</span>{switch_button}</h1>
    <div class="market-bar">
        <span class="mkt-dot" style="background:{mkt_color};"></span>
        <span class="mkt-state">NSE: {mkt_state}</span>
        <span class="mkt-label">— {mkt_label}</span>
        <span id="ist-clock" class="ist-clock">{clock_str} IST</span>
    </div>
    <p style="color:#8b949e; font-size:0.85rem;">Last updated: {updated} · auto-refreshes every 10s</p>

    <div class="card">
        <div class="stat"><div class="label">Mode</div><div class="value">{status.get('mode', 'UNKNOWN')}</div></div>
        <div class="stat"><div class="label">Trades today</div><div class="value">{status.get('trades_today', 0)}</div></div>
        <div class="stat"><div class="label">Symbols watched</div><div class="value">{len(status.get('symbols', []))}</div></div>
        {"<div class='stat'><div class='label'>Halt reason</div><div class='value' style='color:#e74c3c; font-size:0.9rem;'>" + status.get('halt_reason','') + "</div></div>" if halted else ""}
    </div>

    <div class="card">
        <h3 style="margin-top:0;">Capital &amp; risk</h3>
        <div class="stat"><div class="label">Deployed capital</div><div class="value">₹{status.get('deployed_capital', 0):,.2f}</div></div>
        <div class="stat"><div class="label">Total capital cap</div><div class="value">₹{status.get('total_capital_cap', 0):,.2f}</div></div>
        <div class="stat"><div class="label">Realized P&amp;L today</div><div class="value" style="color:{pnl_color};">₹{status.get('realized_pnl_today', 0):,.2f}</div></div>
        <div class="stat"><div class="label">Daily loss limit</div><div class="value">₹{status.get('max_daily_loss', 0):,.2f}</div></div>
        <div class="stat"><div class="label">F&amp;O trading</div><div class="value" style="color:{'#2ecc71' if status.get('allow_fno') else '#8b949e'};">{'ENABLED' if status.get('allow_fno') else 'Disabled'}</div></div>
    </div>

    <div class="card">
        <h3 style="margin-top:0;">Latest prices</h3>
        <table><tr><th>Symbol</th><th>Last traded price</th></tr>{ltp_rows}</table>
    </div>

    <div class="card">
        <h3 style="margin-top:0;">Open positions</h3>
        <table><tr><th>Symbol</th><th>Qty</th><th>Entry price</th><th>Current price</th><th>Unrealized P&amp;L</th></tr>{position_rows}</table>
    </div>

    <div class="card">
        <h3 style="margin-top:0;">Performance</h3>
        <div class="stat"><div class="label">Wins</div><div class="value" style="color:#2ecc71;">{win_count}</div></div>
        <div class="stat"><div class="label">Losses</div><div class="value" style="color:#e74c3c;">{loss_count}</div></div>
        <div class="stat"><div class="label">Win rate</div><div class="value">{win_rate_str}</div></div>
    </div>

    <div class="card">
        <h3 style="margin-top:0;">All orders</h3>
        <table><tr><th>Time</th><th>Symbol</th><th>Segment</th><th>Side</th><th>Qty</th><th>Status</th><th>Reason</th></tr>{order_rows}</table>
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
    _check_auth(request)
    status = _build_status_view()
    return _render(status)


@app.get("/api/status")
def api_status(request: Request):
    _check_auth(request)
    return _build_status_view()


@app.post("/api/halt")
def api_halt(request: Request, reason: str = "manual kill switch (dashboard)"):
    _check_auth(request)
    rm = _risk_manager()
    rm.manual_halt(reason)
    return JSONResponse({"halted": True, "halt_source": "MANUAL", "halt_reason": reason})


@app.post("/api/resume")
def api_resume(request: Request, reason: str = "manual resume (dashboard)"):
    _check_auth(request)
    rm = _risk_manager()
    if not rm.halted:
        return JSONResponse({"halted": False, "message": "Not currently halted."})
    try:
        rm.resume(reason)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return JSONResponse({"halted": False})
