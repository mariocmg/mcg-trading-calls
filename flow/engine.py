"""Motor: 3 reglas transparentes, sin ML ni modelos. Todo MU.

REGLA 1 — Prima grande: un print solo cuenta si mueve dinero serio.
  prima = Δvolumen × precio × 100, y debe ser >= MIN_PREMIUM_USD (50.000 $).

REGLA 2 — Volumen inusual: el flujo debe ser grande frente al interés abierto.
  Δvolumen >= 50 contratos Y (Δvolumen >= 0,5 × OI o volumen del día >= OI).
  (Un contrato con OI 0 pasa el filtro de OI: no hay posición previa que superar.)

REGLA 3 — Sesgo compra/venta (PROBABILÍSTICO, no es tape real):
  posición del último precio dentro de la horquilla: >= 75% -> COMPRA,
  <= 25% -> VENTA; si no, "desconocido" y NO vota en el veredicto.
  Los datos son delayed de Cboe: esto es una inferencia, no una lectura
  de la cinta.

Δvolumen = volumen ahora − volumen del scan anterior (snapshot JSON).
El primer scan del día no tiene snapshot previo: Δvolumen = volumen del día
(lo que lleva negociado la sesión hasta ese momento).

VEREDICTO — suma de primas con sesgo:
  alcista: compras de calls + ventas de puts
  bajista: compras de puts + ventas de calls
  neto >= +500.000 $ -> ALCISTA; <= -500.000 $ -> BAJISTA; si no, NEUTRAL.
  Los prints sin sesgo claro se listan pero no votan.

NIVELES (vencimiento más cercano, el semanal):
  muro call = strike con mayor OI en calls (resistencia)
  muro put  = strike con mayor OI en puts (soporte)
  max pain  = strike que minimiza el valor intrínseco total del OI
              (calls: OI × max(0, P−K) + puts: OI × max(0, K−P))
"""

from __future__ import annotations

from typing import Any

# --- Umbrales: simples, documentados, en un solo sitio. ---
MIN_PREMIUM_USD = 50_000      # prima mínima para que un print cuente
MIN_DELTA_VOL = 50            # contratos mínimos entre scans
MIN_VOL_OI_RATIO = 0.5        # Δvolumen frente a OI
VERDICT_THRESHOLD_USD = 500_000  # neto necesario para ALCISTA / BAJISTA
MAX_PRINTS = 15               # filas que muestra la pantalla


def side_bias(bid: float | None, ask: float | None, last: float | None) -> str:
    """Sesgo PROBABILÍSTICO desde la quote delayed: 'buy', 'sell' o 'unknown'.

    Posición del último precio dentro de la horquilla: pegado al ask (>= 75%)
    -> COMPRA, pegado al bid (<= 25%) -> VENTA, en medio -> desconocido.
    (Un multiplicador sobre el ask, p. ej. last >= ask×0,98, falla con
    horquillas estrechas: un print a mitad de horquilla parecería compra.)
    """
    if last is None or bid is None or ask is None or ask <= bid or bid <= 0:
        return "unknown"
    pos = (last - bid) / (ask - bid)
    if pos >= 0.75:
        return "buy"
    if pos <= 0.25:
        return "sell"
    return "unknown"


def is_unusual(delta_vol: int, volume: int, open_interest: int, premium: float) -> bool:
    """Reglas 1 y 2 juntas: prima grande y volumen inusual frente al OI."""
    if premium < MIN_PREMIUM_USD or delta_vol < MIN_DELTA_VOL:
        return False
    if open_interest <= 0:
        return True
    return delta_vol >= MIN_VOL_OI_RATIO * open_interest or volume >= open_interest


def detect_prints(
    rows: list[dict[str, Any]], prev_volumes: dict[str, int]
) -> list[dict[str, Any]]:
    """Prints inusuales del scan, ordenados por prima descendente (máx. 15).

    prev_volumes: {contrato: volumen} del snapshot anterior. Si un contrato
    no está en el snapshot (primer scan del día), Δvolumen = volumen del día.
    """
    prints: list[dict[str, Any]] = []
    for r in rows:
        price = r.get("price")
        if price is None or price <= 0:
            continue
        prev = prev_volumes.get(r["contract"])
        delta_vol = r["volume"] - prev if prev is not None else r["volume"]
        if delta_vol <= 0:
            continue
        premium = delta_vol * price * 100
        if not is_unusual(delta_vol, r["volume"], r["open_interest"], premium):
            continue
        side = side_bias(r.get("bid"), r.get("ask"), r.get("last"))
        prints.append(
            {
                "contract": r["contract"],
                "expiry": r["expiry"],
                "call_put": r["call_put"],
                "strike": r["strike"],
                "side": side,  # buy / sell / unknown (probabilístico)
                "delta_vol": delta_vol,
                "volume": r["volume"],
                "open_interest": r["open_interest"],
                "premium": round(premium),
            }
        )
    prints.sort(key=lambda p: p["premium"], reverse=True)
    return prints[:MAX_PRINTS]


