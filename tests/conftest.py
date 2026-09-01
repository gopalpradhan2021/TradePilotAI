import pytest

from core.db.migrate import run_migrations


@pytest.fixture(autouse=True)
def not_past_square_off_cutoff(monkeypatch):
    """core.risk_manager.check() and core.orchestrator's square-off cadence both call the
    real is_past_square_off_cutoff(), which depends on the real wall-clock IST time — left
    unpatched, any test run after 3:20 PM IST would spuriously fail (found live 2026-09-01).
    Defaults every test to "before cutoff"; tests exercising cutoff-specific behavior
    monkeypatch it back to True themselves, which overrides this for just that test."""
    import core.orchestrator as orchestrator_module
    import core.risk_manager as risk_manager_module
    monkeypatch.setattr(risk_manager_module, "is_past_square_off_cutoff", lambda *a, **k: False)
    monkeypatch.setattr(orchestrator_module, "is_past_square_off_cutoff", lambda *a, **k: False)


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
