"""Tests del motor: reglas de inusualidad, veredicto, niveles y parseo OCC.

Pocos y al grano: si estas reglas cambian, estos tests deben cambiar con ellas.
"""

from flow.engine import (
    book_stats,
    campaigns,
    classify_prints_ai,
    conviction,
    detect_prints,
    infer_side,
    invalidation,
    is_unusual,
    levels,
    merge_day_prints,
    normalize_print,
    otm_pct,
    positioning_map,
    side_bias,
    stage,
    targets,
    verdict,
)
from flow.fetch import cboe_url_for, clean_ticker, parse_occ


def row(contract="MU261030C01000000", call_put="C", strike=1000.0, volume=500,
        oi=400, price=5.0, bid=4.9, ask=5.1, last=5.09, expiry="2026-10-30"):
    return {
        "contract": contract, "call_put": call_put, "strike": strike,
        "volume": volume, "open_interest": oi, "price": price,
        "bid": bid, "ask": ask, "last": last, "expiry": expiry, "dte": 2,
    }


# --- Parseo OCC ---
def test_parse_occ():
    p = parse_occ("MU261030C01000000")
    assert p == {"expiry": "2026-10-30", "call_put": "C", "strike": 1000.0, "dte": p["dte"]}
    assert parse_occ("BASURA") is None
    assert parse_occ("MU261030P00950000")["call_put"] == "P"


# --- Sesgo compra/venta (probabilístico) ---
def test_side_bias():
    assert side_bias(4.9, 5.1, 5.09) == "buy"    # pegado al ask
    assert side_bias(4.9, 5.1, 4.91) == "sell"   # pegado al bid
    assert side_bias(4.9, 5.1, 5.0) == "unknown"  # a mitad: no vota
    assert side_bias(None, 5.1, 5.09) == "unknown"
    assert side_bias(5.1, 4.9, 5.0) == "unknown"  # bid >= ask: quote inválida


# --- Reglas de inusualidad ---
def test_is_unusual():
    assert is_unusual(500, 500, 400, 250_000)  # prima grande + vol >= OI
    assert is_unusual(300, 300, 1000, 150_000) is False  # Δvol < 0,5×OI y vol < OI
    assert is_unusual(600, 600, 1000, 300_000)  # Δvol >= 0,5×OI aunque vol < OI
    assert is_unusual(10, 10, 0, 100_000) is False  # menos de 50 contratos
    assert is_unusual(500, 500, 400, 49_999) is False  # prima bajo el mínimo
    assert is_unusual(100, 100, 0, 60_000)  # OI 0: pasa el filtro de OI


def test_detect_prints_uses_delta_and_caps_by_premium():
    rows = [
        row("A", volume=1000, oi=100, price=10.0),   # Δ=1000, prima 1.000.000
        row("B", volume=100, oi=50, price=1.0),     # Δ=100, prima 10.000 -> fuera
        row("C", volume=200, oi=100, price=12.0, bid=11.9, ask=12.1, last=12.0),
    ]
    prints = detect_prints(rows, {"A": 0, "B": 0, "C": 150})
    assert [p["contract"] for p in prints] == ["A", "C"]  # ordenados por prima
    assert prints[0]["delta_vol"] == 1000 and prints[0]["premium"] == 1_000_000
    assert prints[1]["delta_vol"] == 50  # 200 - 150 del snapshot


def test_detect_prints_first_scan_uses_day_volume():
    # Sin snapshot previo, Δvolumen = volumen del día.
    prints = detect_prints([row(volume=800, oi=100, price=5.0)], {})
    assert prints and prints[0]["delta_vol"] == 800


