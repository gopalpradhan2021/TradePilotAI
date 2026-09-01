import logging
import time
from datetime import date, datetime

import core.orchestrator as orchestrator_module
from config.settings import RiskConfig, Settings
from core.cost_model import calculate_order_charges, calculate_square_off_penalty
from core.db import orders_repo, positions_repo
from core.execution import BrokerPositionFetchError, PaperBroker
from core.models import OptionChainSnapshot, ProposedOrder, ExecutionResult, Segment, Side, OrderType
from core.orchestrator import Orchestrator
from core.position_sizing import calculate_entry_qty
from core.reconciliation import reconcile_orphaned_fills
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
        allow_fno_index=False,
        fno_paper_validated=False,
    )
    defaults.update(risk_overrides)
    return Settings(mode=mode, risk=RiskConfig(**defaults), ntfy_topic="", candle_interval="5minute")


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

    def get_broker_position(self, symbol, segment=None):
        self.position_calls.append(symbol)
        if symbol in self._raise_for:
            raise BrokerPositionFetchError(f"fetch failed for {symbol}")
        return {"symbol": symbol, "qty": self._broker_qty.get(symbol, 0)}


class FailingStrategy(BaseStrategy):
    def decide(self, symbol, last_traded_price):
        raise RuntimeError("strategy blew up")


class CandleAwareStrategy(BaseStrategy):
    """Declares candle requirements and records what update_candles() was called with —
    for testing Orchestrator._maybe_update_candles()'s cadence/wiring in isolation from any
    real indicator math."""

    def __init__(self, interval="5minute", lookback=40):
        self._req = (interval, lookback)
        self.update_calls = []

    def decide(self, symbol, last_traded_price):
        return None

    def get_candle_requirements(self):
        return self._req

    def update_candles(self, symbol, candles):
        self.update_calls.append((symbol, candles))


class CandleBroker(FixedPriceBroker):
    def __init__(self, price, candles_by_symbol=None):
        super().__init__(price)
        self._candles = candles_by_symbol or {}
        self.candle_calls = []

    def get_recent_candles(self, symbol, interval, lookback_bars):
        self.candle_calls.append((symbol, interval, lookback_bars))
        return self._candles.get(symbol)


def make_buy_order(symbol="RELIANCE", qty=1):
    return ProposedOrder(symbol=symbol, side=Side.BUY, qty=qty, order_type=OrderType.MARKET)


def make_sell_order(symbol="RELIANCE", qty=1):
    return ProposedOrder(symbol=symbol, side=Side.SELL, qty=qty, order_type=OrderType.MARKET)


def test_approved_buy_order_fills_and_opens_position():
    # max_order_value_inr=100 pins resized qty to exactly 1 at price 100.0, keeping this
    # test's assertions about qty==1 valid under capital-aware sizing (core/position_sizing.py).
    settings = make_settings(max_order_value_inr=100)
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


