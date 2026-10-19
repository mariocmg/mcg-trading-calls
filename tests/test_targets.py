"""Tests for institutional target levels (flow/targets.py)."""

from flow.targets import _classify, _cluster, institutional_targets


def _a(**kw):
    base = {
        "underlying": "MU", "call_put": "C", "strike": 950.0,
        "premium_usd": 50000.0, "inst_label": "INSTITUTIONAL",
        "inst_score": 0.8, "side_conf": 0.7, "spot": 920.0,
        "is_block": 0, "combo_type": None,
    }
    base.update(kw)
    return base


def test_classify():
    assert _classify(_a(call_put="C")) == "bull_target"
    assert _classify(_a(call_put="P")) == "bear_target"
    assert _classify(_a(call_put="P", inst_label="HEDGE")) == "hedge"
    assert _classify(_a(call_put="C", inst_label="HEDGE")) == "hedge"
    assert _classify(_a(call_put="X")) == "ignore"


def test_cluster_groups_nearby_strikes():
    rows = [
        _a(strike=950.0, premium_usd=100000.0),
        _a(strike=955.0, premium_usd=60000.0),   # within 2% of 950
        _a(strike=1000.0, premium_usd=200000.0),  # own level
    ]
    out = _cluster(rows, band_pct=0.02, top_n=3)
    assert len(out) == 2
    # ranked by premium: 1000 first
    assert out[0]["strike"] == 1000.0
    assert out[0]["premium_usd"] == 200000.0
    # 950 cluster aggregated
    assert out[1]["strike_lo"] == 950.0 and out[1]["strike_hi"] == 955.0
    assert out[1]["premium_usd"] == 160000.0
    assert out[1]["n_prints"] == 2


def test_cluster_top_n():
    rows = [_a(strike=900.0 + i * 50, premium_usd=1000.0 * (i + 1)) for i in range(6)]
    out = _cluster(rows, band_pct=0.01, top_n=3)
    assert len(out) == 3
    assert out[0]["premium_usd"] == 6000.0  # highest premium first


def test_cluster_empty():
    assert _cluster([]) == []


def test_institutional_targets_split():
    alerts = [
        _a(call_put="C", strike=950.0, premium_usd=100000.0),
        _a(call_put="C", strike=1000.0, premium_usd=50000.0),
        _a(call_put="P", strike=880.0, premium_usd=80000.0),
        _a(call_put="P", strike=870.0, premium_usd=40000.0, inst_label="HEDGE"),
        _a(call_put="X", strike=900.0),  # ignored
    ]
    res = institutional_targets(alerts, "MU")
    assert res["underlying"] == "MU"
    assert res["n_alerts"] == 4
    assert len(res["bull_targets"]) == 2
    assert len(res["bear_targets"]) == 1
    assert len(res["hedge_levels"]) == 1
    assert res["bull_targets"][0]["strike"] == 950.0
    assert res["bull_targets"][0]["otm_pct"] is not None


def test_institutional_targets_other_underlying_filtered():
    alerts = [_a(underlying="AAPL", call_put="C", strike=200.0)]
    res = institutional_targets(alerts, "MU")
    assert res["n_alerts"] == 0
    assert res["bull_targets"] == []
