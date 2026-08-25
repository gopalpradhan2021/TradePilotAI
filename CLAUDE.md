# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An automated trading bot for the Groww broker (Indian equities + F&O). It runs a strategy loop that
proposes orders, forces them through a risk-check gate, and executes them via a paper or live broker
backend. A separate FastAPI dashboard reads the bot's status for monitoring.

## Commands

```bash
# Install deps (project uses a local venv/)
pip install -r requirements.txt

# Run the bot — paper trading (default, safe, no real orders)
python main.py

# Run the bot — paper trading against specific symbols / poll interval
python main.py --symbols RELIANCE TCS --poll-interval 5

# Run the bot — LIVE trading (places real orders)
# Requires BOTH the --live flag AND MODE=LIVE in .env — this is intentional
# redundancy so a stray env var or a stray flag alone can't trade real money.
python main.py --live

# Run the status dashboard (reads from SQLite + logs/heartbeat.json, auto-refreshes every 10s)
uvicorn dashboard.app:app --host 0.0.0.0 --port 8000

# Apply DB migrations manually (main.py also does this automatically on startup)
python -m core.db.migrate

# Run the test suite
pytest -v

# Send the daily Groww API key re-approval reminder manually (normally fires via
# deploy/groww-key-reminder.timer at 9 PM IST, the evening before the 6 AM reset)
python -m scripts.groww_key_reminder
```

Tests live in `tests/` (pytest, see `pytest.ini`/`requirements-dev.txt`) and run in CI on every
push/PR to `main` (`.github/workflows/ci.yml`). There is no lint config yet.

Config is via `.env` (see `.env.example`), loaded through `config/settings.py`. Key risk knobs:
`MAX_ORDER_VALUE_INR`, `MAX_DAILY_LOSS_INR`, `MAX_TRADES_PER_DAY`, `MAX_POSITION_QTY`,
`PRICE_SANITY_BAND_PCT`, `TOTAL_CAPITAL_INR`, `ALLOW_FNO`. `DATABASE_PATH` controls the SQLite file
location (default `data/trading.db`).

## Architecture

Data flow per poll cycle, driven by `Orchestrator.run_forever()` in `core/orchestrator.py`:

```
strategy.decide() -> ProposedOrder -> orders_repo.insert_order() -> risk_manager.check()
    -> broker.place_order() -> orders_repo.update_order_status() -> positions_repo -> heartbeat
```

**No bypass**: every `ProposedOrder` must pass through `RiskManager.check()` before it can reach a
broker. This is a deliberate invariant — don't add code paths that call `broker.place_order()`
directly from the orchestrator or a strategy.

- **`core/models.py`** — shared dataclasses/enums (`ProposedOrder`, `RiskCheckResult`,
  `ExecutionResult`, `Side`, `OrderType`, `Segment`, `OptionType`). `ProposedOrder.total_units`
  accounts for F&O lot size (`qty * lot_size` for FNO, `qty` for CASH) — risk and execution logic
  keys off `total_units`, not `qty`, when valuing/sizing an order.

- **`strategies/`** — `BaseStrategy.decide(symbol, last_traded_price) -> ProposedOrder | None` is the
  only strategy interface. `ma_rsi_strategy.py` (MA crossover + RSI filter) is the only real
  strategy; it keeps its own per-symbol rolling price window (`SymbolState`, fed one LTP per cycle)
  rather than calling a historical-data API. Strategies are stateful across cycles but must stay
  broker-agnostic — they only see price, never account/position state.