# --- Veredicto ---
def test_verdict_thresholds():
    bull = [{"call_put": "C", "side": "buy", "premium": 600_000, "strike": 1000, "expiry": "2026-10-30"}]
    assert verdict(bull)["direction"] == "ALCISTA"
    bear = [{"call_put": "P", "side": "buy", "premium": 600_000, "strike": 1000, "expiry": "2026-10-30"}]
    assert verdict(bear)["direction"] == "BAJISTA"
    # Venta de puts también es alcista; venta de calls, bajista.
    assert verdict([{"call_put": "P", "side": "sell", "premium": 600_000, "strike": 1, "expiry": "x"}])["direction"] == "ALCISTA"
    assert verdict([{"call_put": "C", "side": "sell", "premium": 600_000, "strike": 1, "expiry": "x"}])["direction"] == "BAJISTA"
    small = [{"call_put": "C", "side": "buy", "premium": 100_000, "strike": 1, "expiry": "x"}]
    assert verdict(small)["direction"] == "NEUTRAL"  # bajo el umbral de 500k
    unknown = [{"call_put": "C", "side": "unknown", "premium": 5_000_000, "strike": 1, "expiry": "x"}]
    v = verdict(unknown)
    assert v["direction"] == "NEUTRAL" and v["net"] == 0  # sin sesgo no vota


def test_verdict_evidence_names_the_top_print():
    prints = [{"call_put": "C", "side": "buy", "premium": 800_000, "strike": 1000, "expiry": "2026-10-30"}]
    assert "Compra de calls strike 1000" in verdict(prints)["evidence"]
    assert verdict([])["evidence"] == "Sin prints inusuales hoy."


# --- Niveles ---
def test_levels_walls_and_max_pain():
    rows = [
        row("C1", "C", 1000.0, oi=5000, expiry="2026-10-30"),
        row("C2", "C", 1010.0, oi=1000, expiry="2026-10-30"),
        row("P1", "P", 990.0, oi=3000, expiry="2026-10-30"),
        row("P2", "P", 980.0, oi=800, expiry="2026-10-30"),
        row("P3", "P", 1000.0, oi=100, expiry="2026-10-30"),  # rompe el empate 990/1000
        row("LATER", "C", 1000.0, oi=99999, expiry="2026-11-20"),  # otro vencimiento: ignorado
    ]
    lv = levels(rows)
    assert lv["expiry"] == "2026-10-30"
    assert lv["call_wall"] == {"strike": 1000.0, "oi": 5000}
    assert lv["put_wall"] == {"strike": 990.0, "oi": 3000}
    # Max pain único en 1000: en 990 el put 1000 vale 10 × 100 de OI.
    assert lv["max_pain"]["strike"] == 1000.0
    assert levels([]) is None


# --- ΔOI y posición nueva ---
def test_doi_and_new_position():
    r = row("X", volume=1000, oi=500, price=10.0)
    (p,) = detect_prints([r], {}, {"X": 300})
    assert p["d_oi"] == 200 and p["is_new_position"] is True
    (p,) = detect_prints([r], {}, {"X": 700})
    assert p["d_oi"] == -200 and p["is_new_position"] is False  # cierre, no apertura
    (p,) = detect_prints([r], {})
    assert p["d_oi"] is None and p["is_new_position"] is False  # sin referencia


def test_snapshot_stores_oi(tmp_path, monkeypatch):
    import flow.store as store

    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    store.save_snapshot("2026-10-28", {"A": 100}, {"A": 50})
    date, volumes, oi = store.load_snapshot()
    assert (date, volumes, oi) == ("2026-10-28", {"A": 100}, {"A": 50})


# --- Barridos ---
def _sweep_row(contract, strike):
    return row(contract, "C", strike, volume=1000, oi=100, price=10.0,
               bid=9.9, ask=10.1, last=10.09, expiry="2026-10-30")


def test_sweeps_flagged_only_in_groups_of_three():
    rows = [_sweep_row("S1", 1000.0), _sweep_row("S2", 1005.0), _sweep_row("S3", 1010.0)]
    prints = detect_prints(rows, {}, {}, spot=1000.0)
    assert len(prints) == 3 and all(p["sweep"] for p in prints)
    # Solo 2 prints: no hay barrido.
    assert not any(p["sweep"] for p in detect_prints(rows[:2], {}, {}, spot=1000.0))
    # Mismo lado pero vencimientos distintos: tampoco.
    rows2 = [_sweep_row("S1", 1000.0), _sweep_row("S2", 1005.0),
             _sweep_row("S3", 1010.0, )]
    rows2[2] = row("S3", "C", 1010.0, volume=1000, oi=100, price=10.0,
                   bid=9.9, ask=10.1, last=10.09, expiry="2026-11-06")
    assert not any(p["sweep"] for p in detect_prints(rows2, {}, {}, spot=1000.0))
    # Sin sesgo (last a mitad de horquilla): no forma barrido.
    rows3 = [row(f"U{i}", "C", 1000.0 + i, volume=1000, oi=100, price=10.0,
                 bid=9.9, ask=10.1, last=10.0, expiry="2026-10-30") for i in range(3)]
    assert not any(p["sweep"] for p in detect_prints(rows3, {}, {}, spot=1000.0))