def test_crash_between_order_fill_and_position_open_leaves_orphaned_fill_recoverable(monkeypatch):
    """Reproduces the confirmed live bug: a process kill (or any crash) landing between
    orders_repo.update_order_status(FILLED) and positions_repo.open_position() inside
    _handle_proposed_order() — each call opens its own short-lived connection/transaction
    (core/db/connection.py), so the two writes aren't atomic. Simulated here by making
    open_position() itself raise, which leaves exactly the same DB state a real kill would:
    the order row says FILLED, but no position and no risk_manager.record_fill() ever
    happened. core/reconciliation.py's reconcile_orphaned_fills() must recover it."""
    real_open_position = orchestrator_module.positions_repo.open_position

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash between update_order_status and open_position")
    monkeypatch.setattr(orchestrator_module.positions_repo, "open_position", boom)

    settings = make_settings(max_order_value_inr=100)
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)
    strategy = ScriptedStrategy({"RELIANCE": [make_buy_order()]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)

    try:
        orchestrator.run_once(symbols=["RELIANCE"])
    except RuntimeError:
        pass  # the simulated crash — a real process kill wouldn't even unwind this far

    orders = orders_repo.get_recent_orders(10)
    assert orders[0]["status"] == "FILLED"  # the order-status write already landed
    assert positions_repo.get_open_position("RELIANCE") is None  # but the position never did
    assert risk_manager._trades_today == 0  # record_fill() never reached either

    # Restore the real open_position() — reconciliation runs post-crash, after the process
    # (and whatever induced the crash) has restarted clean.
    monkeypatch.setattr(orchestrator_module.positions_repo, "open_position", real_open_position)
    reconcile_orphaned_fills(risk_manager, logger=logging.getLogger("test"))

    position = positions_repo.get_open_position("RELIANCE")
    assert position is not None
    assert position["qty"] == 1
    assert position["entry_price"] == 100.0
    assert risk_manager._trades_today == 1
    assert risk_manager.halted is False


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
    # max_order_value_inr=150 pins resized BUY qty to exactly 1 at price 100.0 (150 // 100 == 1),
    # matching this test's hand-computed expected P&L/charges basis below, while staying above
    # the SELL leg's own notional value (110.0 * 1) so the exit isn't rejected by the same cap.
    settings = make_settings(max_order_value_inr=150)
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)
    strategy = ScriptedStrategy({"RELIANCE": [make_buy_order()]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)
    orchestrator.run_once(symbols=["RELIANCE"])  # opens position at 100

    broker._price = 110.0
    strategy._queues["RELIANCE"] = [make_sell_order()]
    orchestrator.run_once(symbols=["RELIANCE"])  # closes position at 110

    # realized_pnl is net of real transaction costs (core/cost_model.py), not just the raw
    # (110 - 100) * 1 price move — compute the expected net value the same way production does.
    entry_charges = calculate_order_charges(100.0, Side.BUY)
    exit_charges = calculate_order_charges(110.0, Side.SELL)
    expected_net_pnl = round((110.0 - 100.0) * 1 - entry_charges - exit_charges, 2)

    assert positions_repo.get_open_position("RELIANCE") is None
    assert risk_manager._realized_pnl_today == expected_net_pnl
    assert risk_manager._trades_today == 2


def test_cash_order_charges_are_persisted_on_order_and_position():
    # max_order_value_inr=100 pins resized qty to exactly 1, matching expected_charges'
    # trade-value basis (100.0 * 1) below.
    settings = make_settings(max_order_value_inr=100)
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)
    strategy = ScriptedStrategy({"RELIANCE": [make_buy_order()]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)

    orchestrator.run_once(symbols=["RELIANCE"])

    expected_charges = calculate_order_charges(100.0, Side.BUY)
    orders = orders_repo.get_recent_orders(10)
    assert orders[0]["charges"] == expected_charges

    position = positions_repo.get_open_position("RELIANCE")
    assert position["entry_charges"] == expected_charges


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


# --- stale market-data protection ----------------------------------------

def test_fresh_price_not_flagged_stale(monkeypatch):
    monkeypatch.setattr(orchestrator_module, "is_market_open", lambda: True)
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)

    calls = []

    class RecordingStrategy(BaseStrategy):
        def decide(self, symbol, last_traded_price):
            calls.append(last_traded_price)
            return None

    orchestrator = Orchestrator(settings, broker, risk_manager, RecordingStrategy())

    orchestrator.run_once(symbols=["RELIANCE"])

    assert calls == [100.0]  # strategy was consulted — not treated as stale


def test_unchanged_price_not_flagged_before_threshold(monkeypatch):
    monkeypatch.setattr(orchestrator_module, "is_market_open", lambda: True)
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)
    strategy = ScriptedStrategy({"RELIANCE": [None]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)
    # Same price "first seen" only 10s ago — well under the 120s threshold.
    orchestrator._price_freshness["RELIANCE"] = (100.0, time.monotonic() - 10)

    assert orchestrator._is_stale("RELIANCE", 100.0) is False


def test_unchanged_price_flagged_stale_past_threshold_during_market_hours(monkeypatch):
    monkeypatch.setattr(orchestrator_module, "is_market_open", lambda: True)
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)
    orchestrator = Orchestrator(settings, broker, risk_manager, ScriptedStrategy({}))
    orchestrator._price_freshness["RELIANCE"] = (100.0, time.monotonic() - 9999)

    assert orchestrator._is_stale("RELIANCE", 100.0) is True


def test_unchanged_price_not_flagged_outside_market_hours(monkeypatch):
    monkeypatch.setattr(orchestrator_module, "is_market_open", lambda: False)
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)
    orchestrator = Orchestrator(settings, broker, risk_manager, ScriptedStrategy({}))
    orchestrator._price_freshness["RELIANCE"] = (100.0, time.monotonic() - 9999)

    assert orchestrator._is_stale("RELIANCE", 100.0) is False


def test_price_change_resets_staleness_clock(monkeypatch):
    monkeypatch.setattr(orchestrator_module, "is_market_open", lambda: True)
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)
    orchestrator = Orchestrator(settings, broker, risk_manager, ScriptedStrategy({}))
    orchestrator._price_freshness["RELIANCE"] = (100.0, time.monotonic() - 9999)

    result = orchestrator._is_stale("RELIANCE", 101.0)  # price actually moved

    assert result is False
    assert orchestrator._price_freshness["RELIANCE"][0] == 101.0


