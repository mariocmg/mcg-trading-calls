"""Tests del motor: reglas de inusualidad, veredicto, niveles y parseo OCC.

Pocos y al grano: si estas reglas cambian, estos tests deben cambiar con ellas.
"""

from flow.engine import detect_prints, is_unusual, levels, side_bias, verdict
from flow.fetch import parse_occ


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