# --- Convicción ---
def test_conviction_bounds_and_weights():
    assert conviction(50_000, 0.5, None, "unknown", False) == 30  # base 20 + vol 10
    assert conviction(5_000_000, 2.0, 100, "buy", True) == 100  # tope, no 110
    big = conviction(5_000_000, 2.0, 100, "buy", True)
    small = conviction(60_000, 0.5, None, "unknown", False)
    assert big > small
    assert conviction(100_000, 0.5, -50, "buy", False) < conviction(100_000, 0.5, 50, "buy", False)


# --- Veredicto ponderado ---
def test_weighted_verdict():
    bear_low_conv = [{"call_put": "P", "side": "buy", "premium": 600_000,
                      "strike": 1000, "expiry": "x", "conviction": 50}]
    v = verdict(bear_low_conv)
    assert v["direction"] == "NEUTRAL"  # ponderado 300k < umbral 500k
    assert v["net"] == -300_000 and v["net_unweighted"] == -600_000
    bear_high_conv = [{"call_put": "P", "side": "buy", "premium": 600_000,
                       "strike": 1000, "expiry": "x", "conviction": 100}]
    assert verdict(bear_high_conv)["direction"] == "BAJISTA"
    # Sin clave conviction (prints viejos): cuentan al 100%.
    assert verdict([{"call_put": "C", "side": "buy", "premium": 600_000,
                     "strike": 1, "expiry": "x"}])["direction"] == "ALCISTA"


def test_stage_and_invalidation():
    assert stage("ALCISTA", 80, 4, 5) == ("CONFIRMED", "Confirmado", 3)
    assert stage("ALCISTA", 50, 2, 2) == ("BUILDING", "Construyendo", 2)
    assert stage("NEUTRAL", 90, 9, 9) == ("EARLY", "Temprano", 1)
    lv = {"call_wall": {"strike": 1010.0, "oi": 4000},
          "put_wall": {"strike": 990.0, "oi": 3000}}
    inv = invalidation("ALCISTA", lv, 1000.0)
    assert inv["price"] == 990.0 and inv["arrow"] == "down"
    inv = invalidation("BAJISTA", lv, 1000.0)
    assert inv["price"] == 1010.0 and inv["arrow"] == "up"
    assert invalidation("NEUTRAL", lv, 1000.0) is None


def test_invalidation_picks_nearest_side_not_biggest_wall():
    # Muro mayor en 1010, pero el nivel opuesto más cercano por encima es 1005.
    lv = {"call_wall": {"strike": 1010.0, "oi": 4000},
          "put_wall": {"strike": 990.0, "oi": 3000},
          "call_oi": {1005.0: 500, 1010.0: 4000, 1020.0: 100},
          "put_oi": {990.0: 3000, 995.0: 800}}
    inv = invalidation("BAJISTA", lv, 1000.0)
    assert inv["price"] == 1005.0 and inv["arrow"] == "up" and inv["distance_pct"] == 0.5
    inv = invalidation("ALCISTA", lv, 1000.0)
    assert inv["price"] == 995.0 and inv["arrow"] == "down"


def test_normalize_print_fills_legacy_fields():
    legacy = {"contract": "X", "call_put": "C", "strike": 1000.0, "side": "buy",
              "premium": 600_000, "delta_vol": 500, "open_interest": 400}
    p = normalize_print(dict(legacy))
    assert p["d_oi"] is None and p["is_new_position"] is False
    assert p["sweep"] is False and p["times_seen"] == 1
    assert 0 < p["conviction"] <= 100


