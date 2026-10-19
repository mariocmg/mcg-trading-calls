"""A crashed scan must not leave half-written rows behind."""

import pytest

from flow import db
import flow.scan_runner as sr


@pytest.fixture()
def cfg_path(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "watchlist: [MU]\n"
        f"database: {tmp_path / 't.db'}\n"
        "data_provider: yahoo\n"
        "max_expiries: 1\n"
        "heuristics:\n"
        "  min_premium_usd: 1\n"
        "  min_vol_oi_ratio: 0.1\n"
        "  min_volume: 1\n"
        "  min_delta_volume: 1\n"
        "  min_dte: 0\n"
        "  max_dte: 45\n"
        "  max_moneyness: 0.2\n"
    )
    return p


def _row():
    return {
        "underlying": "MU",
        "expiry": "2026-11-20",
        "contract": "MU261120C00100000",
        "call_put": "C",
        "strike": 100.0,
        "last": 2.0,
        "bid": 1.9,
        "ask": 2.1,
        "price": 2.0,
        "volume": 1000,
        "open_interest": 500,
        "implied_volatility": 0.5,
        "dte": 30,
        "spot": 95.0,
        "side_bias": "buyish",
        "last_trade_time": None,
        "fetched_at": "2026-10-19T12:00:00+00:00",
        "source": "yahoo",
    }


def test_failed_scan_leaves_no_partial_rows(monkeypatch, cfg_path, tmp_path):
    monkeypatch.setattr(sr, "fetch_watchlist", lambda *a, **k: [_row()])

    def boom(conn, alerts):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(sr.db, "insert_alerts", boom)

    with pytest.raises(RuntimeError, match="db exploded"):
        sr.run_scan(str(cfg_path))

    conn = db.connect(tmp_path / "t.db")
    try:
        assert conn.execute("SELECT COUNT(*) c FROM scans").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM alerts").fetchone()["c"] == 0
    finally:
        conn.close()
