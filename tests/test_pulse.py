"""Tests del filtro Pulse: score 0-10 y qué prints cuentan como institucionales.

El score decide el veredicto y la tabla; si sus reglas cambian, estos
tests deben cambiar con ellas.
"""

from flow.engine import PULSE_KEEP_THRESHOLD, pulse_score, verdict


def mkprint(**kw):
    """Print mínimo con valores neutros (score 0 si no se añade nada)."""
    p = {
        "contract": "MU261030C01000000",
        "call_put": "C",
        "strike": 1000.0,
        "expiry": "2026-10-30",
        "side": "buy",
        "premium": 50_000,
        "delta_vol": 10,
        "open_interest": 1000,
        "otm_pct": None,
        "open_close": "UNKNOWN",
        "d_oi": None,
        "sweep": False,
    }
    p.update(kw)
    return p


def score_one(**kw):
    (p,) = pulse_score([mkprint(**kw)])
    return p


def test_premium_tiers():
    assert score_one(premium=1_000_000)["pulse_score"] == 3
    assert score_one(premium=500_000)["pulse_score"] == 2
    assert score_one(premium=250_000)["pulse_score"] == 1
    assert score_one(premium=50_000)["pulse_score"] == 0


def test_volume_vs_oi_tiers():
    assert score_one(delta_vol=1000, open_interest=1000)["pulse_score"] == 2
    assert score_one(delta_vol=500, open_interest=1000)["pulse_score"] == 1
    assert score_one(delta_vol=499, open_interest=1000)["pulse_score"] == 0
    assert score_one(delta_vol=1000, open_interest=0)["pulse_score"] == 0


def test_sweep_and_opening_and_otm_band():
    assert score_one(sweep=True)["pulse_score"] == 1
    assert score_one(open_close="OPEN")["pulse_score"] == 1
    assert score_one(d_oi=100)["pulse_score"] == 1
    assert score_one(otm_pct=2.0)["pulse_score"] == 1
    assert score_one(otm_pct=30.0)["pulse_score"] == 1
    assert score_one(otm_pct=1.9)["pulse_score"] == 0
    assert score_one(otm_pct=30.1)["pulse_score"] == 0
    assert score_one(otm_pct=-5.0)["pulse_score"] == 0  # ITM no puntúa


def test_closing_and_unknown_side_never_keep():
    p = score_one(premium=5_000_000, sweep=True, open_close="CLOSE")
    assert p["pulse_score"] == 0 and p["pulse_keep"] is False
    p = score_one(premium=5_000_000, sweep=True, d_oi=-100)
    assert p["pulse_score"] == 0 and p["pulse_keep"] is False
    p = score_one(premium=5_000_000, sweep=True, side="unknown")
    assert p["pulse_score"] == 0 and p["pulse_keep"] is False


def test_threshold_edge():
    # 3 (prima) + 2 (vol ≥ OI) + 1 (barrido) + 1 (apertura) = 7 -> pasa
    p = score_one(premium=1_000_000, delta_vol=1000, open_interest=1000,
                  sweep=True, open_close="OPEN")
    assert p["pulse_score"] == 7 and p["pulse_keep"] is True
    # Sin la apertura: 6 -> no pasa
    p = score_one(premium=1_000_000, delta_vol=1000, open_interest=1000,
                  sweep=True)
    assert p["pulse_score"] == 6 and p["pulse_keep"] is False
    assert PULSE_KEEP_THRESHOLD == 7


def test_score_capped_at_10():
    # Mano llena con cluster: 3+2+1+2+1+1 = 10 (el tope nunca se supera
    # con estos componentes; el min(10, ...) es defensivo ante cambios).
    base = dict(premium=5_000_000, delta_vol=5000, open_interest=1000,
                sweep=True, open_close="OPEN", otm_pct=10.0,
                contract="MU261030C01000000")
    prints = [mkprint(**base, trade_ts=1_700_000_000),
              mkprint(**base, trade_ts=1_700_000_030),
              mkprint(**base, trade_ts=1_700_000_060)]
    out = pulse_score(prints)
    assert all(p["pulse_score"] == 10 for p in out)
    assert all(p["pulse_keep"] for p in out)


def test_cluster_90s():
    base = dict(premium=50_000, delta_vol=10, open_interest=1000)
    prints = [
        mkprint(**base, trade_ts=1_700_000_000, contract="MU261030C01000000"),
        mkprint(**base, trade_ts=1_700_000_040, contract="MU261030C01000000"),
        mkprint(**base, trade_ts=1_700_000_080, contract="MU261030C01000000"),
    ]
    out = pulse_score(prints)
    assert all(p["pulse_score"] == 2 for p in out)
    assert all("cluster 90s" in p["pulse_reasons"] for p in out)
    # Fuera de ventana: sin bonus
    prints = [
        mkprint(**base, trade_ts=1_700_000_000, contract="MU261030C01000000"),
        mkprint(**base, trade_ts=1_700_000_200, contract="MU261030C01000000"),
        mkprint(**base, trade_ts=1_700_000_400, contract="MU261030C01000000"),
    ]
    out = pulse_score(prints)
    assert all(p["pulse_score"] == 0 for p in out)
    # Otro contrato no cuenta para el cluster
    prints = [
        mkprint(**base, trade_ts=1_700_000_000, contract="MU261030C01000000"),
        mkprint(**base, trade_ts=1_700_000_010, contract="MU261030P00950000"),
        mkprint(**base, trade_ts=1_700_000_020, contract="MU261030C01000000"),
    ]
    out = pulse_score(prints)
    assert all(p["pulse_score"] == 0 for p in out)


def test_chain_prints_without_timestamp_dont_crash():
    p = score_one(premium=1_000_000)  # sin trade_ts: cluster 0, sin error
    assert p["pulse_score"] == 3


def test_verdict_uses_only_institutional_prints():
    # Sin filtro, el neto sería alcista (una ballena compradora enorme).
    # Con filtro, esa ballena es un cierre y no vota: el neto kept es bajista.
    bull = mkprint(premium=5_000_000, side="buy", call_put="C",
                   open_close="CLOSE")
    bear1 = mkprint(premium=600_000, side="buy", call_put="P",
                    delta_vol=1000, open_interest=1000, sweep=True,
                    open_close="OPEN", otm_pct=10.0,
                    contract="MU261030P00950000")
    bear2 = mkprint(premium=600_000, side="buy", call_put="P",
                    delta_vol=1000, open_interest=1000, sweep=True,
                    open_close="OPEN", otm_pct=10.0,
                    contract="MU261030P00950001")
    prints = pulse_score([bull, bear1, bear2])
    kept = [p for p in prints if p["pulse_keep"]]
    assert len(kept) == 2
    v = verdict(kept)
    assert v["direction"] == "BAJISTA"
    # El veredicto sin filtrar habría sido alcista: el filtro cambia el signo
    v_all = verdict(prints)
    assert v_all["direction"] == "ALCISTA"