def test_none_ltp_never_flagged_stale(monkeypatch):
    monkeypatch.setattr(orchestrator_module, "is_market_open", lambda: True)
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)
    orchestrator = Orchestrator(settings, broker, risk_manager, ScriptedStrategy({}))

    assert orchestrator._is_stale("RELIANCE", None) is False


def test_stale_price_skips_strategy_call(monkeypatch):
    monkeypatch.setattr(orchestrator_module, "is_market_open", lambda: True)
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)

    calls = []

    class RecordingStrategy(BaseStrategy):
        def decide(self, symbol, last_traded_price):
            calls.append(symbol)
            return None

    orchestrator = Orchestrator(settings, broker, risk_manager, RecordingStrategy())
    orchestrator._price_freshness["RELIANCE"] = (100.0, time.monotonic() - 9999)

    orchestrator.run_once(symbols=["RELIANCE"])

    assert calls == []  # strategy never consulted on a stale price


def test_stale_notification_fires_once_not_every_cycle(monkeypatch):
    monkeypatch.setattr(orchestrator_module, "is_market_open", lambda: True)
    calls = []
    monkeypatch.setattr(orchestrator_module, "send_notification", lambda settings, msg: calls.append(msg) or True)

    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)
    orchestrator = Orchestrator(settings, broker, risk_manager, ScriptedStrategy({}))
    orchestrator._price_freshness["RELIANCE"] = (100.0, time.monotonic() - 9999)

    orchestrator.run_once(symbols=["RELIANCE"])
    orchestrator.run_once(symbols=["RELIANCE"])
    orchestrator.run_once(symbols=["RELIANCE"])

    assert len(calls) == 1
    assert "stale" in calls[0].lower()


# --- F&O cycle (Phase B): run_once_fno / _maybe_run_fno_cycle -------------

def make_fno_order(symbol="NIFTY26SEP24000CE", qty=1, lot_size=75):
    return ProposedOrder(
        symbol=symbol, underlying_symbol="NIFTY", side=Side.BUY, qty=qty,
        order_type=OrderType.MARKET, segment=Segment.FNO, lot_size=lot_size,
    )


def make_chain(underlying="NIFTY", underlying_ltp=24000.0, strikes=None):
    return OptionChainSnapshot(
        underlying=underlying, underlying_ltp=underlying_ltp, expiry_date=date(2026, 9, 1),
        fetched_at=datetime(2026, 8, 28, 10, 0), strikes=strikes or [],
    )


def make_matching_strike(trading_symbol="NIFTY26SEP24000CE", strike=24000.0, ltp=200.0):
    """A StrikeQuote whose CE trading_symbol matches make_fno_order()'s default symbol —
    lets tests exercise chain.find_quote()'s contract-premium lookup realistically."""
    from core.models import OptionGreeks, OptionQuote, StrikeQuote
    return StrikeQuote(
        strike=strike,
        ce=OptionQuote(
            trading_symbol=trading_symbol, ltp=ltp, open_interest=1000, volume=100,
            greeks=OptionGreeks(delta=0.5, gamma=0.0, theta=0.0, vega=0.0, rho=0.0, iv=20.0),
        ),
        pe=None,
    )


class ScriptedFnoStrategy(BaseStrategy):
    """Returns each order in `orders` in sequence (by underlying), then None forever."""

    def __init__(self, orders_by_underlying: dict[str, list[ProposedOrder | None]]):
        self._queues = {k: list(v) for k, v in orders_by_underlying.items()}
        self.decide_fno_calls = []

    def decide_fno(self, underlying, chain):
        self.decide_fno_calls.append((underlying, chain))
        queue = self._queues.get(underlying, [])
        return queue.pop(0) if queue else None


class StubFnoMarketData:
    def __init__(self, chain_by_underlying: dict[str, OptionChainSnapshot | None]):
        self._chains = chain_by_underlying
        self.calls = []

    def get_chain(self, underlying):
        self.calls.append(underlying)
        return self._chains.get(underlying)


