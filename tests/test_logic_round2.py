"""Tests for the logic round 2: campaigns, DTE horizons, quality gate, gamma regime."""

import sqlite3
from datetime import datetime, timedelta, timezone

from flow import db
from flow.campaigns import active_summary, link_alerts
from flow.levels import gamma_regime
from flow.outcomes import _horizon_days
from flow.quality import assess_chain_quality


def _alert(**kw):
    base = {
        "scan_id": 1,
        "underlying": "MU",
        "contract": "MU260120C00100000",
        "call_put": "C",
        "strike": 100.0,
        "expiry": "2026-11-20",
        "dte": 30,
        "price": 2.0,
        "volume": 100,
        "open_interest": 500,
        "delta_volume": 100,
        "premium_usd": 20000.0,
        "reason": "test",
        "side_bias": "buyish",
        "side_conf": 0.8,
        "spot": 95.0,
        "inst_score": 0.75,
        "inst_label": "INSTITUTIONAL",
    }
    base.update(kw)
    return base


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn


# ---- campaigns ----


def test_campaign_linking_accumulates():
    conn = _db()
    sid = db.start_scan(conn)
    a1 = _alert(scan_id=sid)
    a2 = _alert(scan_id=sid, premium_usd=30000.0, delta_volume=150)
    link_alerts(conn, [a1])
    link_alerts(conn, [a2])
    assert a1["campaign_id"] == a2["campaign_id"]
    camps = db.list_campaigns(conn)
    assert len(camps) == 1
    assert camps[0]["n_prints"] == 2
    assert camps[0]["total_premium"] == 50000.0
    conn.close()


def test_campaign_new_contract_new_campaign():
    conn = _db()
    sid = db.start_scan(conn)
    a1 = _alert(scan_id=sid, contract="AAA")
    a2 = _alert(scan_id=sid, contract="BBB")
    link_alerts(conn, [a1, a2])
    assert a1["campaign_id"] != a2["campaign_id"]
    conn.close()


def test_campaign_stale_closes():
    conn = _db()
    sid = db.start_scan(conn)
    a1 = _alert(scan_id=sid)
    link_alerts(conn, [a1])
    # age the campaign 7h
    conn.execute(
        "UPDATE campaigns SET updated_at = datetime('now', '-7 hours')"
    )
    conn.commit()
    n = db.close_stale_campaigns(conn, max_age_hours=6.0)
    assert n == 1
    # next print opens a NEW campaign
    a2 = _alert(scan_id=sid)
    link_alerts(conn, [a2])
    assert a2["campaign_id"] != a1["campaign_id"]
    conn.close()


def test_active_summary_conviction():
    conn = _db()
    sid = db.start_scan(conn)
    for i in range(5):
        link_alerts(conn, [_alert(scan_id=sid, premium_usd=200000.0)])
    s = active_summary(conn)
    assert s[0]["conviction"] == "ACCUMULATING"
    assert s[0]["n_prints"] == 5
    conn.close()


# ---- DTE horizons ----


def test_horizon_days():
    assert _horizon_days(3) == 1
    assert _horizon_days(7) == 1
    assert _horizon_days(10) == 2
    assert _horizon_days(20) == 3
    assert _horizon_days(40) == 4
    assert _horizon_days(60) == 5
    assert _horizon_days(None) == 3


def test_resolve_uses_dte_horizon():
    conn = _db()
    sid = db.start_scan(conn)
    now = datetime.now(timezone.utc)
    # 0-DTE alert from 2 days ago: horizon=1, grace=2 → resolves
    old = (now - timedelta(days=2)).isoformat()
    conn.execute(
        """INSERT INTO alerts (scan_id, created_at, underlying, contract, call_put,
           strike, expiry, dte, price, volume, open_interest, delta_volume,
           premium_usd, reason, spot, inst_score, inst_label)
           VALUES (?, ?, 'MU', 'c1', 'C', 100, '2026-10-21', 2, 2.0, 100, 500,
           100, 20000, 'test', 95.0, 0.7, 'INSTITUTIONAL')""",
        (sid, old),
    )
    # 45-DTE alert from 2 days ago: horizon=4 → NOT yet
    conn.execute(
        """INSERT INTO alerts (scan_id, created_at, underlying, contract, call_put,
           strike, expiry, dte, price, volume, open_interest, delta_volume,
           premium_usd, reason, spot, inst_score, inst_label)
           VALUES (?, ?, 'MU', 'c2', 'C', 100, '2026-12-04', 45, 2.0, 100, 500,
           100, 20000, 'test', 95.0, 0.7, 'INSTITUTIONAL')""",
        (sid, old),
    )
    conn.commit()
    from flow.outcomes import resolve_outcomes

    n = resolve_outcomes(conn, {"MU": 96.0})
    assert n == 1
    row = conn.execute(
        "SELECT outcome_horizon_days FROM alerts WHERE contract = 'c1'"
    ).fetchone()
    assert row["outcome_horizon_days"] == 1
    row2 = conn.execute(
        "SELECT outcome_hit FROM alerts WHERE contract = 'c2'"
    ).fetchone()
    assert row2["outcome_hit"] is None
    conn.close()


# ---- quality gate ----


def _row(**kw):
    base = {"spot": 100.0, "price": 2.0, "last": 2.0, "volume": 100,
            "bid": 1.9, "ask": 2.1, "gamma": 0.02}
    base.update(kw)
    return base


def test_quality_ok():
    rows = [_row() for _ in range(10)]
    ok, note = assess_chain_quality(rows, prev_spot=100.0)
    assert ok


def test_quality_empty():
    ok, note = assess_chain_quality([])
    assert not ok and "vacía" in note


def test_quality_spot_glitch():
    rows = [_row(spot=150.0) for _ in range(10)]
    ok, note = assess_chain_quality(rows, prev_spot=100.0)
    assert not ok and "glitch" in note


def test_quality_no_prices():
    rows = [_row(price=0, last=0) for _ in range(10)]
    ok, _ = assess_chain_quality(rows)
    assert not ok


def test_quality_inverted_quotes():
    rows = [_row(bid=2.2, ask=2.0) for _ in range(10)]
    ok, _ = assess_chain_quality(rows)
    assert not ok


# ---- gamma regime ----


def test_gamma_regime_long():
    levels = [
        {"net_gex": 1e8}, {"net_gex": 5e7}, {"net_gex": 1e7},
    ]
    r = gamma_regime(levels)
    assert r["regime"] == "LONG_GAMMA"
    assert r["trend_weight"] == 0.5


def test_gamma_regime_short():
    levels = [
        {"net_gex": -1e8}, {"net_gex": -5e7},
    ]
    r = gamma_regime(levels)
    assert r["regime"] == "SHORT_GAMMA"
    assert r["trend_weight"] == 1.3


def test_gamma_regime_neutral():
    levels = [{"net_gex": 1e6}, {"net_gex": -1e6}]
    r = gamma_regime(levels)
    assert r["regime"] == "NEUTRAL_GAMMA"


def test_vector_uses_regime():
    from flow.vector import compute_vector

    alerts = []
    for i in range(30):
        alerts.append(_alert(
            contract=f"c{i}", call_put="C", strike=105, side_bias="buyish",
            side_conf=0.8, premium_usd=150_000, inst_score=0.75,
            created_at="2026-10-19T10:00:00", id=i,
        ))
    v = compute_vector(alerts, "MU")
    assert "gamma_regime" in v
    assert v["gamma_regime"]["regime"] in ("LONG_GAMMA", "SHORT_GAMMA", "NEUTRAL_GAMMA")
