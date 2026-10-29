"""Tests de la fuente OPRA (CaymanBot): mapeo de trades a prints, sin red."""

from datetime import datetime
from zoneinfo import ZoneInfo

from flow import opra

ET = ZoneInfo("America/New_York")


def _trade(**over):
    t = {
        "contract_symbol": "O:MU270115C01100000",
        "underlying_symbol": "MU",
        "expiry_date": "2027-01-15",
        "option_type": "call",
        "strike_price": 1100,
        "premium_value": 607700,
        "price": 59,
        "size": 103,
        "volume": 1251,
        "open_interest": 5429,
        "trade_direction": "BTO",
        "is_at_ask": True,
        "is_at_bid": False,
        "is_whale_activity": True,
        "is_sweep": False,
        "is_unusual_volume": False,
        "is_multileg": False,
        "is_spread_leg": False,
        "strategy_id": "",
        "strategy_type": "",
        "strategy_leg_count": 0,
        "strategy_net_premium": 0,
        "strategy_sentiment": "",
        "classification_sentiment": "bullish",
        "is_hedge": False,
        "unique_id": "u1",
        "delta": 0.39,
        "gamma": 0.01,
        "vega": 0.5,
        "theta": -0.2,
        "implied_volatility": 0.53,
        "underlying_price": 984.61,
        "timestamp": datetime(2026, 10, 28, 15, 0, tzinfo=ET).timestamp(),
    }
    t.update(over)
    return t


def test_side_mapping_all_directions():
    assert opra._trade_side(_trade(trade_direction="BTO")) == "buy"
    assert opra._trade_side(_trade(trade_direction="BTC")) == "buy"
    assert opra._trade_side(_trade(trade_direction="STC")) == "sell"
    assert opra._trade_side(_trade(trade_direction="STO")) == "sell"


def test_side_fallback_to_ask_bid():
    t = _trade(trade_direction="", is_at_ask=False, is_at_bid=True)
    assert opra._trade_side(t) == "sell"
    t = _trade(trade_direction=None, is_at_ask=False, is_at_bid=False)
    assert opra._trade_side(t) == "unknown"


def test_occ_prefix_stripped_and_fields():
    p = opra.trade_to_print(_trade(), spot=984.0, today_et="2026-10-28")
    assert p["contract"] == "MU270115C01100000"
    assert p["call_put"] == "C"
    assert p["strike"] == 1100
    assert p["premium"] == 607700
    assert p["side"] == "buy"
    assert p["delta_vol"] == 103
    assert p["source"] == "OPRA"
    assert p["unique_id"] == "u1"


def test_whale_flag_floors_conviction():
    p = opra.trade_to_print(_trade(), spot=984.0, today_et="2026-10-28")
    assert p["opra_whale"] is True
    assert p["conviction"] >= 85


def test_sweep_and_multileg_preserved():
    t = _trade(
        is_sweep=True,
        is_multileg=True,
        strategy_id="s1",
        strategy_type="CALL SPREAD",
        strategy_leg_count=2,
        strategy_net_premium=100000,
    )
    p = opra.trade_to_print(t, spot=984.0, today_et="2026-10-28")
    assert p["sweep"] is True
    assert p["opra_multileg"] is True
    assert p["strategy_type"] == "CALL SPREAD"
    assert p["strategy_leg_count"] == 2


def test_prints_for_day_filters_date_and_premium():
    today = _trade(unique_id="today")
    yesterday = _trade(
        unique_id="yest",
        timestamp=datetime(2026, 10, 27, 15, 0, tzinfo=ET).timestamp(),
    )
    small = _trade(unique_id="small", premium_value=1000)
    out = opra.prints_for_day([today, yesterday, small], "2026-10-28", spot=984.0)
    assert [p["unique_id"] for p in out] == ["today"]


def test_accumulate_dedups_by_unique_id_and_caps():
    a = opra.trade_to_print(_trade(unique_id="a", premium_value=100000), spot=984.0, today_et="2026-10-28")
    b = opra.trade_to_print(_trade(unique_id="b", premium_value=200000), spot=984.0, today_et="2026-10-28")
    kept = opra.accumulate_opra_prints([], [a, b])
    kept = opra.accumulate_opra_prints(kept, [a])  # repetido: no duplica
    assert [p["unique_id"] for p in kept] == ["b", "a"]


def test_fetch_failure_returns_empty(monkeypatch):
    def boom(url, timeout=15.0, attempts=2):
        raise ConnectionError("no red")

    monkeypatch.setattr(opra, "_get_json", boom)
    trades, meta = opra.fetch_opra_trades("MU")
    assert trades == [] and meta is None


def test_fetch_success_parses_meta(monkeypatch):
    payload = {
        "success": True,
        "delayed": True,
        "data_delay_minutes": 60,
        "free_delay_minutes": 16,
        "subscription": {"authenticated": False},
        "data_window": {"start_date": "x", "end_date": "y"},
        "trades": [_trade()],
    }
    monkeypatch.setattr(opra, "_get_json", lambda url, timeout=15.0, attempts=2: payload)
    trades, meta = opra.fetch_opra_trades("MU")
    assert len(trades) == 1
    assert meta["delay_minutes"] == 60
    assert meta["authenticated"] is False