def test_fno_buy_order_flows_through_the_same_risk_and_execution_pipeline_as_cash():
    settings = make_settings(allow_fno=True, allow_fno_index=True)
    risk_manager = RiskManager(settings.risk, mode="PAPER")  # PAPER: no margin_provider needed
    broker = FixedPriceBroker(price=200.0)
    fno_strategy = ScriptedFnoStrategy({"NIFTY": [make_fno_order()]})
    chain = make_chain(strikes=[make_matching_strike(ltp=200.0)])
    fno_market_data = StubFnoMarketData({"NIFTY": chain})
    orchestrator = Orchestrator(settings, broker, risk_manager, ScriptedStrategy({}),
                                 fno_strategy=fno_strategy, fno_market_data=fno_market_data)

    orchestrator.run_once_fno(["NIFTY"])

    assert fno_market_data.calls == ["NIFTY"]
    orders = orders_repo.get_recent_orders(10)
    assert len(orders) == 1
    assert orders[0]["status"] == "FILLED"
    assert orders[0]["fill_price"] == 200.0  # the contract's own premium, not underlying_ltp
    assert orders[0]["segment"] == "FNO"

    position = positions_repo.get_open_position("NIFTY26SEP24000CE")
    assert position is not None
    assert position["segment"] == "FNO"
    assert position["entry_price"] == 200.0


def test_fno_order_has_no_charges_computed():
    # core/cost_model.py is CASH-only — FNO has a materially different fee structure and
    # isn't modeled, so an FNO order's `charges` column stays None, not 0.0.
    settings = make_settings(allow_fno=True, allow_fno_index=True)
    risk_manager = RiskManager(settings.risk, mode="PAPER")
    broker = FixedPriceBroker(price=200.0)
    fno_strategy = ScriptedFnoStrategy({"NIFTY": [make_fno_order()]})
    chain = make_chain(strikes=[make_matching_strike(ltp=200.0)])
    fno_market_data = StubFnoMarketData({"NIFTY": chain})
    orchestrator = Orchestrator(settings, broker, risk_manager, ScriptedStrategy({}),
                                 fno_strategy=fno_strategy, fno_market_data=fno_market_data)

    orchestrator.run_once_fno(["NIFTY"])

    orders = orders_repo.get_recent_orders(10)
    assert orders[0]["charges"] is None


def test_fno_order_rejected_by_risk_manager_does_not_open_a_position():
    # allow_fno_index left False -> risk_manager.check() rejects every FNO order, proving
    # decide_fno()'s output still goes through the same gate CASH orders do, not a bypass.
    settings = make_settings(allow_fno=False)
    risk_manager = RiskManager(settings.risk, mode="PAPER")
    broker = FixedPriceBroker(price=200.0)
    fno_strategy = ScriptedFnoStrategy({"NIFTY": [make_fno_order()]})
    fno_market_data = StubFnoMarketData({"NIFTY": make_chain()})
    orchestrator = Orchestrator(settings, broker, risk_manager, ScriptedStrategy({}),
                                 fno_strategy=fno_strategy, fno_market_data=fno_market_data)

    orchestrator.run_once_fno(["NIFTY"])

    orders = orders_repo.get_recent_orders(10)
    assert len(orders) == 1
    assert orders[0]["status"] == "BLOCKED"
    assert positions_repo.get_open_position("NIFTY26SEP24000CE") is None


def test_run_once_fno_skips_underlyings_with_no_chain_available():
    settings = make_settings(allow_fno=True, allow_fno_index=True)
    risk_manager = RiskManager(settings.risk, mode="PAPER")
    broker = FixedPriceBroker(price=200.0)
    fno_strategy = ScriptedFnoStrategy({"NIFTY": [make_fno_order()]})
    fno_market_data = StubFnoMarketData({"NIFTY": None})  # chain fetch "failed"
    orchestrator = Orchestrator(settings, broker, risk_manager, ScriptedStrategy({}),
                                 fno_strategy=fno_strategy, fno_market_data=fno_market_data)

    orchestrator.run_once_fno(["NIFTY"])

    assert fno_strategy.decide_fno_calls == []  # never even consulted without a chain
    assert orders_repo.get_recent_orders(10) == []


def test_fno_cycle_is_a_no_op_when_no_fno_strategy_configured():
    settings = make_settings()
    risk_manager = RiskManager(settings.risk, mode="PAPER")
    broker = FixedPriceBroker(price=100.0)
    orchestrator = Orchestrator(settings, broker, risk_manager, ScriptedStrategy({}))

    # Should not raise even though fno_strategy/fno_market_data are both None
    orchestrator.run_once(symbols=[], fno_underlyings=["NIFTY"])


