"""
SQLite connection helper. Opens a short-lived connection per call rather than
sharing one across threads/processes — the orchestrator polls every few
seconds and the dashboard serves occasional requests from a threadpool, so
connection-open overhead is negligible and this sidesteps sqlite3's
cross-thread sharing caveats entirely.
"""
import os
import sqlite3
from contextlib import contextmanager

from dotenv import load_dotenv

load_dotenv()

DEFAULT_DB_PATH = "data/trading.db"


def _db_path() -> str:
    return os.getenv("DATABASE_PATH", DEFAULT_DB_PATH)


@contextmanager
def get_connection():
    path = _db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
    finally:
        conn.close()
