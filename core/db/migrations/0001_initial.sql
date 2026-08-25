CREATE TABLE IF NOT EXISTS orders (
    id                INTEGER PRIMARY KEY,
    idempotency_key   TEXT NOT NULL UNIQUE,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    order_type        TEXT NOT NULL CHECK (order_type IN ('MARKET','LIMIT')),
    segment           TEXT NOT NULL CHECK (segment IN ('CASH','FNO')) DEFAULT 'CASH',
    qty               INTEGER NOT NULL,
    lot_size          INTEGER NOT NULL DEFAULT 1,
    limit_price       REAL,
    expiry_date       TEXT,
    strike_price      REAL,
    option_type       TEXT CHECK (option_type IN ('CE','PE')),
    reason            TEXT NOT NULL DEFAULT '',
    -- status is intentionally not CHECK-constrained: LiveBroker passes through
    -- arbitrary broker status strings. Known values: PROPOSED, BLOCKED, FILLED,
    -- ERROR, plus whatever growwapi returns (e.g. PENDING).
    status            TEXT NOT NULL DEFAULT 'PROPOSED',
    reference_price   REAL,
    broker_order_id   TEXT,
    fill_price        REAL,
    message           TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_symbol_created ON orders(symbol, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY,
    symbol          TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('OPEN','CLOSED')) DEFAULT 'OPEN',
    qty             INTEGER NOT NULL,
    entry_price     REAL NOT NULL,
    entry_order_id  INTEGER NOT NULL REFERENCES orders(id),
    exit_price      REAL,
    exit_order_id   INTEGER REFERENCES orders(id),
    realized_pnl    REAL,
    opened_at       TEXT NOT NULL,
    closed_at       TEXT
);
-- enforces the existing "one open position per symbol" assumption at the DB level
CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_open_symbol
    ON positions(symbol) WHERE status = 'OPEN';

CREATE TABLE IF NOT EXISTS risk_events (
    id                          INTEGER PRIMARY KEY,
    order_id                    INTEGER REFERENCES orders(id),
    symbol                      TEXT,
    event_type                  TEXT NOT NULL
                                 CHECK (event_type IN
                                   ('CHECK_APPROVED','CHECK_REJECTED','HALTED','HALT_CLEARED')),
    reasons                     TEXT,
    reference_price             REAL,
    order_value                 REAL,
    deployed_capital_at_event   REAL,
    trades_today_at_event       INTEGER,
    realized_pnl_today_at_event REAL,
    created_at                  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risk_events_created_at ON risk_events(created_at);

CREATE TABLE IF NOT EXISTS daily_summary (
    trade_date     TEXT PRIMARY KEY,
    trades_count   INTEGER NOT NULL DEFAULT 0,
    realized_pnl   REAL NOT NULL DEFAULT 0,
    halted         INTEGER NOT NULL DEFAULT 0,
    halt_reason    TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
