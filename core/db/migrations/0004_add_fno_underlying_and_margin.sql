-- FNO support: `symbol` on orders/positions is now the exact per-contract broker
-- trading_symbol for options (e.g. "NIFTY2690122000CE"), not the underlying — these
-- columns carry the underlying separately and the real SPAN+exposure margin used,
-- since neither is derivable from price*qty the way CASH's notional value is.
ALTER TABLE orders ADD COLUMN underlying_symbol TEXT;
ALTER TABLE orders ADD COLUMN margin_used REAL;

ALTER TABLE positions ADD COLUMN underlying_symbol TEXT;
ALTER TABLE positions ADD COLUMN margin_used REAL;
-- positions previously had no segment column at all (only orders did) — needed so
-- get_deployed_capital() can branch CASH (qty*entry_price) vs FNO (margin_used).
ALTER TABLE positions ADD COLUMN segment TEXT NOT NULL DEFAULT 'CASH';
