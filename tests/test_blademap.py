"""Tests de lo aplicado desde BladeMap: badges BM, lectura
Apertura/Cierre, ciclo Vector, gates, setup plegado y OI sintético.

Reglas fijadas aquí (si cambian, cambiar estos tests con ellas):
- Badge BM: Whale ≥ $1M; Golden ≥ $900K y 2–30% OTM; Silver ≥ $300K
  con etiqueta de convicción (barrido o volumen > OI).
- Vector: racha 1 = FLIP, 2 = FOLLOW, 3+ = CONFIRM; NEUTRAL = NONE.
  Frescura: ≤15 Reciente, ≤45 Activo, ≤90 Envejeciendo, resto Tardía.
- Gates: frescura (retraso declarado), dirección, respaldo (≥2 prints
  votando o racha ≥2), explicado (evidencia no vacía).
- Setup: misma clave dirección:etapa suma al contador; distinta,
  reinicia. ready = gates en verde + etapa CONFIRM.
- OI sintético: OI oficial + flujo firmado del día por strike.
"""

from flow.engine import (
    blademap_badge,
    detect_prints,
    gates,
    merge_day_prints,
    normalize_print,
    open_close_from_doi,
    setup_update,
    synthetic_oi,
    vector_freshness,
    vector_update,
)
from flow.opra import trade_to_print


# --- Badges BladeMap ---

def test_badge_whale_by_size_only():
    assert blademap_badge(1_000_000, None, False) == "WHALE"
    assert blademap_badge(2_500_000, 50.0, False) == "WHALE"


def test_badge_golden_needs_premium_and_otm_band():
    assert blademap_badge(900_000, 10.0, False) == "GOLDEN"
    assert blademap_badge(900_000, 2.0, False) == "GOLDEN"
    assert blademap_badge(900_000, 30.0, False) == "GOLDEN"
    assert blademap_badge(900_000, 1.9, False) is None
    assert blademap_badge(900_000, 30.1, False) is None
    assert blademap_badge(900_000, None, False) is None
    assert blademap_badge(899_999, 10.0, False) is None


def test_badge_silver_needs_premium_and_tag():
    assert blademap_badge(300_000, 5.0, True) == "SILVER"
    assert blademap_badge(300_000, 5.0, False) is None
    assert blademap_badge(299_999, 5.0, True) is None


def test_classify_sets_bm_badge():
    prints = [{
        "contract": "MU261030C01000000", "call_put": "C", "strike": 1100.0,
        "expiry": "2026-10-30", "side": "buy", "delta_vol": 1200,
        "volume": 1200, "open_interest": 100, "d_oi": 100,
        "premium": 1_200_000, "sweep": False, "otm_pct": 10.0,
    }]
    from flow.engine import classify_prints_ai
    classify_prints_ai(prints, spot=1000.0)
    assert prints[0]["bm_badge"] == "WHALE"


# --- Apertura / Cierre ---

def test_open_close_from_doi():
    assert open_close_from_doi(50) == "OPEN"
    assert open_close_from_doi(-50) == "CLOSE"
    assert open_close_from_doi(0) == "UNKNOWN"
    assert open_close_from_doi(None) == "UNKNOWN"


def test_detect_prints_sets_open_close():
    rows = [{
        "contract": "MU261030C01000000", "call_put": "C", "strike": 1000.0,
        "volume": 500, "open_interest": 400, "price": 5.0, "bid": 4.9,
        "ask": 5.1, "last": 5.09, "expiry": "2026-10-30", "spot": 1000.0,
    }]
    prints = detect_prints(rows, {}, {"MU261030C01000000": 300}, spot=1000.0)
    assert prints and prints[0]["open_close"] == "OPEN"
    prints = detect_prints(rows, {}, {"MU261030C01000000": 500}, spot=1000.0)
    assert prints and prints[0]["open_close"] == "CLOSE"


def test_merge_refreshes_open_close():
    day = [{
        "contract": "MU261030C01000000", "call_put": "C", "strike": 1000.0,
        "expiry": "2026-10-30", "side": "buy", "delta_vol": 100,
        "volume": 500, "open_interest": 400, "d_oi": 100,
        "open_close": "OPEN", "premium": 50_000, "sweep": False,
    }]
    new = [dict(day[0], delta_vol=50, volume=550, open_interest=350, d_oi=-50,
                is_new_position=False, conviction=50)]
    merged = merge_day_prints(day, new, "2026-10-30T10:00:00-04:00")
    assert merged[0]["open_close"] == "CLOSE"


def test_opra_open_close_from_trade_direction():
    base = {
        "contract_symbol": "O:MU261030C01000000", "option_type": "call",
        "strike_price": 1000.0, "premium_value": 60_000, "size": 100,
        "volume": 100, "open_interest": 500, "expiry_date": "2026-10-30",
        "timestamp": 1793278800, "unique_id": "u1",
    }
    assert trade_to_print({**base, "trade_direction": "BTO"})["open_close"] == "OPEN"
    assert trade_to_print({**base, "trade_direction": "STO"})["open_close"] == "OPEN"
    assert trade_to_print({**base, "trade_direction": "BTC"})["open_close"] == "CLOSE"
    assert trade_to_print({**base, "trade_direction": "STC"})["open_close"] == "CLOSE"
    assert trade_to_print({**base, "trade_direction": ""})["open_close"] == "UNKNOWN"


def test_normalize_keeps_opra_open_close():
    p = {"contract": "X", "call_put": "C", "strike": 1.0, "expiry": "2026-10-30",
         "side": "buy", "delta_vol": 10, "volume": 10, "open_interest": 0,
         "d_oi": None, "premium": 60_000, "open_close": "OPEN"}
    normalize_print(p, spot=1000.0)
    assert p["open_close"] == "OPEN"