def test_fno_cycle_does_not_fire_immediately_after_construction():
    # Mirrors _maybe_reconcile's own deliberate design: the clock starts at construction,
    # so the very first cycle right after startup doesn't fire either.
    settings = make_settings(allow_fno=True, allow_fno_index=True)
    risk_manager = RiskManager(settings.risk, mode="PAPER")
    broker = FixedPriceBroker(price=200.0)
    fno_strategy = ScriptedFnoStrategy({})
    fno_market_data = StubFnoMarketData({"NIFTY": make_chain()})
    orchestrator = Orchestrator(settings, broker, risk_manager, ScriptedStrategy({}),
                                 fno_strategy=fno_strategy, fno_market_data=fno_market_data)

    orchestrator.run_once(symbols=[], fno_underlyings=["NIFTY"])

    assert fno_market_data.calls == []


def test_fno_cycle_gated_to_at_most_once_per_interval():
    settings = make_settings(allow_fno=True, allow_fno_index=True)
    risk_manager = RiskManager(settings.risk, mode="PAPER")
    broker = FixedPriceBroker(price=200.0)
    fno_strategy = ScriptedFnoStrategy({})
    fno_market_data = StubFnoMarketData({"NIFTY": make_chain()})
    orchestrator = Orchestrator(settings, broker, risk_manager, ScriptedStrategy({}),
                                 fno_strategy=fno_strategy, fno_market_data=fno_market_data)
    orchestrator._last_fno_cycle_time = time.monotonic() - 9999

    orchestrator.run_once(symbols=[], fno_underlyings=["NIFTY"])
    orchestrator.run_once(symbols=[], fno_underlyings=["NIFTY"])
    orchestrator.run_once(symbols=[], fno_underlyings=["NIFTY"])

    assert len(fno_market_data.calls) == 1  # only the first call actually ran the FNO cycle


def test_fno_cycle_fires_again_after_interval_elapses():
    settings = make_settings(allow_fno=True, allow_fno_index=True)
    risk_manager = RiskManager(settings.risk, mode="PAPER")
    broker = FixedPriceBroker(price=200.0)
    fno_strategy = ScriptedFnoStrategy({})
    fno_market_data = StubFnoMarketData({"NIFTY": make_chain()})
    orchestrator = Orchestrator(settings, broker, risk_manager, ScriptedStrategy({}),
                                 fno_strategy=fno_strategy, fno_market_data=fno_market_data)
    orchestrator._last_fno_cycle_time = time.monotonic() - 9999

    orchestrator.run_once(symbols=[], fno_underlyings=["NIFTY"])

    assert len(fno_market_data.calls) == 1


# --- candle-fetch cadence (candle-based MA/RSI redesign) -------------------

