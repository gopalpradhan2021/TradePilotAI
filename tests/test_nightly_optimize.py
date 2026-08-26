from scripts.nightly_optimize import MIN_TRADES_FOR_CONFIDENCE, TOP_N_CANDIDATES, rank_candidates
from strategies.ma_rsi_strategy import MARsiParams


def make_report(net_pnl, total_trades):
    return {"net_pnl": net_pnl, "total_trades": total_trades, "win_rate_pct": None,
            "wins": 0, "losses": 0, "bars_processed": 100, "avg_win": None, "avg_loss": None,
            "max_drawdown": 0.0, "risk_halt_events": 0}


def test_rank_candidates_orders_by_out_of_sample_pnl_descending():
    candidates = [
        (MARsiParams(short_window=5), make_report(10, 5), make_report(20, 5)),
        (MARsiParams(short_window=9), make_report(100, 5), make_report(5, 5)),
        (MARsiParams(short_window=12), make_report(1, 5), make_report(50, 5)),
    ]

    ranked = rank_candidates(candidates)

    assert [r["out_sample"]["net_pnl"] for r in ranked] == [50, 20, 5]


def test_high_in_sample_pnl_does_not_win_if_out_of_sample_is_worse():
    """The whole point of the walk-forward split: a candidate that looks great in-sample but
    poor out-of-sample must not be ranked first — that's exactly the overfitting this design
    is meant to catch."""
    overfit = (MARsiParams(short_window=5), make_report(1000, 10), make_report(-50, 5))
    robust = (MARsiParams(short_window=9), make_report(20, 5), make_report(30, 5))

    ranked = rank_candidates([overfit, robust])

    assert ranked[0]["params"]["short_window"] == 9


def test_candidate_below_min_trades_flagged_insufficient_sample():
    thin = (MARsiParams(), make_report(500, 1), make_report(500, 1))

    ranked = rank_candidates([thin])

    assert ranked[0]["insufficient_sample"] is True


def test_candidate_at_min_trades_not_flagged():
    enough = (MARsiParams(), make_report(50, MIN_TRADES_FOR_CONFIDENCE),
              make_report(50, MIN_TRADES_FOR_CONFIDENCE))

    ranked = rank_candidates([enough])

    assert ranked[0]["insufficient_sample"] is False


def test_rank_candidates_caps_at_top_n():
    candidates = [
        (MARsiParams(short_window=i), make_report(i, 5), make_report(i, 5))
        for i in range(TOP_N_CANDIDATES + 5)
    ]

    ranked = rank_candidates(candidates)

    assert len(ranked) == TOP_N_CANDIDATES


def test_rank_candidates_empty_input_returns_empty():
    assert rank_candidates([]) == []