# --- Vector ciclo de vida ---

NOW = "2026-10-30T14:00:00-04:00"


def test_vector_first_scan_is_flip():
    v = vector_update(None, "ALCISTA", 1, NOW)
    assert v["stage"] == "FLIP"
    assert v["flipped_at"] == NOW
    assert v["freshness"] == "FRESH"
    assert v["stage_drops"] == 0


def test_vector_follow_then_confirm_by_streak():
    v1 = vector_update(None, "ALCISTA", 1, NOW)
    v2 = vector_update(v1, "ALCISTA", 2, "2026-10-30T14:00:30-04:00")
    assert v2["stage"] == "FOLLOW"
    assert v2["flipped_at"] == NOW  # el giro no se mueve
    v3 = vector_update(v2, "ALCISTA", 3, "2026-10-30T14:01:00-04:00")
    assert v3["stage"] == "CONFIRM"


def test_vector_direction_change_resets_and_counts_drop():
    v1 = vector_update(None, "ALCISTA", 3, NOW)
    v2 = vector_update(v1, "BAJISTA", 1, "2026-10-30T14:05:00-04:00")
    assert v2["stage"] == "FLIP"
    assert v2["flipped_at"] == "2026-10-30T14:05:00-04:00"
    assert v2["stage_drops"] == 1


def test_vector_neutral_has_no_stage():
    v1 = vector_update(None, "ALCISTA", 2, NOW)
    v2 = vector_update(v1, "NEUTRAL", 1, "2026-10-30T14:05:00-04:00")
    assert v2["stage"] == "NONE"
    assert v2["freshness"] is None


def test_vector_legacy_rebuild_uses_stored_streak():
    # Estado viejo sin vector guardado pero con racha 3: ya confirmado.
    v = vector_update(None, "ALCISTA", 3, NOW)
    assert v["stage"] == "CONFIRM"


def test_vector_freshness_tiers():
    assert vector_freshness(0) == ("FRESH", "Reciente")
    assert vector_freshness(15) == ("FRESH", "Reciente")
    assert vector_freshness(16) == ("ACTIVE", "Activo")
    assert vector_freshness(45) == ("ACTIVE", "Activo")
    assert vector_freshness(46) == ("AGING", "Envejeciendo")
    assert vector_freshness(90) == ("AGING", "Envejeciendo")
    assert vector_freshness(91) == ("LATE", "Entrada tardía")
    assert vector_freshness(None) == (None, None)


# --- Gates y setup ---

def _verdict(direction="ALCISTA", n_voted=3, streak=3):
    return {
        "direction": direction,
        "evidence": "Compra de calls strike 1000 vto. 2026-10-30 por $60,000 de prima.",
        "n_voted": n_voted, "streak": streak,
    }


def test_gates_all_green():
    gs = gates(_verdict(), 60,
               trend={"position": "above", "sma5": 100.0, "last_bar_et": "2026-10-29 15:30 ET"})
    assert all(g["ok"] for g in gs)
    assert [g["key"] for g in gs] == ["fresh", "direction", "backing", "explained", "sma"]


def test_gates_fail_without_direction_or_delay_or_backing():
    gs = gates(_verdict(direction="NEUTRAL"), None)
    by_key = {g["key"]: g["ok"] for g in gs}
    assert by_key["fresh"] is False
    assert by_key["direction"] is False
    assert by_key["sma"] is None  # sin tendencia, el gate SMA queda en "?"
    gs = gates(_verdict(n_voted=1, streak=1), 60)
    by_key = {g["key"]: g["ok"] for g in gs}
    assert by_key["backing"] is False


def test_setup_folds_same_key_and_resets_on_change():
    gs = gates(_verdict(), 60,
               trend={"position": "above", "sma5": 100.0, "last_bar_et": "2026-10-29 15:30 ET"})
    s1 = setup_update(None, "ALCISTA", "CONFIRM", gs)
    assert s1 == {"key": "ALCISTA:CONFIRM", "count": 1, "ready": True,
                  "label": "SETUP CONFIRMADO"}
    s2 = setup_update(s1, "ALCISTA", "CONFIRM", gs)
    assert s2["count"] == 2 and s2["ready"] is True
    s3 = setup_update(s2, "ALCISTA", "FOLLOW", gs)
    assert s3["count"] == 1 and s3["ready"] is False  # sin Confirm no hay ready


# --- OI sintético ---

def test_synthetic_oi_adds_signed_day_flow():
    levels_ = {
        "call_oi": {1000.0: 5000, 1100.0: 3000},
        "put_oi": {900.0: 4000},
    }
    prints = [
        {"call_put": "C", "strike": 1000.0, "side": "buy", "delta_vol": 100},
        {"call_put": "C", "strike": 1000.0, "side": "sell", "delta_vol": 30},
        {"call_put": "P", "strike": 900.0, "side": "sell", "delta_vol": 200},
        {"call_put": "C", "strike": 1000.0, "side": "unknown", "delta_vol": 999},
    ]
    syn = synthetic_oi(levels_, prints)
    assert syn["call"]["1000"] == 5070
    assert syn["call"]["1100"] == 3000
    assert syn["put"]["900"] == 3800
    assert syn["top_call"] == {"strike": 1000.0, "oi": 5000, "syn_oi": 5070}
    assert syn["top_put"] == {"strike": 900.0, "oi": 4000, "syn_oi": 3800}


def test_synthetic_oi_none_without_levels():
    assert synthetic_oi(None, []) is None
