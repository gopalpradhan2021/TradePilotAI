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

HEARTBEAT_PATH = "logs/heartbeat.json"


def write_heartbeat(*, mode: str, halted: bool, halt_reason: str,
                     symbols: list[str], last_ltp: dict[str, float | None]) -> None:
    os.makedirs(os.path.dirname(HEARTBEAT_PATH), exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "halted": halted,
        "halt_reason": halt_reason,
        "symbols": symbols,
        "last_ltp": last_ltp,
    }
    dir_name = os.path.dirname(HEARTBEAT_PATH) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, HEARTBEAT_PATH)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def read_heartbeat() -> dict:
    if not os.path.exists(HEARTBEAT_PATH):
        return {
            "updated_at": None,
            "mode": "UNKNOWN",
            "halted": False,
            "halt_reason": "",
            "symbols": [],
            "last_ltp": {},
        }
    with open(HEARTBEAT_PATH) as f:
        return json.load(f)
