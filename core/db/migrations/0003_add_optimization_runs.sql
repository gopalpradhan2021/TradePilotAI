CREATE TABLE IF NOT EXISTS optimization_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    in_sample_start TEXT NOT NULL,
    in_sample_end TEXT NOT NULL,
    out_sample_start TEXT NOT NULL,
    out_sample_end TEXT NOT NULL,
    baseline_json TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    combinations_tried INTEGER NOT NULL,
    notes TEXT NOT NULL DEFAULT ''
);
