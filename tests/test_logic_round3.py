"""Tests for logic round 3: campaign outcomes, memory decay, regime real labels."""

import sqlite3
from datetime import datetime, timedelta, timezone

from flow import db
from flow.campaigns import link_alerts, resolve_outcomes as resolve_camp_outcomes
from flow.outcomes import decay_memory, label_follow_through


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn


def _alert(**kw):
    base = {
        "scan_id": 1, "underlying": "MU", "contract": "MUc", "call_put": "C",
        "strike": 100.0, "expiry": "2026-11-20", "dte": 30, "price": 2.0,
        "volume": 100, "open_interest": 500, "delta_volume": 100,
        "premium_usd": 20000.0, "reason": "test", "side_bias": "buyish",
        "spot": 95.0, "inst_score": 0.75, "inst_label": "INSTITUTIONAL",
    }
    base.update(kw)
    return base


# ---- shared label helper ----


def test_label_follow_through_shape():
    ts = datetime(2026, 10, 1, 15, 30, tzinfo=timezone.utc)
    hit, raw, adj = label_follow_through("MU", 100.0, ts, 110.0, False, atr_cache={})
    assert hit in (0, 1) and abs(raw - 0.10) < 1e-9
    hit2, _, _ = label_follow_through("MU", 100.0, ts, 90.0, True, atr_cache={})
    assert hit2 in (0, 1)


def test_label_follow_through_atr_floor():
    ts = datetime(2026, 10, 1, 15, 30, tzinfo=timezone.utc)
    # tiny move vs huge ATR floor → 0
    hit, _, _ = label_follow_through(
        "MU", 100.0, ts, 100.5, False, atr_cache={"MU": 0.10}
    )
    assert hit == 0


# ---- campaign outcomes ----


def test_campaign_outcome_resolves():
    conn = _db()
    sid = db.start_scan(conn)
    link_alerts(conn, [_alert(scan_id=sid, spot=95.0)])
    # age the campaign 4 days so the horizon (3d) has elapsed
    conn.execute("UPDATE campaigns SET updated_at = datetime('now', '-4 days')")
    conn.commit()
    n = resolve_camp_outcomes(conn, {"MU": 100.0}, horizon_days=3.0)
    assert n == 1
    row = conn.execute("SELECT outcome_hit, outcome_return FROM campaigns").fetchone()
    assert row["outcome_hit"] in (0, 1)
    assert row["outcome_return"] is not None
    conn.close()


def test_campaign_outcome_too_young():
    conn = _db()
    sid = db.start_scan(conn)
    link_alerts(conn, [_alert(scan_id=sid, spot=95.0)])
    n = resolve_camp_outcomes(conn, {"MU": 100.0}, horizon_days=3.0)
    assert n == 0
    conn.close()


def test_campaign_outcome_bearish():
    conn = _db()
    sid = db.start_scan(conn)
    link_alerts(conn, [_alert(scan_id=sid, spot=95.0, call_put="P")])
    conn.execute("UPDATE campaigns SET updated_at = datetime('now', '-4 days')")
    conn.commit()
    n = resolve_camp_outcomes(conn, {"MU": 100.0}, horizon_days=3.0)
    assert n == 1
    row = conn.execute("SELECT outcome_hit, direction FROM campaigns").fetchone()
    assert row["direction"] == "BEARISH"
    conn.close()


# ---- memory decay ----


def test_decay_memory():
    conn = _db()
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    conn.execute(
        "INSERT INTO inst_memory (bucket, hits, n, updated_at) VALUES (?, 80, 100, ?)",
        ("C|30|1|INSTITUTIONAL", old),
    )
    conn.commit()
    decay_memory(conn, half_life_days=30.0)
    row = conn.execute("SELECT hits, n FROM inst_memory").fetchone()
    # 60 days = 2 half-lives → ×0.25
    assert abs(float(row["hits"]) - 20.0) < 1.0
    assert abs(float(row["n"]) - 25.0) < 1.0
    conn.close()


def test_decay_memory_recent_untouched():
    conn = _db()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO inst_memory (bucket, hits, n, updated_at) VALUES (?, 80, 100, ?)",
        ("C|30|1|INSTITUTIONAL", now),
    )
    conn.commit()
    decay_memory(conn, half_life_days=30.0)
    row = conn.execute("SELECT hits, n FROM inst_memory").fetchone()
    assert float(row["hits"]) == 80.0
    conn.close()


# ---- regime model on real labels ----


def _seed_batches(conn, n_days=65):
    base = datetime(2026, 7, 1, 15, 30, tzinfo=timezone.utc)
    sid = db.start_scan(conn)
    for d in range(n_days):
        day = (base + timedelta(days=d)).isoformat()
        bullish = d % 2 == 0
        for i in range(6):
            conn.execute(
                """INSERT INTO alerts (scan_id, created_at, underlying, contract,
                   call_put, strike, expiry, dte, price, volume, open_interest,
                   delta_volume, premium_usd, reason, spot, otm_pct, inst_score,
                   inst_label, outcome_adj_return, outcome_hit)
                   VALUES (?, ?, 'MU', ?, ?, 100, '2026-11-20', 20, 2.0, 100,
                   500, 100, 20000, 'test', 95.0, 5.0, 0.7, 'INSTITUTIONAL',
                   ?, ?)""",
                (sid, day, f"c{d}_{i}",
                 "C" if bullish else "P",
                 0.05 if bullish else -0.05,
                 1 if bullish else 0),
            )
    conn.commit()


def test_regime_trains_on_real_labels():
    from flow import ml_positioning

    conn = _db()
    _seed_batches(conn)
    assert ml_positioning._count_batches(conn) >= 60
    trained = ml_positioning._train_from_outcomes(conn)
    assert trained is not None
    _model, info = trained
    assert info["synthetic"] is False
    assert info["n_train"] >= 60
    conn.close()


def test_regime_falls_back_without_labels():
    from flow import ml_positioning

    conn = _db()
    assert ml_positioning._train_from_outcomes(conn) is None
    conn.close()