def test_candle_update_does_not_fire_immediately_after_construction():
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    strategy = CandleAwareStrategy()
    broker = CandleBroker(price=100.0, candles_by_symbol={"RELIANCE": [{"close": 100.0}]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)

    orchestrator.run_once(symbols=["RELIANCE"])

    assert broker.candle_calls == []
    assert strategy.update_calls == []


def test_candle_update_gated_to_at_most_once_per_interval():
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    strategy = CandleAwareStrategy()
    broker = CandleBroker(price=100.0, candles_by_symbol={"RELIANCE": [{"close": 100.0}]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)
    orchestrator._last_candle_fetch_time = time.monotonic() - 9999

    orchestrator.run_once(symbols=["RELIANCE"])
    orchestrator.run_once(symbols=["RELIANCE"])
    orchestrator.run_once(symbols=["RELIANCE"])

    assert len(broker.candle_calls) == 1  # only the first call actually fetched candles


def test_candle_update_fires_again_after_interval_elapses():
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    strategy = CandleAwareStrategy()
    broker = CandleBroker(price=100.0, candles_by_symbol={"RELIANCE": [{"close": 100.0}]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)
    orchestrator._last_candle_fetch_time = time.monotonic() - 9999

    orchestrator.run_once(symbols=["RELIANCE"])

    assert len(broker.candle_calls) == 1


def test_candle_update_is_a_noop_when_strategy_has_no_candle_requirements():
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    strategy = ScriptedStrategy({"RELIANCE": [None]})  # get_candle_requirements() -> None
    broker = CandleBroker(price=100.0, candles_by_symbol={"RELIANCE": [{"close": 100.0}]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)
    orchestrator._last_candle_fetch_time = time.monotonic() - 9999

    orchestrator.run_once(symbols=["RELIANCE"])

    assert broker.candle_calls == []


def test_candle_update_calls_strategy_update_candles_with_fetched_data():
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    strategy = CandleAwareStrategy(interval="15minute", lookback=30)
    candles = [{"close": 100.0}, {"close": 101.0}]
    broker = CandleBroker(price=100.0, candles_by_symbol={"RELIANCE": candles})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)
    orchestrator._last_candle_fetch_time = time.monotonic() - 9999

    orchestrator.run_once(symbols=["RELIANCE"])

    assert broker.candle_calls == [("RELIANCE", "15minute", 30)]
    assert strategy.update_calls == [("RELIANCE", candles)]


def test_candle_update_staggers_calls_with_a_delay_between_symbols(monkeypatch):
    # Found live 2026-08-31: firing all N candle fetches back-to-back with zero delay
    # could burst past Groww's per-second rate limit (shared with the CASH LTP loop),
    # causing repeated "Rate limit has breached" errors. A small stagger between calls
    # keeps any 1-second window well under the limit.
    sleep_calls = []
    monkeypatch.setattr(orchestrator_module.time, "sleep", lambda s: sleep_calls.append(s))

    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    strategy = CandleAwareStrategy()
    broker = CandleBroker(price=100.0, candles_by_symbol={
        "RELIANCE": [{"close": 100.0}], "TCS": [{"close": 200.0}], "INFY": [{"close": 300.0}],
    })
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)
    orchestrator._last_candle_fetch_time = time.monotonic() - 9999

    orchestrator.run_once(symbols=["RELIANCE", "TCS", "INFY"])

    # 3 symbols -> 2 stagger delays (none before the first symbol's own fetch).
    assert sleep_calls == [Orchestrator._CANDLE_FETCH_STAGGER_SEC] * 2
    assert len(broker.candle_calls) == 3


def test_candle_update_skips_symbol_on_fetch_none_without_crashing():
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    strategy = CandleAwareStrategy()
    broker = CandleBroker(price=100.0, candles_by_symbol={})  # get_recent_candles() -> None
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)
    orchestrator._last_candle_fetch_time = time.monotonic() - 9999

    orchestrator.run_once(symbols=["RELIANCE"])  # must not raise

    assert strategy.update_calls == []  # skipped, not called with None


def test_candle_update_uses_injected_clock_not_real_time():
    fake_clock = {"now": 1000.0}
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    strategy = CandleAwareStrategy()
    broker = CandleBroker(price=100.0, candles_by_symbol={"RELIANCE": [{"close": 100.0}]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy,
                                 clock=lambda: fake_clock["now"])

    orchestrator.run_once(symbols=["RELIANCE"])  # construction-time clock value, no fire
    assert broker.candle_calls == []

    fake_clock["now"] += 61  # past _CANDLE_FETCH_INTERVAL_SEC (60), by the injected clock only
    orchestrator.run_once(symbols=["RELIANCE"])
    assert len(broker.candle_calls) == 1


# --- capital-aware CASH position sizing -----------------------------------

class NoneLtpBroker(FixedPriceBroker):
    """get_ltp() always returns None — used to reach the resize step's ref_price is None
    guard, since MARsiStrategy itself always guards this and can't exercise the gap."""

    def get_ltp(self, symbol, segment=None):
        return None


def test_buy_order_qty_resized_from_available_capital():
    settings = make_settings(max_order_value_inr=50_000, total_capital_inr=50_000,
                              max_position_qty=1_000)
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)
    strategy = ScriptedStrategy({"RELIANCE": [make_buy_order()]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)

    orchestrator.run_once(symbols=["RELIANCE"])

    expected_qty = calculate_entry_qty(100.0, 50_000.0, 50_000.0, 1_000)
    assert expected_qty == 500  # sanity-check the hand expectation itself

    orders = orders_repo.get_recent_orders(10)
    assert orders[0]["qty"] == expected_qty  # resize is visible on the persisted order row
    position = positions_repo.get_open_position("RELIANCE")
    assert position["qty"] == expected_qty


def test_buy_order_qty_resize_respects_deployed_capital_from_prior_position():
    # Seed an existing open position directly (mirrors test_risk_manager.py's own
    # "deployed capital" test pattern) so available_capital = total - already_deployed,
    # not just total_capital_inr.
    oid = orders_repo.insert_order(make_buy_order(symbol="RELIANCE"), status="FILLED",
                                    reference_price=8000.0)
    positions_repo.open_position(symbol="RELIANCE", qty=1, entry_price=8000.0, entry_order_id=oid)

    settings = make_settings(max_order_value_inr=100_000, total_capital_inr=10_000,
                              max_position_qty=1_000)
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=50.0)
    strategy = ScriptedStrategy({"TCS": [make_buy_order(symbol="TCS")]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)

    orchestrator.run_once(symbols=["TCS"])

    # available_capital = 10_000 - 8_000 = 2_000 -> floor(2000 / 50) = 40, well under the
    # (deliberately loose) value/qty caps, proving deployed capital is what binds here.
    position = positions_repo.get_open_position("TCS")
    assert position["qty"] == 40


def test_unaffordable_buy_order_is_blocked_not_crashed():
    settings = make_settings(max_order_value_inr=50, total_capital_inr=50, max_position_qty=1_000)
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=100.0)  # resized qty = 0 (50 // 100)
    strategy = ScriptedStrategy({"RELIANCE": [make_buy_order()]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)

    orchestrator.run_once(symbols=["RELIANCE"])  # must not raise

    orders = orders_repo.get_recent_orders(10)
    assert orders[0]["status"] == "BLOCKED"
    assert "Quantity must be positive" in orders[0]["message"]
    assert positions_repo.get_open_position("RELIANCE") is None


def test_buy_order_resize_skips_cleanly_when_no_reference_price_available():
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = NoneLtpBroker(price=100.0)  # get_ltp() -> None; order has no limit_price either
    strategy = ScriptedStrategy({"RELIANCE": [make_buy_order()]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)

    orchestrator.run_once(symbols=["RELIANCE"])  # must not raise

    orders = orders_repo.get_recent_orders(10)
    assert orders[0]["status"] == "BLOCKED"
    assert "No reference price available" in orders[0]["message"]


def test_sell_order_qty_overridden_to_match_held_position():
    # Seed a position at a qty that would never come from today's DEFAULT_ORDER_QTY=1
    # placeholder, proving the SELL branch reads the real held qty rather than trusting
    # whatever the strategy proposed.
    oid = orders_repo.insert_order(make_buy_order(symbol="RELIANCE", qty=40), status="FILLED",
                                    reference_price=100.0)
    positions_repo.open_position(symbol="RELIANCE", qty=40, entry_price=100.0, entry_order_id=oid)

    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=110.0)
    strategy = ScriptedStrategy({"RELIANCE": [make_sell_order(qty=1)]})  # stale placeholder qty
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)

    orchestrator.run_once(symbols=["RELIANCE"])

    orders = orders_repo.get_recent_orders(10)
    assert orders[0]["qty"] == 40  # not the strategy's placeholder qty=1
    assert positions_repo.get_open_position("RELIANCE") is None  # fully closed


def test_sell_order_with_no_open_position_leaves_qty_unresized():
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=110.0)
    strategy = ScriptedStrategy({"RELIANCE": [make_sell_order(qty=1)]})
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)

    orchestrator.run_once(symbols=["RELIANCE"])  # must not raise — no open position to close

    orders = orders_repo.get_recent_orders(10)
    assert orders[0]["qty"] == 1  # left as the strategy proposed it


