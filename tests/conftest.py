import pytest

from core.db.migrate import run_migrations


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test gets its own throwaway SQLite file with migrations applied,
    so tests can't see each other's rows or the real data/trading.db."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    run_migrations()
    yield str(db_path)