# --- Acumulación del día y campañas ---
def _day_print(contract, premium, times_seen=1, conviction=50):
    return {
        "contract": contract, "call_put": "C", "strike": 1000.0, "expiry": "2026-10-30",
        "side": "buy", "premium": premium, "delta_vol": 100, "volume": 500,
        "open_interest": 400, "d_oi": 10, "is_new_position": True,
        "sweep": False, "conviction": conviction, "times_seen": times_seen,
        "first_seen": "2026-10-28T14:02:00-04:00", "last_seen": "2026-10-28T14:47:00-04:00",
    }


def test_merge_day_prints_accumulates():
    day = [_day_print("A", 100_000)]
    new = [_day_print("A", 50_000), _day_print("B", 200_000)]
    merged = merge_day_prints(day, new, "2026-10-28T15:00:00-04:00")
    a = [p for p in merged if p["contract"] == "A"][0]
    assert a["times_seen"] == 2 and a["premium"] == 150_000
    assert a["first_seen"] == "2026-10-28T14:02:00-04:00"
    assert a["last_seen"] == "2026-10-28T15:00:00-04:00"
    assert [p["contract"] for p in merged] == ["B", "A"]  # orden por prima


def test_campaigns_only_repeated_contracts():
    prints = [_day_print("A", 300_000, times_seen=3, conviction=80),
              _day_print("B", 500_000, times_seen=1, conviction=90)]
    camps = campaigns(prints)
    assert [c["contract"] for c in camps] == ["A"]  # B visto 1 vez: no es campaña
    assert camps[0]["n_prints"] == 3 and camps[0]["label"] == "FUERTE"


# --- Book y targets ---
def test_book_stats():
    prints = [
        {"call_put": "C", "strike": 1000.0, "premium": 200_000, "dte": 3, "is_new_position": True},
        {"call_put": "C", "strike": 1010.0, "premium": 100_000, "dte": 10, "is_new_position": False},
        {"call_put": "P", "strike": 990.0, "premium": 100_000, "dte": 30, "is_new_position": False},
    ]
    b = book_stats(prints)
    assert (b["call_premium"], b["put_premium"], b["net"]) == (300_000, 100_000, 200_000)
    assert b["avg_strike"] == (1000 * 200_000 + 1010 * 100_000 + 990 * 100_000) / 400_000
    assert b["dte_buckets"]["0-7"]["total"] == 200_000
    assert b["dte_buckets"]["8-21"]["total"] == 100_000
    assert b["dte_buckets"]["22-45"]["total"] == 100_000
    assert (b["new_position_premium"], b["new_position_count"]) == (200_000, 1)


def test_targets_from_today_flow():
    prints = [
        {"call_put": "C", "side": "buy", "strike": 1010.0, "premium": 300_000},
        {"call_put": "C", "side": "buy", "strike": 1020.0, "premium": 100_000},
        {"call_put": "P", "side": "buy", "strike": 980.0, "premium": 250_000},
        {"call_put": "P", "side": "sell", "strike": 970.0, "premium": 999_000},  # venta: no es objetivo bajista
    ]
    lv = {"put_wall": {"strike": 990.0, "oi": 3000}}
    t = targets(prints, lv)
    assert t["bull"]["strike"] == 1010.0 and t["bull"]["premium"] == 300_000
    assert t["bear"]["strike"] == 980.0
    assert t["hedge"] == {"strike": 990.0, "oi": 3000}


# --- OTM ---
def test_otm_pct():
    assert otm_pct("C", 1050.0, 1000.0) == 5.0   # call fuera de dinero
    assert otm_pct("P", 950.0, 1000.0) == 5.0    # put fuera de dinero
    assert otm_pct("C", 950.0, 1000.0) == -5.0   # call dentro de dinero
    assert otm_pct("C", 1000.0, None) is None


# --- Inferencia Híbrida de Agresión (horquilla + theo Cboe) ---
def test_infer_side_uses_theo_when_quote_shifts():
    # A mitad del spread (pos=0.60), side_bias clásico da 'unknown',
    # pero si last (5.02) pagó claramente sobre theo (4.92), infer_side detecta compra.
    assert side_bias(4.90, 5.10, 5.02) == "unknown"
    assert infer_side(4.90, 5.10, 5.02, theo=4.95) == "buy"
    assert infer_side(4.90, 5.10, 4.98, theo=5.05) == "sell"
    assert infer_side(4.90, 5.10, 5.00, theo=5.00) == "unknown"


