import requests

from config.settings import Settings, RiskConfig
from core import notifier


def make_settings(topic=""):
    return Settings(
        mode="PAPER",
        risk=RiskConfig(
            max_order_value_inr=100_000, max_daily_loss_inr=2_000, max_trades_per_day=10,
            max_position_qty=1_000, price_sanity_band_pct=3.0, total_capital_inr=1_000_000,
            allow_fno=False, allow_fno_index=False, fno_paper_validated=False,
        ),
        ntfy_topic=topic,
        candle_interval="5minute",
    )


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def test_unconfigured_is_a_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append((a, k)) or FakeResponse())

    result = notifier.send_notification(make_settings(topic=""), "hello")

    assert result is False
    assert calls == []


def test_configured_success_calls_ntfy_api(monkeypatch):
    calls = []

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append((url, data, headers, timeout))
        return FakeResponse(status_code=200)

    monkeypatch.setattr(requests, "post", fake_post)

    result = notifier.send_notification(make_settings(topic="my-topic"), "hello world")

    assert result is True
    assert len(calls) == 1
    url, data, headers, timeout = calls[0]
    assert url == "https://ntfy.sh/my-topic"
    assert data == b"hello world"
    assert timeout == notifier._HTTP_TIMEOUT_SEC


def test_network_failure_does_not_raise(monkeypatch):
    def raise_connection_error(*a, **k):
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(requests, "post", raise_connection_error)

    result = notifier.send_notification(make_settings(topic="my-topic"), "hello")

    assert result is False


def test_non_2xx_response_returns_false(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(status_code=500, text="error"))

    result = notifier.send_notification(make_settings(topic="my-topic"), "hello")

    assert result is False


def test_title_header_included_when_provided(monkeypatch):
    calls = []

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append(headers)
        return FakeResponse(status_code=200)

    monkeypatch.setattr(requests, "post", fake_post)

    notifier.send_notification(make_settings(topic="my-topic"), "hello", title="Alert")

    assert calls[0] == {"Title": "Alert"}


def test_send_notification_raw_configured_success(monkeypatch):
    calls = []

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append((url, data))
        return FakeResponse(status_code=200)

    monkeypatch.setattr(requests, "post", fake_post)

    result = notifier.send_notification_raw("topic2", "raw message")

    assert result is True
    assert calls[0][0] == "https://ntfy.sh/topic2"
    assert calls[0][1] == b"raw message"


def test_send_notification_raw_unconfigured_is_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(1) or FakeResponse())

    result = notifier.send_notification_raw("", "raw message")

    assert result is False
    assert calls == []


def test_send_notification_raw_network_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: (_ for _ in ()).throw(requests.Timeout("slow")))

    result = notifier.send_notification_raw("topic2", "raw message")

    assert result is False