def test_fno_order_qty_never_resized_by_cash_sizing_logic():
    # Generous caps (so the order is approved for reasons unrelated to sizing) — the point
    # is that order.qty stays exactly what the strategy proposed (1), never recomputed by
    # core/position_sizing.py, since the resize step is gated on segment == CASH.
    settings = make_settings(allow_fno=True, allow_fno_index=True)
    risk_manager = RiskManager(settings.risk, mode="PAPER")
    broker = FixedPriceBroker(price=200.0)
    fno_strategy = ScriptedFnoStrategy({"NIFTY": [make_fno_order(qty=1, lot_size=75)]})
    chain = make_chain(strikes=[make_matching_strike(ltp=200.0)])
    fno_market_data = StubFnoMarketData({"NIFTY": chain})
    orchestrator = Orchestrator(settings, broker, risk_manager, ScriptedStrategy({}),
                                 fno_strategy=fno_strategy, fno_market_data=fno_market_data)

    orchestrator.run_once_fno(["NIFTY"])

    orders = orders_repo.get_recent_orders(10)
    assert orders[0]["qty"] == 1
    assert orders[0]["status"] == "FILLED"


# --- MIS auto-square-off (3:20 PM IST cutoff) -----------------------------

class ForceExitRecordingStrategy(BaseStrategy):
    def __init__(self):
        self.force_exit_calls = []

    def decide(self, symbol, last_traded_price):
        return None

    def force_exit(self, symbol):
        self.force_exit_calls.append(symbol)


def _seed_open_position(symbol="RELIANCE", qty=5, entry_price=100.0, entry_charges=1.0,
                         segment="CASH", underlying_symbol=None):
    oid = orders_repo.insert_order(make_buy_order(symbol=symbol, qty=qty),
                                    status="FILLED", reference_price=entry_price)
    positions_repo.open_position(symbol=symbol, qty=qty, entry_price=entry_price,
                                  entry_order_id=oid, segment=segment,
                                  underlying_symbol=underlying_symbol, entry_charges=entry_charges)


