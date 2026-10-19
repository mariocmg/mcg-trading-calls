"""Tests for OCC parsing, market hours, heuristics and the institutional filter."""

from datetime import datetime
from zoneinfo import ZoneInfo

from flow.cboe import _parse_occ
from flow.heuristics import _otm_pct
from flow.institutional import (
    _cluster_sizes,
    bucket_key,
    contract_features,
    premium_percentile,
)
from flow.market_hours import (
    ET,
    is_rth_timestamp,
    parse_et_timestamp,
    session_date_et,
    us_equity_options_open,
)


def test_parse_occ():
    p = _parse_occ("MU260116C00125000")
    assert p is not None
    assert p["expiry"] == "2026-01-16"
    assert p["call_put"] == "C"
    assert p["strike"] == 125.0
    assert isinstance(p["dte"], int)
    assert _parse_occ("garbage") is None


def test_market_hours_open_closed():
    mon_open = datetime(2026, 10, 19, 10, 0, tzinfo=ET)  # Monday
    assert us_equity_options_open(mon_open) is True
    mon_early = datetime(2026, 10, 19, 9, 0, tzinfo=ET)
    assert us_equity_options_open(mon_early) is False
    sat = datetime(2026, 10, 24, 12, 0, tzinfo=ET)
    assert us_equity_options_open(sat) is False


def test_parse_et_timestamp_naive_is_et():
    dt = parse_et_timestamp("2026-10-19T10:00:00")
    assert dt is not None and dt.tzinfo is not None
    assert dt.utcoffset() == ET.utcoffset(dt.replace(tzinfo=None))
    assert is_rth_timestamp("2026-10-19T10:00:00") is True
    assert is_rth_timestamp("2026-10-19T02:00:00") is False
    assert is_rth_timestamp(None) is False


def test_session_date_et():
    assert session_date_et(datetime(2026, 10, 19, 23, 30, tzinfo=ET)).isoformat() == "2026-10-19"


def test_otm_pct():
    assert _otm_pct("C", 110.0, 100.0) == 10.0
    assert _otm_pct("P", 90.0, 100.0) == 10.0
    assert _otm_pct("C", 100.0, 100.0) == 0.0
    assert _otm_pct("C", 100.0, None) is None
    assert _otm_pct("C", 100.0, 0) is None


def _alert(**kw):
    base = {
        "underlying": "MU",
        "contract": "X",
        "call_put": "C",
        "strike": 100.0,
        "expiry": "2026-11-20",
        "dte": 30,
        "price": 2.0,
        "volume": 1000,
        "open_interest": 500,
        "delta_volume": 300,
        "premium_usd": 60000.0,
        "otm_pct": 5.0,
        "side_bias": "buyish",
        "spot": 95.0,
    }
    base.update(kw)
    return base


def test_contract_features_keys():
    feats = contract_features(_alert())
    assert set(feats) == {
        "log_premium", "log_day_premium", "vol_oi", "log_volume", "log_oi",
        "delta_share", "abs_otm", "dte_norm", "near_term", "is_put", "buyish",
        "lottery", "hedge_shape", "size_vs_oi", "premium_rank", "opening_risk",
        "sweet_spot_dte", "cluster_flag",
    }
    assert feats["lottery"] == 0.0
    deep_lotto = _alert(otm_pct=25.0, price=0.5, premium_usd=10_000.0)
    assert contract_features(deep_lotto)["lottery"] == 1.0


def test_bucket_key():
    a = _alert(call_put="P", dte=5, otm_pct=3.0)
    assert bucket_key(a).startswith("HEDGE|P|near|")
    b = _alert(call_put="C", dte=30, otm_pct=4.0)
    assert bucket_key(b) == "INSTITUTIONAL|C|long|mild"


def test_cluster_sizes_groups_nearby_strikes():
    cands = [
        _alert(contract="A", strike=100.0, spot=100.0),
        _alert(contract="B", strike=101.0, spot=100.0),  # within 2.5%
        _alert(contract="C", strike=110.0, spot=100.0),  # outside
    ]
    sizes = _cluster_sizes(cands)
    assert sizes[0] == 2 and sizes[1] == 2 and sizes[2] == 1


def test_premium_percentile():
    assert premium_percentile([], 5.0) == 0.5
    assert premium_percentile([1.0, 2.0, 3.0, 4.0], 3.0) == 0.75
