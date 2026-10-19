"""Tests for the AI upgrade round: combos, levels, vector v2, outcomes, backtest."""

import sqlite3
from datetime import datetime, timedelta, timezone

from flow import db
from flow.backtest import temporal_split_report, walk_forward
from flow.combos import detect_combos
from flow.heuristics import _is_block, _tod_score
from flow.levels import compute_positioning_levels, summarize_persisted
from flow.outcomes import _thesis_hit


def _alert(**kw):
    base = {
        "underlying": "MU",
        "contract": "MUW",
        "call_put": "C",
        "strike": 100.0,
        "expiry": "2026-11-20",
        "side_bias": "buyish",
        "premium_usd": 100_000.0,
        "inst_score": 0.7,
        "spot": 95.0,
    }
    base.update(kw)
    return base


# ---- combos ----


def test_risk_reversal_bullish():
    alerts = [
        _alert(contract="c1", call_put="C", strike=100, side_bias="buyish", premium_usd=200_000),
        _alert(contract="p1", call_put="P", strike=98, side_bias="sellish", premium_usd=180_000),
    ]
    combos = detect_combos(alerts)
    rr = [c for c in combos if c.combo_type == "RISK_REVERSAL"]
    assert rr and rr[0].direction == "BULLISH"
    assert rr[0].confidence > 0.3


def test_risk_reversal_bearish():
    alerts = [
        _alert(contract="c1", call_put="C", strike=100, side_bias="sellish", premium_usd=200_000),
        _alert(contract="p1", call_put="P", strike=98, side_bias="buyish", premium_usd=180_000),
    ]
    combos = detect_combos(alerts)
    rr = [c for c in combos if c.combo_type == "RISK_REVERSAL"]
    assert rr and rr[0].direction == "BEARISH"


def test_call_spread():
    alerts = [
        _alert(contract="c1", call_put="C", strike=100, side_bias="buyish", premium_usd=150_000),
        _alert(contract="c2", call_put="C", strike=110, side_bias="buyish", premium_usd=120_000),
    ]
    combos = detect_combos(alerts)
    assert any(c.combo_type == "CALL_SPREAD" and c.direction == "BULLISH" for c in combos)


def test_no_combo_without_side_agreement():
    alerts = [
        _alert(contract="c1", call_put="C", strike=100, side_bias="unknown", premium_usd=150_000),
        _alert(contract="p1", call_put="P", strike=98, side_bias="unknown", premium_usd=120_000),
    ]
    combos = detect_combos(alerts)
    assert not [c for c in combos if c.combo_type == "RISK_REVERSAL"]


def test_combo_as_dict_shape():
    alerts = [
        _alert(contract="c1", call_put="C", strike=100, side_bias="buyish"),
        _alert(contract="c2", call_put="C", strike=110, side_bias="buyish"),
    ]
    d = detect_combos(alerts)[0].as_dict()
    assert {"combo_type", "direction", "confidence", "label_es", "legs"} <= set(d)


# ---- levels ----


def _chain_rows():
    rows = []
    for strike, call_oi, put_oi, gamma in [
        (90, 100, 5000, 0.02),
        (95, 200, 3000, 0.025),
        (100, 8000, 1500, 0.03),
        (105, 6000, 800, 0.028),
        (110, 3000, 400, 0.02),
    ]:
        for cp, oi in (("C", call_oi), ("P", put_oi)):
            rows.append({
                "underlying": "MU", "expiry": "2026-11-20", "call_put": cp,
                "strike": strike, "open_interest": oi, "gamma": gamma,
                "spot": 100.0, "volume": 100, "price": 2.0, "dte": 30,
            })
    return rows


def test_levels_walls_and_gex():
    out = compute_positioning_levels(_chain_rows(), "MU")
    assert out["call_wall"]["strike"] == 100  # max call OI
    assert out["put_wall"]["strike"] == 90    # max put OI
    assert out["net_gex"] > 0  # calls dominate
    assert out["n_strikes"] == 5


def test_max_pain_sane():
    out = compute_positioning_levels(_chain_rows(), "MU")
    mp = out["max_pain_nearest"]
    assert mp is not None
    assert 90 <= mp["strike"] <= 110


def test_gamma_flip_between_walls():
    out = compute_positioning_levels(_chain_rows(), "MU")
    flip = out["gamma_flip"]
    # net gex per strike: calls add, puts subtract; cumulative should cross
    assert flip is None or 90 <= flip["strike"] <= 110


def test_dte_buckets():
    out = compute_positioning_levels(_chain_rows(), "MU")
    b = out["dte_buckets"]["22-45"]
    assert b["call_premium"] > 0 and b["put_premium"] > 0
    assert 0 < b["call_share"] < 1


