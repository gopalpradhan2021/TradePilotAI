"""
Covers the Start/Stop dashboard control: the trading halt/resume half (no special privilege
needed) and the bot-process start/stop half (needs a one-time sudoers rule the operator sets
up separately — these tests mock subprocess.run so they never touch a real systemctl).
"""
import os

os.environ.setdefault("DASHBOARD_PASSWORD", "test-password-123")
os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret-key-not-for-production")

import subprocess
from unittest.mock import MagicMock

from starlette.testclient import TestClient

import dashboard.app as dashboard_module
from dashboard.app import app, _failed_logins, _bot_process_status, _control_bot_process


def make_client():
    _failed_logins.clear()
    client = TestClient(app)
    client.post("/login", data={"password": os.environ["DASHBOARD_PASSWORD"]})
    return client


def _fake_run(returncode, stdout="", stderr=""):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def test_bot_process_status_returns_stripped_stdout(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_run(0, stdout="active\n"))
    assert _bot_process_status() == "active"


def test_bot_process_status_unknown_on_exception(monkeypatch):
    def raise_exc(*a, **k):
        raise OSError("systemctl not found")
    monkeypatch.setattr(subprocess, "run", raise_exc)
    assert _bot_process_status() == "unknown"


def test_control_bot_process_success(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_run(0))
    ok, err = _control_bot_process("start")
    assert ok is True
    assert err is None


def test_control_bot_process_failure_without_sudo_configured(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _fake_run(1, stderr="sudo: a password is required"),
    )
    ok, err = _control_bot_process("start")
    assert ok is False
    assert "password" in err


def test_api_bot_stop_halts_trading_even_if_process_control_fails(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_run(1, stderr="no sudo rule"))
    client = make_client()

    response = client.post("/api/bot/stop")

    assert response.status_code == 200
    body = response.json()
    assert body["halted"] is True
    assert body["halt_source"] == "MANUAL"
    assert body["process_stopped"] is False
    assert body["process_error"]


def test_api_bot_stop_reports_process_success(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_run(0))
    client = make_client()

    response = client.post("/api/bot/stop")

    body = response.json()
    assert body["process_stopped"] is True
    assert body["process_error"] is None


def test_api_bot_start_resumes_a_manual_halt(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_run(0))
    client = make_client()
    client.post("/api/bot/stop")  # manual halt first

    response = client.post("/api/bot/start")

    body = response.json()
    assert body["halted"] is False
    assert body["trade_error"] is None
    assert body["process_started"] is True


def test_api_bot_start_reports_trade_error_without_failing_the_request(monkeypatch):
    """An AUTO halt (daily loss, circuit breaker, reconciliation) must not be clearable via
    Start — resume() correctly refuses, and that shows up as trade_error in the response
    rather than the request itself failing, since the process side may still have succeeded."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_run(0))
    client = make_client()

    rm = dashboard_module._risk_manager()
    rm.halt_circuit_breaker(5)

    response = client.post("/api/bot/start")

    assert response.status_code == 200
    body = response.json()
    assert body["trade_error"] is not None
    assert body["halted"] is True
