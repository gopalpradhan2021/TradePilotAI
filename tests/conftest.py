import pytest

from core.db.migrate import run_migrations


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test gets its own throwaway SQLite file with migrations applied,
    so tests can't see each other's rows or the real data/trading.db. Also isolates
    HEARTBEAT_PATH — Orchestrator.run_once() writes a heartbeat on every call, and without
    this every orchestrator test was silently overwriting the real logs/heartbeat.json."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("HEARTBEAT_PATH", str(tmp_path / "test_heartbeat.json"))
    run_migrations()
    yield str(db_path)
