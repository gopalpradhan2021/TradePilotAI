import json
from datetime import datetime, timezone

from core.db.connection import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_run(*, symbol: str, in_sample_start: str, in_sample_end: str,
                out_sample_start: str, out_sample_end: str, baseline: dict,
                candidates: list[dict], combinations_tried: int, notes: str = "") -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO optimization_runs (
                run_at, symbol, in_sample_start, in_sample_end, out_sample_start, out_sample_end,
                baseline_json, candidates_json, combinations_tried, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (_now(), symbol, in_sample_start, in_sample_end, out_sample_start, out_sample_end,
             json.dumps(baseline), json.dumps(candidates), combinations_tried, notes),
        )
        conn.commit()
        return cur.lastrowid


def get_recent_runs(limit: int = 10) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM optimization_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        runs = []
        for row in rows:
            run = dict(row)
            run["baseline"] = json.loads(run.pop("baseline_json"))
            run["candidates"] = json.loads(run.pop("candidates_json"))
            runs.append(run)
        return runs
