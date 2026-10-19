"""Tests for db: new persisted columns, shared institutional predicate."""

import sqlite3
from datetime import datetime, timezone

import pytest

from flow import db


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


def _alert(scan_id=1, **kw):
    base = {
        "scan_id": scan_id,
        "underlying": "MU",
        "contract": "MU260116C00125000",
        "call_put": "C",
        "strike": 125.0,
        "expiry": "2026-01-16",
        "dte": 20,
        "price": 2.0,
        "volume": 1000,
        "open_interest": 500,
        "delta_volume": 300,
        "premium_usd": 60000.0,
        "vol_oi_ratio": 2.0,
        "reason": "premium_delta",
        "side_bias": "buyish",
        "spot": 120.0,
        "otm_pct": 4.2,
        "inst_score": 0.75,
        "inst_label": "INSTITUTIONAL",
        "inst_reason": "score:0.75",
        "inst_subtype": "CALL_SWEEP",
        "inst_ml_prob": 0.81,
        "inst_pct": 0.9,
        "cluster_n": 3,
        "opening": 1,
        # event_at inside RTH: Monday 2026-10-19 15:00 UTC = 11:00 ET
        "event_at": "2026-10-19T15:00:00+00:00",
    }
    base.update(kw)
    return base


def test_migrate_adds_new_columns(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    assert {"inst_subtype", "inst_ml_prob", "inst_pct"} <= cols


def test_insert_alerts_persists_subtype_and_ml_prob(conn):
    db.insert_alerts(conn, [_alert()])
    row = conn.execute("SELECT inst_subtype, inst_ml_prob, inst_pct FROM alerts").fetchone()
    assert row["inst_subtype"] == "CALL_SWEEP"
    assert row["inst_ml_prob"] == pytest.approx(0.81)
    assert row["inst_pct"] == pytest.approx(0.9)


def test_is_institutional_row_predicate(conn):
    db.insert_alerts(conn, [_alert()])
    (rth_ok,) = conn.execute("SELECT * FROM alerts").fetchall()
    assert db.is_institutional_row(rth_ok, min_score=0.5) is True
    assert db.is_institutional_row(rth_ok, min_score=0.9) is False

    # Off-hours stamp fails the predicate
    db.insert_alerts(conn, [_alert(contract="Y", event_at="2026-10-19T02:00:00+00:00")])
    off = conn.execute("SELECT * FROM alerts WHERE contract='Y'").fetchone()
    assert db.is_institutional_row(off) is False

    # Non-institutional label fails
    db.insert_alerts(conn, [_alert(contract="Z", inst_label="RETAIL", inst_score=0.9)])
    retail = conn.execute("SELECT * FROM alerts WHERE contract='Z'").fetchone()
    assert db.is_institutional_row(retail) is False


def test_summary_and_recent_alerts_agree(conn):
    db.insert_alerts(conn, [_alert()])
    db.insert_alerts(conn, [_alert(contract="Y", call_put="P", premium_usd=40000.0)])
    # Noise that the predicate must exclude from BOTH views:
    db.insert_alerts(conn, [_alert(contract="N1", event_at="2026-10-19T02:00:00+00:00")])
    db.insert_alerts(conn, [_alert(contract="N2", inst_label="RETAIL", inst_score=0.9)])

    recent = db.recent_alerts(conn, limit=50, days=3650)
    summary = db.weekly_summary(conn, days=3650)
    assert len(recent) == 2
    by_cp = {r["call_put"]: r for r in summary}
    assert by_cp["C"]["alert_count"] == 1
    assert by_cp["P"]["alert_count"] == 1
    assert by_cp["C"]["total_premium"] == pytest.approx(60000.0)
    assert by_cp["P"]["total_premium"] == pytest.approx(40000.0)


def test_recent_alerts_after_id(conn):
    db.insert_alerts(conn, [_alert(contract="A")])
    db.insert_alerts(conn, [_alert(contract="B")])
    rows = conn.execute("SELECT id, contract FROM alerts ORDER BY id").fetchall()
    first_id = rows[0]["id"]
    recent = db.recent_alerts(conn, limit=50, after_id=first_id)
    assert [r["contract"] for r in recent] == ["B"]


def test_no_destructive_purge_helpers():
    # The dead destructive helpers must stay gone; filtering lives in queries.
    assert not hasattr(db, "run_maintenance")
    assert not hasattr(db, "purge_off_hours_alerts")
    assert not hasattr(db, "purge_non_institutional")
    assert hasattr(db, "cleanup_old_snapshots")
    assert hasattr(db, "is_institutional_row")
