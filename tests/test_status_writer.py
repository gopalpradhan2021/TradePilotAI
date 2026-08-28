from core.status_writer import write_heartbeat, read_heartbeat


def test_strategy_debug_round_trips_through_heartbeat():
    write_heartbeat(
        mode="PAPER", halted=False, halt_reason="", symbols=["RELIANCE"],
        last_ltp={"RELIANCE": 100.0},
        strategy_debug={"RELIANCE": {"warmed_up": True, "short_ma": 101.2}},
    )

    result = read_heartbeat()

    assert result["strategy_debug"] == {"RELIANCE": {"warmed_up": True, "short_ma": 101.2}}


def test_strategy_debug_defaults_to_empty_dict_when_omitted():
    write_heartbeat(mode="PAPER", halted=False, halt_reason="", symbols=[], last_ltp={})

    result = read_heartbeat()

    assert result["strategy_debug"] == {}


def test_read_heartbeat_defaults_strategy_debug_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HEARTBEAT_PATH", str(tmp_path / "does_not_exist.json"))

    result = read_heartbeat()

    assert result["strategy_debug"] == {}


def test_fno_fields_round_trip_through_heartbeat():
    write_heartbeat(
        mode="PAPER", halted=False, halt_reason="", symbols=["RELIANCE"],
        last_ltp={"RELIANCE": 100.0},
        fno_underlyings=["NIFTY"],
        fno_strategy_debug={"NIFTY": {"warmed_up": True, "in_position": False}},
    )

    result = read_heartbeat()

    assert result["fno_underlyings"] == ["NIFTY"]
    assert result["fno_strategy_debug"] == {"NIFTY": {"warmed_up": True, "in_position": False}}


def test_fno_fields_default_to_empty_when_omitted():
    write_heartbeat(mode="PAPER", halted=False, halt_reason="", symbols=[], last_ltp={})

    result = read_heartbeat()

    assert result["fno_underlyings"] == []
    assert result["fno_strategy_debug"] == {}


def test_read_heartbeat_defaults_fno_fields_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HEARTBEAT_PATH", str(tmp_path / "does_not_exist2.json"))

    result = read_heartbeat()

    assert result["fno_underlyings"] == []
    assert result["fno_strategy_debug"] == {}
