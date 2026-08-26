"""
Writes a small JSON heartbeat after each cycle: process liveness (so the
dashboard can tell "no signal" apart from "stopped") plus the latest LTPs,
which are process-local and change too fast to be worth persisting to the
DB. Durable state — orders, positions, P&L, risk events — lives in SQLite
(see core/db/) and is read directly from there, not from this file.
"""
import json
import os
import tempfile
from datetime import datetime, timezone

DEFAULT_HEARTBEAT_PATH = "logs/heartbeat.json"


def _heartbeat_path() -> str:
    # Overridable so scripts/backtest.py can point a replay run at an isolated file
    # instead of clobbering the real bot's liveness heartbeat.
    return os.getenv("HEARTBEAT_PATH", DEFAULT_HEARTBEAT_PATH)


def write_heartbeat(*, mode: str, halted: bool, halt_reason: str,
                     symbols: list[str], last_ltp: dict[str, float | None]) -> None:
    path = _heartbeat_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "halted": halted,
        "halt_reason": halt_reason,
        "symbols": symbols,
        "last_ltp": last_ltp,
    }
    dir_name = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def read_heartbeat() -> dict:
    path = _heartbeat_path()
    if not os.path.exists(path):
        return {
            "updated_at": None,
            "mode": "UNKNOWN",
            "halted": False,
            "halt_reason": "",
            "symbols": [],
            "last_ltp": {},
        }
    with open(path) as f:
        return json.load(f)