def test_square_off_closes_open_cash_position_past_cutoff(monkeypatch):
    monkeypatch.setattr(orchestrator_module, "is_past_square_off_cutoff", lambda *a, **k: True)
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=110.0)
    strategy = ForceExitRecordingStrategy()
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)
    orchestrator._last_square_off_check_time = time.monotonic() - 9999

    _seed_open_position(qty=5, entry_price=100.0, entry_charges=1.0)

    orchestrator.run_once(symbols=["RELIANCE"])

    assert positions_repo.get_open_position("RELIANCE") is None
    assert strategy.force_exit_calls == ["RELIANCE"]

    orders = orders_repo.get_recent_orders(10)
    sell_order = next(o for o in orders if o["side"] == "SELL")
    assert sell_order["status"] == "FILLED"
    assert sell_order["reason"].startswith("AUTO_SQUARE_OFF:")

    exit_charges = calculate_order_charges(110.0 * 5, Side.SELL) + calculate_square_off_penalty()
    expected_pnl = round((110.0 - 100.0) * 5 - 1.0 - exit_charges, 2)
    closed = positions_repo.get_closed_positions()
    assert closed[0]["realized_pnl"] == expected_pnl


def test_square_off_noop_before_cutoff(monkeypatch):
    monkeypatch.setattr(orchestrator_module, "is_past_square_off_cutoff", lambda *a, **k: False)
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=110.0)
    strategy = ForceExitRecordingStrategy()
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)
    orchestrator._last_square_off_check_time = time.monotonic() - 9999

    _seed_open_position()

    orchestrator.run_once(symbols=["RELIANCE"])

    assert positions_repo.get_open_position("RELIANCE") is not None
    assert strategy.force_exit_calls == []
    orders = orders_repo.get_recent_orders(10)
    assert all(o["side"] == "BUY" for o in orders)  # no SELL was proposed


def test_square_off_noop_when_no_open_position(monkeypatch):
    monkeypatch.setattr(orchestrator_module, "is_past_square_off_cutoff", lambda *a, **k: True)
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=110.0)
    strategy = ForceExitRecordingStrategy()
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)
    orchestrator._last_square_off_check_time = time.monotonic() - 9999

    orchestrator.run_once(symbols=["RELIANCE"])  # must not raise

    assert orders_repo.get_recent_orders(10) == []
    assert strategy.force_exit_calls == []


def test_square_off_check_only_runs_once_per_interval(monkeypatch):
    calls = []

    def fake_cutoff(*args, **kwargs):
        calls.append(1)
        return False

    monkeypatch.setattr(orchestrator_module, "is_past_square_off_cutoff", fake_cutoff)
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=110.0)
    strategy = ForceExitRecordingStrategy()
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)
    orchestrator._last_square_off_check_time = time.monotonic() - 9999

    orchestrator.run_once(symbols=["RELIANCE"])
    orchestrator.run_once(symbols=["RELIANCE"])
    orchestrator.run_once(symbols=["RELIANCE"])

    assert len(calls) == 1  # only the first call actually checked the cutoff


def test_square_off_blocked_order_leaves_position_open(monkeypatch):
    monkeypatch.setattr(orchestrator_module, "is_past_square_off_cutoff", lambda *a, **k: True)
    settings = make_settings(max_trades_per_day=0)
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=110.0)
    strategy = ForceExitRecordingStrategy()
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)
    orchestrator._last_square_off_check_time = time.monotonic() - 9999

    _seed_open_position()

    orchestrator.run_once(symbols=["RELIANCE"])

    assert positions_repo.get_open_position("RELIANCE") is not None
    assert strategy.force_exit_calls == []
    orders = orders_repo.get_recent_orders(10)
    sell_order = next(o for o in orders if o["side"] == "SELL")
    assert sell_order["status"] == "BLOCKED"


def test_square_off_skips_fno_positions(monkeypatch):
    monkeypatch.setattr(orchestrator_module, "is_past_square_off_cutoff", lambda *a, **k: True)
    settings = make_settings()
    risk_manager = RiskManager(settings.risk)
    broker = FixedPriceBroker(price=110.0)
    strategy = ForceExitRecordingStrategy()
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)
    orchestrator._last_square_off_check_time = time.monotonic() - 9999

    _seed_open_position(symbol="NIFTY2690122000CE", segment="FNO", underlying_symbol="NIFTY")

    orchestrator.run_once(symbols=["NIFTY2690122000CE"])

    assert positions_repo.get_open_position("NIFTY2690122000CE") is not None
    assert strategy.force_exit_calls == []