# --- Clasificador IA de Intención Institucional (BladeMap Pulse) ---
def test_classify_prints_ai_archetypes_and_hedge():
    prints = [
        # Ballena alcista en Calls
        {
            "contract": "MU261030C01000000", "call_put": "C", "strike": 1000.0,
            "expiry": "2026-10-30", "side": "buy", "premium": 1_200_000,
            "delta_vol": 2000, "open_interest": 800, "d_oi": 500,
            "otm_pct": 4.2, "conviction": 90, "sweep": True, "delta": 0.42,
        },
        # Put OTM comprada mientras domina el flujo alcista -> clasificada como HEDGE (cobertura)
        {
            "contract": "MU261030P00900000", "call_put": "P", "strike": 900.0,
            "expiry": "2026-10-30", "side": "buy", "premium": 200_000,
            "delta_vol": 400, "open_interest": 300, "d_oi": 100,
            "otm_pct": 6.0, "conviction": 72, "sweep": False, "delta": -0.18,
        },
        # Cierre de posición (d_oi < 0) -> filtrado por la IA (ai_keep = False)
        {
            "contract": "MU261030C00950000", "call_put": "C", "strike": 950.0,
            "expiry": "2026-10-30", "side": "sell", "premium": 300_000,
            "delta_vol": 500, "open_interest": 1000, "d_oi": -400,
            "otm_pct": -0.5, "conviction": 55, "sweep": False, "delta": 0.55,
        },
    ]
    classify_prints_ai(prints, spot=960.0, spot_change_pct=1.5)
    assert prints[0]["ai_intent"] == "WHALE_BULL" and prints[0]["ai_grade"] == "WHALE"
    assert prints[0]["ai_keep"] is True
    assert prints[1]["ai_intent"] == "HEDGE" and prints[1]["ai_weight"] == 0.25
    assert prints[1]["ai_keep"] is True
    assert prints[2]["ai_intent"] == "CLOSING" and prints[2]["ai_keep"] is False


# --- Lattice GEX real y Mapa de Posicionamiento ---
def test_levels_gex_and_positioning_map():
    rows = [
        {**row("C1", "C", 1000.0, oi=8000, expiry="2026-10-30"), "gamma": 0.015, "spot": 960.0},
        {**row("P1", "P", 940.0, oi=4000, expiry="2026-10-30"), "gamma": 0.010, "spot": 960.0},
    ]
    lv = levels(rows)
    assert lv["net_gex"] > 0
    assert lv["gamma_regime"] == "POSITIVE"
    assert lv["king_node"]["strike"] == 1000.0
    assert lv["gamma_flip"] is not None

    prints = [
        {
            "contract": "C1", "call_put": "C", "strike": 1000.0, "expiry": "2026-10-30",
            "side": "buy", "premium": 1_500_000, "delta_vol": 1500, "open_interest": 8000,
            "d_oi": 500, "otm_pct": 4.2, "conviction": 88, "sweep": True,
        }
    ]
    classify_prints_ai(prints, spot=960.0)
    v = verdict(prints, streak=3)
    v["invalidation"] = invalidation(v["direction"], lv, 960.0)
    pos = positioning_map(prints, lv, v, 960.0)
    assert pos["direction"] == "ALCISTA"
    assert pos["target_strike"] == 1000.0
    assert pos["support_strike"] == 940.0
    assert "$1000" in pos["thesis_es"]
    assert any(n["kind"] == "target" for n in pos["ladder"])


def test_clean_ticker_and_cboe_url():
    assert clean_ticker(" nvda ") == "NVDA"
    assert clean_ticker("") == "MU"
    assert "NVDA.json" in cboe_url_for("NVDA")


def test_invalidation_accepts_json_string_keys():
    # Cuando state.json se lee de disco, las claves de call_oi y put_oi son strings.
    lv = {
        "call_wall": {"strike": 1010.0, "oi": 4000},
        "put_wall": {"strike": 990.0, "oi": 3000},
        "call_oi": {"1005.0": 500, "1010.0": 4000},
        "put_oi": {"990.0": 3000, "995.0": 800},
    }
    inv = invalidation("ALCISTA", lv, 1000.0)
    assert inv["price"] == 995.0 and inv["arrow"] == "down"


