import requests

from config.settings import Settings, RiskConfig
from core import notifier


def make_settings(token="", chat_id=""):
    return Settings(
        mode="PAPER",
        risk=RiskConfig(
            max_order_value_inr=100_000, max_daily_loss_inr=2_000, max_trades_per_day=10,
            max_position_qty=1_000, price_sanity_band_pct=3.0, total_capital_inr=1_000_000,
            allow_fno=False,
        ),
        telegram_bot_token=token,
        telegram_chat_id=chat_id,
    )


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def test_unconfigured_is_a_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append((a, k)) or FakeResponse())

    result = notifier.send_telegram(make_settings(token="", chat_id=""), "hello")

    assert result is False
    assert calls == []


def test_configured_success_calls_telegram_api(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json, timeout))
        return FakeResponse(status_code=200)

    monkeypatch.setattr(requests, "post", fake_post)

    result = notifier.send_telegram(make_settings(token="TOK", chat_id="CHAT"), "hello world")

    assert result is True
    assert len(calls) == 1
    url, json_body, timeout = calls[0]
    assert url == "https://api.telegram.org/botTOK/sendMessage"
    assert json_body == {"chat_id": "CHAT", "text": "hello world"}
    assert timeout == notifier._HTTP_TIMEOUT_SEC


def test_network_failure_does_not_raise(monkeypatch):
    def raise_connection_error(*a, **k):
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(requests, "post", raise_connection_error)

    result = notifier.send_telegram(make_settings(token="TOK", chat_id="CHAT"), "hello")

    assert result is False


def test_non_2xx_response_returns_false(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(status_code=401, text="Unauthorized"))

    result = notifier.send_telegram(make_settings(token="BAD", chat_id="CHAT"), "hello")

    assert result is False


def test_send_telegram_raw_configured_success(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json, timeout))
        return FakeResponse(status_code=200)

    monkeypatch.setattr(requests, "post", fake_post)

    result = notifier.send_telegram_raw("TOK2", "CHAT2", "raw message")

    assert result is True
    assert calls[0][0] == "https://api.telegram.org/botTOK2/sendMessage"
    assert calls[0][1] == {"chat_id": "CHAT2", "text": "raw message"}


def test_send_telegram_raw_unconfigured_is_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(1) or FakeResponse())

    result = notifier.send_telegram_raw("", "", "raw message")

    assert result is False
    assert calls == []


def test_send_telegram_raw_network_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: (_ for _ in ()).throw(requests.Timeout("slow")))

    result = notifier.send_telegram_raw("TOK", "CHAT", "raw message")

    assert result is False
