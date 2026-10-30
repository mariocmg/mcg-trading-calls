"""Tests del sidecar TV (tendencia horaria) y del gate SMA.

El subprocess real solo se prueba en vivo (ver README); aquí se mockea
_run_sidecar para que la suite no dependa de Node ni de red.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from flow import engine, tv_hourly
from flow.server import _enrich_legacy_state

ET = ZoneInfo("America/New_York")

SAMPLE = {
    "ticker": "MU", "exchange": "NASDAQ", "sma5": 935.31,
    "last_close": 934.06, "last_bar_et": "2026-10-29 15:30 ET",
    "n_bars": 70, "source": "tradingview",
}


@pytest.fixture(autouse=True)
def _clear_cache():
    tv_hourly._CACHE.clear()
    yield
    tv_hourly._CACHE.clear()


def _verdict(direction="ALCISTA"):
    return {"direction": direction, "n_voted": 3, "streak": 3,
            "evidence": "puts vendidas"}


def test_snapshot_position_and_distance(monkeypatch):
    monkeypatch.setattr(tv_hourly, "_run_sidecar", lambda t: (dict(SAMPLE), None))
    above = tv_hourly.snapshot("MU", spot=940.0)
    assert above["position"] == "above"
    assert above["distance_pct"] == pytest.approx(0.5, abs=0.01)
    assert above["stale"] is False and above["error"] is None
    below = tv_hourly.snapshot("MU", spot=930.0)
    assert below["position"] == "below"
    flat = tv_hourly.snapshot("MU", spot=935.31)
    assert flat["position"] == "flat"


def test_snapshot_uses_cache(monkeypatch):
    count = {"n": 0}

    def counting(t):
        count["n"] += 1
        return dict(SAMPLE), None

    monkeypatch.setattr(tv_hourly, "_run_sidecar", counting)
    tv_hourly.snapshot("MU", spot=940.0)
    tv_hourly.snapshot("MU", spot=940.0)
    assert count["n"] == 1


def test_snapshot_refresh_after_ttl(monkeypatch):
    count = {"n": 0}

    def counting(t):
        count["n"] += 1
        return dict(SAMPLE), None

    monkeypatch.setattr(tv_hourly, "_run_sidecar", counting)
    tv_hourly.snapshot("MU", spot=940.0)
    assert count["n"] == 1
    # Simular 16 min después: el TTL es de 15 min.
    real_time = tv_hourly.time.time
    monkeypatch.setattr(tv_hourly.time, "time", lambda: real_time() + 1000)
    tv_hourly.snapshot("MU", spot=940.0)
    assert count["n"] == 2


def test_snapshot_failure_without_cache(monkeypatch):
    monkeypatch.setattr(tv_hourly, "_run_sidecar", lambda t: (None, "Node no encontrado"))
    out = tv_hourly.snapshot("MU", spot=940.0)
    assert out["sma5"] is None and out["position"] is None
    assert out["error"] == "Node no encontrado" and out["stale"] is False


def test_snapshot_failure_keeps_stale_cache(monkeypatch):
    monkeypatch.setattr(tv_hourly, "_run_sidecar", lambda t: (dict(SAMPLE), None))
    tv_hourly.snapshot("MU", spot=940.0)
    monkeypatch.setattr(tv_hourly, "_run_sidecar", lambda t: (None, "red caída"))
    # Forzar refresco: TTL vencido.
    real_time = tv_hourly.time.time
    monkeypatch.setattr(tv_hourly.time, "time", lambda: real_time() + 1000)
    out = tv_hourly.snapshot("MU", spot=940.0)
    assert out["sma5"] == 935.31 and out["stale"] is True
    assert out["error"] == "red caída"


def test_latest_completed_hour():
    assert tv_hourly._latest_completed_hour_start(
        datetime(2026, 10, 29, 10, 20, tzinfo=ET)
    ) == datetime(2026, 10, 29, 9, 0, tzinfo=ET)
    assert tv_hourly._latest_completed_hour_start(
        datetime(2026, 10, 29, 0, 20, tzinfo=ET)
    ) == datetime(2026, 10, 28, 23, 0, tzinfo=ET)


def test_gates_sma_cases():
    trend_above = {**SAMPLE, "position": "above"}
    g = engine.gates(_verdict("ALCISTA"), 60, trend=trend_above)
    sma = [x for x in g if x["key"] == "sma"][0]
    assert sma["ok"] is True
    g = engine.gates(_verdict("BAJISTA"), 60, trend=trend_above)
    sma = [x for x in g if x["key"] == "sma"][0]
    assert sma["ok"] is False  # bajista con spot por encima: no pasa
    g = engine.gates(_verdict("ALCISTA"), 60, trend=None)
    sma = [x for x in g if x["key"] == "sma"][0]
    assert sma["ok"] is None  # "?" en la UI, no un fallo rojo
    g = engine.gates(_verdict("ALCISTA"), 60,
                     trend={"sma5": None, "position": None, "error": "Node no encontrado"})
    sma = [x for x in g if x["key"] == "sma"][0]
    assert sma["ok"] is None and "Node" in sma["detail"]


def test_setup_blocked_without_sma():
    g = engine.gates(_verdict("ALCISTA"), 60, trend=None)
    assert engine.setup_update(None, "ALCISTA", "CONFIRM", g)["ready"] is False
    g = engine.gates(_verdict("ALCISTA"), 60,
                     trend={**SAMPLE, "position": "above"})
    assert engine.setup_update(None, "ALCISTA", "CONFIRM", g)["ready"] is True


def test_enrich_never_calls_sidecar(monkeypatch):
    def _boom(t):
        raise AssertionError("enrich no debe llamar al sidecar")

    monkeypatch.setattr(tv_hourly, "_run_sidecar", _boom)
    state = {
        "verdict": _verdict("ALCISTA"), "prints": [], "levels": None,
        "trend": {**SAMPLE, "position": "above"},
        "positioning": {"dummy": True},
        "prints_delay_minutes": 60, "updated_at": "2026-10-29T20:00:00-04:00",
    }
    out = _enrich_legacy_state(state)
    assert out["trend"]["sma5"] == 935.31
    assert any(x["key"] == "sma" for x in out["gates"])