def _signed_premium(p: dict[str, Any]) -> float:
    """Signo del print para el veredicto. Sin sesgo -> 0 (no vota)."""
    if p["side"] == "unknown":
        return 0.0
    bullish = (p["call_put"] == "C" and p["side"] == "buy") or (
        p["call_put"] == "P" and p["side"] == "sell"
    )
    bearish = (p["call_put"] == "P" and p["side"] == "buy") or (
        p["call_put"] == "C" and p["side"] == "sell"
    )
    if bullish:
        return float(p["premium"])
    if bearish:
        return -float(p["premium"])
    return 0.0


def verdict(prints: list[dict[str, Any]]) -> dict[str, Any]:
    """Veredicto desde la suma de primas con sesgo + una frase de evidencia."""
    net = sum(_signed_premium(p) for p in prints)
    if net >= VERDICT_THRESHOLD_USD:
        direction = "ALCISTA"
    elif net <= -VERDICT_THRESHOLD_USD:
        direction = "BAJISTA"
    else:
        direction = "NEUTRAL"

    # Evidencia: el print con sesgo más grande; si no hay ninguno con sesgo,
    # el print mayor sin afirmar dirección.
    voted = [p for p in prints if p["side"] != "unknown"]
    top = max(voted, key=lambda p: p["premium"]) if voted else (
        max(prints, key=lambda p: p["premium"]) if prints else None
    )
    if top is None:
        evidence = "Sin prints inusuales hoy."
    else:
        action = {"buy": "Compra", "sell": "Venta"}.get(top["side"], "Flujo")
        kind = "calls" if top["call_put"] == "C" else "puts"
        if top["side"] == "unknown":
            evidence = (
                f"Mayor print sin sesgo claro: {kind} strike {top['strike']:g} "
                f"vto. {top['expiry']} por ${top['premium']:,} de prima."
            )
        else:
            evidence = (
                f"{action} de {kind} strike {top['strike']:g} "
                f"vto. {top['expiry']} por ${top['premium']:,} de prima."
            )
    headline = {
        "ALCISTA": "Las instituciones están comprando",
        "BAJISTA": "Las instituciones están vendiendo",
        "NEUTRAL": "Sin dirección institucional clara",
    }[direction]
    return {"direction": direction, "headline": headline, "evidence": evidence, "net": round(net)}


def levels(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Muro call, muro put y max pain del vencimiento más cercano. Nada más."""
    if not rows:
        return None
    weekly_expiry = min(r["expiry"] for r in rows)
    weekly = [r for r in rows if r["expiry"] == weekly_expiry]
    by_strike: dict[float, dict[str, int]] = {}
    for r in weekly:
        slot = by_strike.setdefault(r["strike"], {"call_oi": 0, "put_oi": 0})
        if r["call_put"] == "C":
            slot["call_oi"] += r["open_interest"]
        else:
            slot["put_oi"] += r["open_interest"]
    if not by_strike:
        return None
    call_wall = max(by_strike.items(), key=lambda kv: kv[1]["call_oi"])
    put_wall = max(by_strike.items(), key=lambda kv: kv[1]["put_oi"])

    # Max pain: el precio que minimiza el valor intrínseco total del OI.
    def pain(candidate: float) -> float:
        total = 0.0
        for strike, oi in by_strike.items():
            total += oi["call_oi"] * max(0.0, candidate - strike)
            total += oi["put_oi"] * max(0.0, strike - candidate)
        return total

    max_pain = min(by_strike.keys(), key=pain)
    return {
        "expiry": weekly_expiry,
        "call_wall": {"strike": call_wall[0], "oi": call_wall[1]["call_oi"]},
        "put_wall": {"strike": put_wall[0], "oi": put_wall[1]["put_oi"]},
        "max_pain": {"strike": max_pain},
    }
