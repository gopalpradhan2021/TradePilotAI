-- Real transaction-cost model (core/cost_model.py) for CASH-segment intraday orders —
-- brokerage, STT, exchange/SEBI charges, stamp duty, GST. `orders.charges` is the
-- per-order-leg cost (computed once at fill time); `positions.entry_charges`/
-- `exit_charges` mirror the entry/exit legs' costs so realized_pnl can be reported
-- net of real trading costs. Existing CLOSED positions predate this model and keep
-- their (gross) realized_pnl — not retroactively recomputed.
ALTER TABLE orders ADD COLUMN charges REAL;

ALTER TABLE positions ADD COLUMN entry_charges REAL NOT NULL DEFAULT 0.0;
ALTER TABLE positions ADD COLUMN exit_charges REAL NOT NULL DEFAULT 0.0;
