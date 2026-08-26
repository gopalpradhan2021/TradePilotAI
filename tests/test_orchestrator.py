import time

import core.orchestrator as orchestrator_module
from config.settings import RiskConfig, Settings
from core.db import orders_repo, positions_repo
from core.execution import BrokerPositionFetchError, PaperBroker
from core.models import ProposedOrder, ExecutionResult, Side, OrderType
from core.orchestrator import Orchestrator
from core.risk_manager import RiskManager
from strategies.base_strategy import BaseStrategy


def make_settings(mode="PAPER", **risk_overrides):
    defaults = dict(
        max_order_value_inr=100_000,
        max_daily_loss_inr=2_000,
        max_trades_per_day=10,
        max_position_qty=1_000,
        price_sanity_band_pct=3.0,
        total_capital_inr=1_000_000,
        allow_fno=False,
    )
    defaults.update(risk_overrides)
    return Settings(mode=mode, risk=RiskConfig(**defaults), ntfy_topic="")


class ScriptedStrategy(BaseStrategy):
    """Returns each order in `orders` in sequence (by symbol), then None forever."""

    def __init__(self, orders_by_symbol: dict[str, list[ProposedOrder | None]]):
        self._queues = {k: list(v) for k, v in orders_by_symbol.items()}

    def decide(self, symbol, last_traded_price):
        queue = self._queues.get(symbol, [])
        return queue.pop(0) if queue else None


class FixedPriceBroker(PaperBroker):
    def __init__(self, price: float):
        super().__init__(market_data_client=None)
        self._price = price

    def get_ltp(self, symbol, segment=None):
        return self._price


class NonFillingBroker(FixedPriceBroker):
    """Always returns a fixed non-FILLED ExecutionResult, for testing item-6 notifications."""

    def __init__(self, price: float, status: str, message: str = "simulated"):
        super().__init__(price)
        self._status = status
        self._message = message

    def place_order(self, order, last_traded_price):
        return ExecutionResult(order=order, status=self._status, message=self._message)


class ReconcilingBroker(FixedPriceBroker):
    """A LiveBroker-like stub exposing get_broker_position — PaperBroker deliberately has no
    such method, so tests can also rely on an AttributeError here to prove PAPER mode never
    even attempts a periodic reconciliation call."""

    def __init__(self, price: float, broker_qty_by_symbol: dict[str, int] | None = None,
                 raise_for: set[str] | None = None):
        super().__init__(price)
        self._broker_qty = broker_qty_by_symbol or {}
        self._raise_for = raise_for or set()
        self.position_calls = []

    def get_broker_position(self, symbol):
        self.position_calls.append(symbol)
        if symbol in self._raise_for:
            raise BrokerPositionFetchError(f"fetch failed for {symbol}")
        return {"symbol": symbol, "qty": self._broker_qty.get(symbol, 0)}


class FailingStrategy(BaseStrategy):
    def decide(self, symbol, last_traded_price):
        raise RuntimeError("strategy blew up")


def make_buy_order(symbol="RELIANCE", qty=1):
    return ProposedOrder(symbol=symbol, side=Side.BUY, qty=qty, order_type=OrderType.MARKET)


def make_sell_order(symbol="RELIANCE", qty=1):
    return ProposedOrder(symbol=symbol, side=Side.SELL, qty=qty, order_type=OrderType.MARKET)


