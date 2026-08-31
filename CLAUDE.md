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
  broker-agnostic — they only see price, never account/position state. Crossover detection requires
  the short/long MA gap to exceed `MIN_CROSSOVER_GAP_PCT` (filters sub-tick price noise from being
  read as a trend reversal) and gates new entries with a `COOLDOWN_SECONDS` wall-clock cooldown
  after an exit (`SymbolState.last_exit_time`, `time.monotonic()`) — added in Phase 3 after live
  trading showed the un-gated version whipsawing on flat price action.

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
  try/except wrapper over `core.notifier.send_notification`, mirroring the `_write_heartbeat`
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

- **`core/notifier.py`** — best-effort push notifications via ntfy.sh (a free, no-signup
  publish/subscribe service — `POST https://ntfy.sh/<topic>`, the topic name doubles as the
  password). Chosen over Telegram after the user's Telegram account was blocked by Telegram
  itself from creating new bots — not a technical issue with the bot-notification approach, just
  that provider. Never raises (dead network / unconfigured topic just means a missed message,
  not a broken trading loop). Two entrypoints sharing one HTTP call: `send_notification(settings,
  message)` for callers with a `Settings` object (`Orchestrator`, `main.py`),
  `send_notification_raw(topic, message)` for callers that only have the raw topic string
  (`RiskManager`, `scripts/groww_key_reminder.py`). No-ops silently when `NTFY_TOPIC` is unset.
  Notifies on: bot startup, order fills, and halts (automatic daily-loss-breach and manual) —
  deliberately not on routine risk-rejected orders, which would make the channel too noisy to
  be useful.

- **`scripts/groww_key_reminder.py`** — standalone daily reminder (not imported by anything
  else) that pings ntfy.sh to re-approve the Groww API key. Deliberately independent of the
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
- **Phase 2 (done, deployed)** added `core/notifier.py` (ntfy.sh alerts on startup, fills, and
  halts — switched from an original Telegram design after the user's Telegram account was
  blocked from creating new bots) and `scripts/groww_key_reminder.py` (daily reminder for
  Groww's manual API key re-approval, via `deploy/groww-key-reminder.timer`). Live on the
  droplet with a real `NTFY_TOPIC` configured.
- **Groww's live-data `403 Access forbidden` issue** (present as of 2026-08-25) was resolved on
  2026-08-26 — root cause was the account being on Groww's free tier, which doesn't include Live
  Data access; a paid Trading API subscription (₹499+taxes/month) unlocked it immediately, no
  code changes needed. The bot began placing real (paper) trades against live NSE prices that day.
- **Phase 3 (done)** fixed whipsaw/overtrading discovered on that first live day: within ~15
  minutes the MA(9,21) crossover strategy made 10 trades and hit `MAX_TRADES_PER_DAY`, going idle
  for the rest of the session — every bad entry had a short/long MA gap of ≤0.0008% (sub-tick
  noise, not a real trend). Fixed in `strategies/ma_rsi_strategy.py` with `MIN_CROSSOVER_GAP_PCT`
  (minimum relative MA gap to count as a real crossover) and `COOLDOWN_SECONDS` (minimum
  wall-clock time after an exit before re-entry).
- **Phase 4 (done)** — live-trading readiness audit and fixes, in two rounds:
  - Critical: `LiveBroker.place_order()` was calling the Groww SDK without its required
    `validity`/`exchange`/`product` args (every real order would have raised `TypeError`,
    silently caught and returned as a generic `ERROR` — never reached Groww). Fixed, using
    `PRODUCT_MIS` (matches the strategy's same-session entry/exit design) and `VALIDITY_DAY`.
    Also fixed F&O orders sending `qty` instead of `total_units` (`qty * lot_size`).
  - Added an operator kill-switch (`scripts/halt_bot.py`/`scripts/resume_bot.py`) backed by a
    `daily_summary.halt_source` column (`"MANUAL"` vs `"AUTO"`) and
    `RiskManager.refresh_halt_state()` (called every `check()` and at the top of
    `Orchestrator.run_once()`), so an externally-triggered halt/resume takes effect on the
    running bot within one poll cycle, not just on day rollover. `resume()` refuses to clear an
    `"AUTO"` halt (daily-loss breach, circuit breaker, reconciliation mismatch) — only
    `"MANUAL"` halts can be manually cleared.
  - Added `BaseStrategy.restore_position()` (default no-op, overridden in `MARsiStrategy`),
    called from `main.py` at startup for any already-open position, so a restart no longer
    causes the strategy to think it's flat and risk a duplicate entry.
  - Non-FILLED order results (`ERROR`, `REJECTED`, `PENDING`) now trigger a notification —
    previously only fills were notified.
  - `LiveBroker` re-authenticates and retries exactly once on
    `GrowwAPIAuthenticationException`/`GrowwAPIAuthorisationException`, in `get_ltp()`,
    `place_order()`, and `get_broker_position()`.
  - `Orchestrator` tracks consecutive `run_once()` failures; 5 in a row trips a circuit breaker
    (`RiskManager.halt_circuit_breaker()`, `"AUTO"`-sourced).
  - Startup-only, LIVE-mode-only reconciliation (`main.reconcile_positions()`): compares each
    traded symbol's local open position against `LiveBroker.get_broker_position()`, halting via
    `RiskManager.halt_reconciliation_mismatch()` on any mismatch or fetch failure. Deliberately
    not periodic — no live date yet, larger scope deferred.
  - `RiskManager._halt()` is now a single private helper shared by `manual_halt()`,
    `record_fill()`'s daily-loss branch, `halt_circuit_breaker()`, and
    `halt_reconciliation_mismatch()` — was duplicated inline in two places before this round.
- **Phases 5+** not yet scoped.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