def test_summarize_persisted_roundtrip(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    sid = db.start_scan(conn)
    out = compute_positioning_levels(_chain_rows(), "MU")
    db.save_strike_levels(conn, sid, "MU", out["levels"])
    rows = db.load_strike_levels(conn, "MU")
    s = summarize_persisted([dict(r) for r in rows], "MU")
    assert s["call_wall"]["strike"] == 100
    assert s["put_wall"]["strike"] == 90
    conn.close()


# ---- outcomes labels ----


def test_thesis_hit_atr_floor():
    # below 0.5×ATR noise floor → 0 even in the right direction
    assert _thesis_hit(False, 0.003, 0.01) == 0
    assert _thesis_hit(False, 0.02, 0.01) == 1
    assert _thesis_hit(True, -0.02, 0.01) == 1
    assert _thesis_hit(True, 0.02, 0.01) == 0


# ---- vector v2 ----


def test_vector_checks_present():
    from flow.vector import compute_vector

    alerts = []
    for i in range(30):
        alerts.append(_alert(
            contract=f"c{i}", call_put="C", strike=105, side_bias="buyish",
            premium_usd=150_000, inst_score=0.75, created_at="2026-10-19T10:00:00",
            id=i,
        ))
    v = compute_vector(alerts, "MU")
    assert "checks" in v and len(v["checks"]) >= 4
    assert v["stage"] in ("EARLY", "BUILDING", "CONFIRMED")
    assert "confidence_note" in v
    assert "time" in v["invalidation"]
    assert v["checks_ok"] == sum(1 for c in v["checks"] if c["ok"])


def test_vector_invalidation_uses_wall():
    from flow.vector import _invalidation_v2

    levels = [
        {"strike": 90.0, "expiry": "2026-11-20", "put_oi": 9000, "call_oi": 100},
        {"strike": 95.0, "expiry": "2026-11-20", "put_oi": 2000, "call_oi": 200},
        {"strike": 100.0, "expiry": "2026-11-20", "put_oi": 300, "call_oi": 8000},
    ]
    inv = _invalidation_v2({"atr": 2.0}, 92.0, "BULLISH", levels)
    assert inv["price"] == 90.0
    assert "put_wall" in inv["method"]
    assert inv["time"] == "2026-11-20"


def test_vector_invalidation_atr_fallback():
    from flow.vector import _invalidation_v2

    inv = _invalidation_v2({"atr": 2.0, "swing_low": 85.0}, 92.0, "BULLISH", None)
    assert inv["price"] is not None and inv["price"] < 92.0


def test_calibrated_confidence_shape():
    from flow.vector import _calibrated_confidence

    c1, _ = _calibrated_confidence(0.9, "BULLISH", 5, conn=None)
    c2, _ = _calibrated_confidence(0.1, "BULLISH", 1, conn=None)
    assert c1 > c2
    assert 52.0 <= c2 <= 96.5


# ---- heuristics helpers ----


def test_tod_score_open_close():
    from datetime import datetime

    assert _tod_score(datetime(2026, 10, 19, 9, 35)) == 1.0   # open
    assert _tod_score(datetime(2026, 10, 19, 15, 50)) == 1.0  # close
    assert _tod_score(datetime(2026, 10, 19, 12, 0)) == 0.35  # midday
    assert _tod_score(None) == 0.5


def test_is_block_thresholds():
    assert _is_block(100, "x", None) is False
    assert _is_block(10_000, "x", None) is True


# ---- backtest ----


def _seed_resolved(conn, n=40):
    conn.execute("DELETE FROM alerts")
    base = datetime(2026, 9, 1, 15, 30, tzinfo=timezone.utc)
    for i in range(n):
        hit = 1 if i % 2 == 0 else 0
        conn.execute(
            """INSERT INTO alerts (scan_id, created_at, underlying, contract, call_put,
               strike, expiry, dte, price, volume, open_interest, delta_volume,
               premium_usd, reason, inst_score, inst_label, inst_subtype, spot,
               outcome_hit, outcome_return, outcome_adj_return)
               VALUES (1, ?, 'MU', ?, 'C', 100, '2026-11-20', 30, 2.0, 100, 500, 100,
               20000, 'test', ?, 'ACCUMULATION', 'DIRECTIONAL', 95, ?, 0.01, 0.008)""",
            ((base + timedelta(days=i)).isoformat(), f"c{i}",
            0.9 if i % 2 == 0 else 0.55, hit),
        )
    conn.commit()


def test_walk_forward_buckets():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    _seed_resolved(conn)
    rep = walk_forward(conn, min_score=0.5)
    assert rep["n"] == 40
    high = next(b for b in rep["by_score"] if b["bucket"] == "0.80+")
    low = next(b for b in rep["by_score"] if b["bucket"] == "0.50-0.65")
    assert high["hit_rate"] > low["hit_rate"]
    conn.close()


def test_temporal_split_needs_labels():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    rep = temporal_split_report(conn)
    assert rep["ok"] is False
    conn.close()