def test_approved_buy_order_fills_and_opens_position():
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)
    strategy = ScriptedStrategy({"RELIANCE": [make_buy_order()]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)

    orchestrator.run_once(symbols=["RELIANCE"])

    orders = orders_repo.get_recent_orders(10)
    assert len(orders) == 1
    assert orders[0]["status"] == "FILLED"
    assert orders[0]["fill_price"] == 100.0

    position = positions_repo.get_open_position("RELIANCE")
    assert position is not None
    assert position["qty"] == 1
    assert position["entry_price"] == 100.0

    assert risk_manager._trades_today == 1


def test_rejected_order_is_blocked_and_no_position_opened():
    settings = make_settings(max_trades_per_day=0)
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)
    strategy = ScriptedStrategy({"RELIANCE": [make_buy_order()]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)

    orchestrator.run_once(symbols=["RELIANCE"])

    orders = orders_repo.get_recent_orders(10)
    assert len(orders) == 1
    assert orders[0]["status"] == "BLOCKED"
    assert positions_repo.get_open_position("RELIANCE") is None


def test_sell_closes_position_and_records_pnl():
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)
    strategy = ScriptedStrategy({"RELIANCE": [make_buy_order()]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)
    orchestrator.run_once(symbols=["RELIANCE"])  # opens position at 100

    broker._price = 110.0
    strategy._queues["RELIANCE"] = [make_sell_order()]
    orchestrator.run_once(symbols=["RELIANCE"])  # closes position at 110

    assert positions_repo.get_open_position("RELIANCE") is None
    assert risk_manager._realized_pnl_today == 10.0  # (110 - 100) * 1
    assert risk_manager._trades_today == 2


def test_duplicate_idempotency_key_is_blocked_without_double_processing():
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)
    order = make_buy_order()
    # Same ProposedOrder instance -> same idempotency_key both times.
    strategy = ScriptedStrategy({"RELIANCE": [order, order]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)

    orchestrator.run_once(symbols=["RELIANCE"])
    orchestrator.run_once(symbols=["RELIANCE"])

    # Second attempt must be rejected at the dedup stage, not create a second order row.
    assert len(orders_repo.get_recent_orders(10)) == 1
    assert risk_manager._trades_today == 1


def test_halted_orchestrator_skips_cycle_without_calling_strategy():
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    risk_manager.manual_halt("halted for test")
    broker = FixedPriceBroker(price=100.0)

    calls = []

    class RecordingStrategy(BaseStrategy):
        def decide(self, symbol, last_traded_price):
            calls.append(symbol)
            return None

    orchestrator = Orchestrator(settings, broker, risk_manager, RecordingStrategy())
    orchestrator.run_once(symbols=["RELIANCE"])

    assert calls == []  # strategy never consulted while halted
    assert orders_repo.get_recent_orders(10) == []


def test_strategy_returning_none_does_not_create_order():
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)
    strategy = ScriptedStrategy({"RELIANCE": [None]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)

    orchestrator.run_once(symbols=["RELIANCE"])

    assert orders_repo.get_recent_orders(10) == []


def test_filled_order_triggers_exactly_one_notification(monkeypatch):
    calls = []
    monkeypatch.setattr(orchestrator_module, "send_notification", lambda settings, msg: calls.append(msg) or True)

    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)
    strategy = ScriptedStrategy({"RELIANCE": [make_buy_order()]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)

    orchestrator.run_once(symbols=["RELIANCE"])

    assert len(calls) == 1
    assert "RELIANCE" in calls[0]
    assert "FILLED" in calls[0]


def test_blocked_order_does_not_trigger_notification(monkeypatch):
    calls = []
    monkeypatch.setattr(orchestrator_module, "send_notification", lambda settings, msg: calls.append(msg) or True)

    settings = make_settings(max_trades_per_day=0)
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)
    strategy = ScriptedStrategy({"RELIANCE": [make_buy_order()]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)

    orchestrator.run_once(symbols=["RELIANCE"])

    assert calls == []


def test_external_halt_via_separate_risk_manager_stops_orchestrator():
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)  # the orchestrator's own instance, not halted
    broker = FixedPriceBroker(price=100.0)
    strategy = ScriptedStrategy({"RELIANCE": [make_buy_order()]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)

    # Simulates scripts/halt_bot.py running as a separate process against the same DB.
    external_risk_manager = RiskManager(settings.risk)
    external_risk_manager.manual_halt("external halt")

    orchestrator.run_once(symbols=["RELIANCE"])

    assert orders_repo.get_recent_orders(10) == []
    assert risk_manager.halted is True


def test_error_order_triggers_notification(monkeypatch):
    calls = []
    monkeypatch.setattr(orchestrator_module, "send_notification", lambda settings, msg: calls.append(msg) or True)

    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = NonFillingBroker(price=100.0, status="ERROR", message="simulated failure")
    strategy = ScriptedStrategy({"RELIANCE": [make_buy_order()]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)

    orchestrator.run_once(symbols=["RELIANCE"])

    assert len(calls) == 1
    assert "ERROR" in calls[0]
    assert "RELIANCE" in calls[0]


def test_pending_order_triggers_notification(monkeypatch):
    calls = []
    monkeypatch.setattr(orchestrator_module, "send_notification", lambda settings, msg: calls.append(msg) or True)

    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = NonFillingBroker(price=100.0, status="PENDING", message="resting order")
    strategy = ScriptedStrategy({"RELIANCE": [make_buy_order()]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)

    orchestrator.run_once(symbols=["RELIANCE"])

    assert len(calls) == 1
    assert "PENDING" in calls[0]


def test_run_cycle_resets_counter_on_success():
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)
    strategy = ScriptedStrategy({"RELIANCE": [None]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)
    orchestrator._consecutive_failures = 3

    orchestrator._run_cycle(["RELIANCE"])

    assert orchestrator._consecutive_failures == 0


def test_run_cycle_increments_and_notifies_on_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(orchestrator_module, "send_notification", lambda settings, msg: calls.append(msg) or True)

    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)
    orchestrator = Orchestrator(settings, broker, risk_manager, FailingStrategy())

    orchestrator._run_cycle(["RELIANCE"])

    assert orchestrator._consecutive_failures == 1
    assert len(calls) == 1
    assert risk_manager.halted is False


def test_circuit_breaker_trips_at_threshold(monkeypatch):
    monkeypatch.setattr(orchestrator_module, "send_notification", lambda settings, msg: True)

    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)
    orchestrator = Orchestrator(settings, broker, risk_manager, FailingStrategy())

    for i in range(4):
        orchestrator._run_cycle(["RELIANCE"])
        assert risk_manager.halted is False, f"should not be halted after {i + 1} failures"

    orchestrator._run_cycle(["RELIANCE"])

    assert risk_manager.halted is True
    assert risk_manager.halt_source == "AUTO"


def test_circuit_breaker_resets_after_intervening_success(monkeypatch):
    monkeypatch.setattr(orchestrator_module, "send_notification", lambda settings, msg: True)

    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)
    failing = Orchestrator(settings, broker, risk_manager, FailingStrategy())
    succeeding_strategy = ScriptedStrategy({"RELIANCE": [None]})

    failing._run_cycle(["RELIANCE"])
    failing._run_cycle(["RELIANCE"])
    failing.strategy = succeeding_strategy
    failing._run_cycle(["RELIANCE"])
    failing.strategy = FailingStrategy()
    failing._run_cycle(["RELIANCE"])
    failing._run_cycle(["RELIANCE"])

    assert risk_manager.halted is False
    assert failing._consecutive_failures == 2