- **`core/risk_manager.py`** — the single gate all orders pass through. Per-day counters
  (`_trades_today`, `_realized_pnl_today`) and the `halted`/`halt_reason` kill-switch are persisted
  in the `daily_summary` table (via `core/db/risk_repo.py`) and reloaded on `__init__` and on
  wall-clock date change (`_roll_day_if_needed`), so a halt survives a process restart instead of
  silently clearing. `_deployed_capital` is not stored state at all — it's a property that queries
  `positions_repo.get_deployed_capital()` (`SUM(qty*entry_price)` over open positions) fresh on every
  check, so it can't drift from reality. Idempotency-key dedup is enforced by a `UNIQUE` constraint on
  `orders.idempotency_key`, not an in-memory set — `check()` takes an `order_id` (the row inserted
  just before it's called) purely to link the audit row in `risk_events`; the dedup rejection itself
  happens earlier, in `Orchestrator._handle_proposed_order()`, when `orders_repo.insert_order()`
  raises `DuplicateIdempotencyKeyError`. `check()` writes one `risk_events` row per call (approved or
  rejected) via `_record_check_event()`; `record_fill()`/`manual_halt()` also write a `HALTED` event.
  Once halted, `check()` short-circuits and rejects everything until day rollover.

- **`core/execution.py`** — `Broker` ABC with two implementations: `PaperBroker` (simulates fills at
  LTP or limit price, no real orders, but can still fetch real LTP from Groww if a client is passed)
  and `LiveBroker` (wraps `growwapi.GrowwAPI`). Both share `_build_trading_symbol()` for constructing
  Groww F&O trading symbols (`SYMBOL` + expiry + strike + CE/PE, or `FUT`).

- **`core/db/`** — the persistence layer (Phase 0). Plain `sqlite3` + hand-written SQL, no ORM.
  - `connection.py` — `get_connection()` is a context manager that opens a short-lived connection
    per call (WAL mode, `foreign_keys=ON`) rather than sharing one across threads/processes. DB file
    path comes from `DATABASE_PATH` env var (default `data/trading.db`).
  - `migrations/NNNN_*.sql` + `migrate.py` — numbered, idempotent (`CREATE TABLE IF NOT EXISTS`) SQL
    files applied in order and tracked in a `schema_migrations` table. Run via `python -m
    core.db.migrate`, or automatically at `main.py` startup. Add new migrations as new numbered files
    — never edit an already-applied one.
  - `orders_repo.py` / `positions_repo.py` / `risk_repo.py` — one module per table (plus
    `risk_events`/`daily_summary` sharing `risk_repo.py`). All SQL lives here; `risk_manager.py` and
    `orchestrator.py` never touch `sqlite3` directly.
  - Tables: `orders` (full lifecycle, `idempotency_key UNIQUE`), `positions` (one open row per
    symbol, enforced by a partial unique index; `entry_order_id`/`exit_order_id` FK back to
    `orders`), `risk_events` (audit trail of every risk decision plus halt/clear events,
    `order_id` nullable), `daily_summary` (one row per trading day: `trades_count`, `realized_pnl`,
    `halted`, `halt_reason`).

- **`core/orchestrator.py`** — wires strategy → risk_manager → broker per symbol per cycle. Holds
  no durable state itself, only `_last_ltp` (this-cycle price cache, ephemeral by design — see
  `status_writer.py` below). `_handle_proposed_order()` inserts an `orders` row immediately
  (`status='PROPOSED'`), then calls `risk_manager.check()`, then updates that same row's status after
  execution, then writes to `positions_repo` on a fill. Win/loss counts and deployed capital are
  **not** tracked as running counters anywhere — they're derived from `positions` on read (see
  `dashboard/app.py`), so they can't drift from the orders/positions ledger. `_notify()` (a thin
  try/except wrapper over `core.notifier.send_telegram`, mirroring the `_write_heartbeat`
  fire-and-forget pattern) fires on fills (after the order status DB update, never before) and
  on an unhandled exception escaping `run_once` inside `run_forever`'s loop.

- **`core/status_writer.py`** — writes a small `logs/heartbeat.json` after every cycle (mode,
  halted, halt_reason, symbols, last_ltp only) via atomic write (`tempfile` + `os.replace`). This is
  deliberately *not* a full state snapshot anymore — it exists only so the dashboard can tell "bot is
  alive but quiet" apart from "bot process is dead" (via `updated_at` staleness) and to carry
  process-local LTP data that isn't persisted to the DB. Everything durable (orders, positions, P&L,
  risk events) is read from SQLite instead.

- **`dashboard/app.py`** — read-only FastAPI app. `_build_status_view()` combines
  `read_heartbeat()` (liveness + LTP) with live queries against `core/db/*_repo` (recent orders, open
  positions, win/loss, deployed capital) and `config.settings.load_settings()` (static caps like
  `total_capital_inr`) into the same dict shape the pre-Phase-0 `_render()` function already expects
  — `_render()` itself is untouched. Renders as HTML (`/`) and JSON (`/api/status`). Optional `?key=`
  query-param auth via `DASHBOARD_PASSWORD` env var. Has no direct connection to the orchestrator
  process; reads the DB and heartbeat file independently, so it works even if the bot is on a
  different process (or host, given a shared DB path).