# --- periodic reconciliation ---------------------------------------------

def test_periodic_reconciliation_does_not_fire_immediately_after_construction():
    settings = make_settings(mode="LIVE")
    risk_manager = RiskManager(settings.risk)
    broker = ReconcilingBroker(price=100.0, broker_qty_by_symbol={"RELIANCE": 5})  # mismatch
    strategy = ScriptedStrategy({"RELIANCE": [None]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)

    orchestrator.run_once(symbols=["RELIANCE"])

    assert broker.position_calls == []
    assert risk_manager.halted is False


def test_periodic_reconciliation_fires_after_interval_elapses_and_halts_on_mismatch():
    settings = make_settings(mode="LIVE")
    risk_manager = RiskManager(settings.risk)
    broker = ReconcilingBroker(price=100.0, broker_qty_by_symbol={"RELIANCE": 5})  # local=0
    strategy = ScriptedStrategy({"RELIANCE": [None]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)
    orchestrator._last_reconcile_time = time.monotonic() - 9999

    orchestrator.run_once(symbols=["RELIANCE"])

    assert broker.position_calls == ["RELIANCE"]
    assert risk_manager.halted is True
    assert risk_manager.halt_source == "AUTO"


def test_periodic_reconciliation_matching_position_does_not_halt():
    settings = make_settings(mode="LIVE")
    risk_manager = RiskManager(settings.risk)
    broker = ReconcilingBroker(price=100.0, broker_qty_by_symbol={"RELIANCE": 0})  # matches local=0
    strategy = ScriptedStrategy({"RELIANCE": [None]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)
    orchestrator._last_reconcile_time = time.monotonic() - 9999

    orchestrator.run_once(symbols=["RELIANCE"])

    assert broker.position_calls == ["RELIANCE"]
    assert risk_manager.halted is False


def test_periodic_reconciliation_skipped_entirely_in_paper_mode():
    settings = make_settings(mode="PAPER")
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)  # no get_broker_position at all
    strategy = ScriptedStrategy({"RELIANCE": [None]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)
    orchestrator._last_reconcile_time = time.monotonic() - 9999

    orchestrator.run_once(symbols=["RELIANCE"])  # must not raise AttributeError

    assert risk_manager.halted is False


def test_periodic_reconciliation_halt_skips_rest_of_cycle():
    settings = make_settings(mode="LIVE")
    risk_manager = RiskManager(settings.risk)
    broker = ReconcilingBroker(price=100.0, broker_qty_by_symbol={"RELIANCE": 5})
    strategy = ScriptedStrategy({"RELIANCE": [make_buy_order()]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)
    orchestrator._last_reconcile_time = time.monotonic() - 9999

    orchestrator.run_once(symbols=["RELIANCE"])

    # Reconciliation halted mid-cycle — the strategy's queued order must never have been
    # proposed/placed on top of a known-bad local picture.
    assert orders_repo.get_recent_orders(10) == []


def test_periodic_reconciliation_does_not_refire_within_the_same_interval():
    settings = make_settings(mode="LIVE")
    risk_manager = RiskManager(settings.risk)
    broker = ReconcilingBroker(price=100.0, broker_qty_by_symbol={"RELIANCE": 0})
    strategy = ScriptedStrategy({"RELIANCE": [None, None]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)
    orchestrator._last_reconcile_time = time.monotonic() - 9999

    orchestrator.run_once(symbols=["RELIANCE"])
    orchestrator.run_once(symbols=["RELIANCE"])

    assert broker.position_calls == ["RELIANCE"]  # only the first call reconciled