- **`core/auth.py`** — Groww API auth using the API Key + Secret flow (`GROWW_API_KEY` /
  `GROWW_API_SECRET`, resets daily), not the alternative TOTP flow, despite `pyotp` being a
  dependency. The daily reset requires a human to click "Approve" on Groww's own dashboard
  (`https://groww.in/trade-api/api-keys`) before 6 AM IST — there is no API for that click, so
  it can't be automated (see `scripts/groww_key_reminder.py` below).

- **`core/notifier.py`** — best-effort Telegram notifications, never raises (dead network / bad
  token just means a missed message, not a broken trading loop). Two entrypoints sharing one
  HTTP call: `send_telegram(settings, message)` for callers with a `Settings` object
  (`Orchestrator`, `main.py`), `send_telegram_raw(token, chat_id, message)` for callers that
  only have the two raw config values (`RiskManager`, `scripts/groww_key_reminder.py`). No-ops
  silently when `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are unset — both are present but empty
  in `.env.example` and on the droplet today, so this is currently dormant in production.
  Notifies on: bot startup, order fills, and halts (automatic daily-loss-breach and manual) —
  deliberately not on routine risk-rejected orders, which would make the channel too noisy to
  be useful.

- **`scripts/groww_key_reminder.py`** — standalone daily reminder (not imported by anything
  else) that pings Telegram to re-approve the Groww API key. Deliberately independent of the
  bot process, the DB, and Groww auth itself, since it has to keep working even when the thing
  it's warning about — an expired key — is the bot's actual problem. Scheduled via
  `deploy/groww-key-reminder.timer` (systemd, not cron, to stay consistent with how the other
  two services are run) at 9 PM IST the evening before the reset.

- **`main.py`** — entry point. Builds `RiskManager` → broker (`LiveBroker` or `PaperBroker`
  depending on the `--live` flag + `MODE` env var) → strategy → `Orchestrator`, then calls
  `run_forever()`. Paper mode still attempts Groww auth to get real LTP data but degrades gracefully
  (falls back to no live prices) if credentials are missing. Sends a startup notification (mode +
  symbols) right before entering `run_forever()`.

- **`deploy/`** — systemd unit files, checked in for reproducibility. `groww-bot.service` and
  `groww-dashboard.service` are copies of what's actually running on the droplet (deployed
  manually to `/etc/systemd/system/`, not auto-synced from the repo — a `git pull` alone does
  not update them). `groww-key-reminder.{service,timer}` run the daily key-reminder script.

## Known phased plan

This codebase is being developed in phases (Phase 0–6+ as tracked by the user outside this repo).

- **Phase 0 (done)** replaced the in-memory orchestrator/risk_manager state and the full
  `logs/status.json` snapshot with a SQLite-backed layer (`core/db/`) for orders, positions,
  risk_events, and daily summaries — `logs/heartbeat.json` remains, but only for
  process-liveness + live LTP, both of which are intentionally not persisted to the DB. Deployed
  to a DigitalOcean droplet as two systemd services (`groww-bot.service`,
  `groww-dashboard.service`, see `deploy/`).
- **Phase 1 (done)** added the `tests/` pytest suite (risk manager, orchestrator, strategy,
  execution, DB layer) and CI (`.github/workflows/ci.yml`).
- **Phase 2 (done, not yet deployed)** added `core/notifier.py` (Telegram alerts on startup,
  fills, and halts) and `scripts/groww_key_reminder.py` (daily reminder for Groww's manual API
  key re-approval). Merged and tested, but **not yet live on the droplet** — needs a real
  Telegram bot token/chat ID in `.env` and the new systemd units installed before it does
  anything (currently a silent no-op in production, by design).
- **Phases 3+** not yet scoped. Known open item independent of phase work: Groww's live-data
  quote endpoint was returning `403 Access forbidden` as of 2026-08-25, cause not yet confirmed
  (market-hours restriction vs. an account-side Live Data permission gap) — being rechecked
  during NSE trading hours.
