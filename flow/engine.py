"""Motor: detección explicable + Filtro IA híbrido de posicionamiento institucional.

Inspirado en la arquitectura de BladeMap AI (Pulse + Vector + Lattice):
1. PULSE (Filtro IA de Microestructura e Intención):
   - Inferencia de agresión mejorada: combina la posición en la horquilla
     (bid/ask) con la desviación frente al valor teórico de Cboe (theo).
   - Clasificador de Intención Institucional por print:
       🐋 WHALE_BULL / WHALE_BEAR   -> Apertura institucional agresiva de alta convicción
       🎯 DIRECTIONAL_BULL / BEAR   -> Posicionamiento direccional limpio (compra C/P)
       💰 INCOME_BULL / BEAR        -> Venta de prima institucional (suelo put / techo call)
       🛡️ HEDGE                     -> Cobertura de cartera (compra de puts OTM en sesión alcista)
       🔄 CLOSING                   -> Cierre o rotación de posición previa (ΔOI < 0)
       ⚪ UNCLASSIFIED              -> Flujo mixto / sin sesgo claro (filtrado por defecto)
   - Detección de estructuras multi-pata: Call/Put Spreads, Risk Reversals y Straddles.

2. LATTICE (Exposición Gamma Real de Dealers — GEX y Niveles Clave):
   - Calcula el GEX neto por strike usando la gamma real de Cboe:
     GEX_call = +OI × gamma × 100 × spot² × 0.01
     GEX_put  = -OI × gamma × 100 × spot² × 0.01
   - Identifica el King Node (mayor imán gamma), Gamma Flip (cambio de régimen)
     y si el mercado está en Gamma Positiva (amortiguado/rango) o Negativa
     (acelerado/tendencial).

3. VECTOR & MAPA DE POSICIONAMIENTO ("Hacia dónde se están posicionando"):
   - Sintetiza en una vista directa hacia qué strike exacto apuntan las
     instituciones (Imán Institucional), dónde defienden (Soporte/Suelo),
     dónde se invalida la tesis y si hoy toca operar o mantenerse al margen
     (Stand Down).
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime
from typing import Any
from urllib.request import Request, urlopen

# --- Umbrales: simples, documentados, en un solo sitio. ---
MIN_PREMIUM_USD = 50_000      # prima mínima para que un print cuente
MIN_DELTA_VOL = 50            # contratos mínimos entre scans
MIN_VOL_OI_RATIO = 0.5        # Δvolumen frente a OI
VERDICT_THRESHOLD_USD = 500_000  # neto ponderado para ALCISTA / BAJISTA
MAX_PRINTS = 25               # prints nuevos por scan (antes de acumular)
MAX_DAY_PRINTS = 200          # contratos distintos acumulados en el día
SWEEP_MIN_PRINTS = 3          # prints para que haya barrido
SWEEP_MAX_SPREAD_PCT = 0.10   # rango de strikes del barrido, sobre el spot

# --- Pesos de convicción (ver docstring). ---
CONV_BASE = 20
CONV_PREMIUM_CAP = 30
CONV_SWEEP_BONUS = 15
CONV_NEW_POSITION = 15
CONV_CLOSING_PENALTY = -10
CONV_CLEAR_SIDE = 10


def side_bias(bid: float | None, ask: float | None, last: float | None) -> str:
    """Sesgo PROBABILÍSTICO desde la quote delayed: 'buy', 'sell' o 'unknown'.

    Posición del último precio dentro de la horquilla: pegado al ask (>= 75%)
    -> COMPRA, pegado al bid (<= 25%) -> VENTA, en medio -> desconocido.
    """
    if last is None or bid is None or ask is None or ask <= bid or bid <= 0:
        return "unknown"
    pos = (last - bid) / (ask - bid)
    if pos >= 0.75:
        return "buy"
    if pos <= 0.25:
        return "sell"
    return "unknown"


def infer_side(
    bid: float | None,
    ask: float | None,
    last: float | None,
    theo: float | None = None,
) -> str:
    """Inferencia híbrida de agresión: horquilla bid/ask + precio teórico Cboe (theo).

    Cuando entra un bloque grande, el market maker mueve la horquilla y el
    último precio queda a veces en el 55–74% de la nueva horquilla, pero
    claramente por encima del valor teórico (theo) del modelo de Cboe.
    """
    direct = side_bias(bid, ask, last)
    if direct != "unknown":
        return direct
    if (
        last is None
        or bid is None
        or ask is None
        or theo is None
        or ask <= bid
        or bid <= 0
        or theo <= 0
    ):
        return "unknown"
    spread = ask - bid
    pos = (last - bid) / spread
    # Si pagaron claramente por encima del valor teórico y en la mitad superior del spread
    if last >= theo + 0.15 * spread and pos >= 0.55:
        return "buy"
    # Si vendieron claramente por debajo del valor teórico y en la mitad inferior
    if last <= theo - 0.15 * spread and pos <= 0.45:
        return "sell"
    return "unknown"


def is_unusual(delta_vol: int, volume: int, open_interest: int, premium: float) -> bool:
    """Reglas 1 y 2 juntas: prima grande y volumen inusual frente al OI."""
    if premium < MIN_PREMIUM_USD or delta_vol < MIN_DELTA_VOL:
        return False
    if open_interest <= 0:
        return True
    return delta_vol >= MIN_VOL_OI_RATIO * open_interest or volume >= open_interest


def otm_pct(call_put: str, strike: float, spot: float | None) -> float | None:
    """Distancia OTM en %: positiva = fuera de dinero, negativa = dentro.

    Calls: (strike − spot) / spot; puts: (spot − strike) / spot.
    """
    if not spot or spot <= 0:
        return None
    if call_put == "C":
        return round((strike - spot) / spot * 100, 1)
    return round((spot - strike) / spot * 100, 1)


def conviction(
    premium: float,
    vol_oi_ratio: float,
    d_oi: int | None,
    side: str,
    sweep: bool,
) -> int:
    """Score 0–100 con los pesos documentados arriba. Sin modelo."""
    score = CONV_BASE
    if premium >= MIN_PREMIUM_USD:
        score += min(CONV_PREMIUM_CAP, 10 * math.log10(premium / MIN_PREMIUM_USD))
    if vol_oi_ratio >= 2:
        score += 20
    elif vol_oi_ratio >= 1:
        score += 15
    elif vol_oi_ratio >= 0.5:
        score += 10
    if d_oi is not None:
        if d_oi > 0:
            score += CONV_NEW_POSITION
        elif d_oi < 0:
            score += CONV_CLOSING_PENALTY
    if side in ("buy", "sell"):
        score += CONV_CLEAR_SIDE
    if sweep:
        score += CONV_SWEEP_BONUS
    return max(0, min(100, round(score)))


# --- Badges estilo BladeMap Pulse (umbrales del teardown, NO los del
# clasificador IA propio: ai_grade usa convicción ≥82/70/55 y prima
# ≥$250K; estos usan solo prima + distancia OTM + etiqueta de
# convicción, para que la comparativa sea legible en la misma fila). ---
BM_WHALE_PREMIUM = 1_000_000  # Whale = tamaño, sin condición OTM
BM_GOLDEN_PREMIUM = 900_000   # Golden = calidad: prima alta y 2–30% OTM
BM_GOLDEN_OTM_MIN = 2.0
BM_GOLDEN_OTM_MAX = 30.0
BM_SILVER_PREMIUM = 300_000   # Silver = prima + etiqueta de convicción


def blademap_badge(
    premium: float,
    otm: float | None,
    has_conviction_tag: bool,
) -> str | None:
    """Sello BladeMap de un print: WHALE / GOLDEN / SILVER / None.

    Orden BladeMap: primero tamaño (Whale ≥ $1M), luego calidad
    (Golden ≥ $900K y 2–30% OTM), luego Silver (≥ $300K + etiqueta
    de convicción: barrido o volumen > OI). Sin esas condiciones no
    hay sello, aunque el print pase el filtro de prima mínima.
    """
    if premium >= BM_WHALE_PREMIUM:
        return "WHALE"
    if (
        premium >= BM_GOLDEN_PREMIUM
        and otm is not None
        and BM_GOLDEN_OTM_MIN <= otm <= BM_GOLDEN_OTM_MAX
    ):
        return "GOLDEN"
    if premium >= BM_SILVER_PREMIUM and has_conviction_tag:
        return "SILVER"
    return None


def open_close_from_doi(d_oi: int | None) -> str:
    """Lectura Apertura/Cierre desde ΔOI (cadena): subir = apertura,
    bajar = cierre, sin dato = desconocido. En modo OPRA la lectura
    viene de trade_direction (ver opra.trade_to_print) y no se toca."""
    if d_oi is None:
        return "UNKNOWN"
    if d_oi > 0:
        return "OPEN"
    if d_oi < 0:
        return "CLOSE"
    return "UNKNOWN"


def _estimate_abs_delta(call_put: str, otm: float | None, raw_delta: float | None) -> float:
    """Usa el delta real de Cboe si existe; si no, lo aproxima desde % OTM."""
    if raw_delta is not None and 0 < abs(raw_delta) <= 1.0:
        return abs(raw_delta)
    if otm is None:
        return 0.50
    # Aproximación logística suave alrededor de ATM (otm=0 -> 0.50)
    return max(0.05, min(0.95, round(1.0 / (1.0 + math.exp(otm / 3.5)), 2)))


def classify_prints_ai(
    prints: list[dict[str, Any]],
    spot: float | None = None,
    spot_change_pct: float | None = None,
) -> list[dict[str, Any]]:
    """Clasificador IA de intención institucional (inspirado en BladeMap Pulse).

    Evalúa cada print en el contexto global de la sesión (compras de calls vs puts,
    variación del spot, delta, d_oi, barridos y convicción) para etiquetar la
    intención real de la institución y decidir si pasa el Filtro IA (`ai_keep`).
    """
    call_buy_prem = sum(
        p.get("premium", 0)
        for p in prints
        if p.get("call_put") == "C" and p.get("side") == "buy"
    )
    put_buy_prem = sum(
        p.get("premium", 0)
        for p in prints
        if p.get("call_put") == "P" and p.get("side") == "buy"
    )
    dominant_bull_tape = call_buy_prem > 1.35 * max(1.0, put_buy_prem) or (
        spot_change_pct is not None and spot_change_pct >= 0.75 and call_buy_prem >= put_buy_prem
    )

    for p in prints:
        cp = p.get("call_put", "C")
        side = p.get("side", "unknown")
        prem = float(p.get("premium") or 0)
        conv = int(p.get("conviction") or 0)
        d_oi = p.get("d_oi")
        otm = p.get("otm_pct")
        sweep = bool(p.get("sweep"))
        delta_vol = int(p.get("delta_vol") or 0)
        oi = int(p.get("open_interest") or 0)
        abs_delta = _estimate_abs_delta(cp, otm, p.get("delta"))

        # Sello BladeMap (prima + OTM + etiqueta de convicción). La
        # etiqueta aquí es barrido o volumen > OI; la pata "5x sobre
        # la norma" no se puede calcular desde el snapshot (no hay
        # media de volumen por contrato), así que no se finge.
        has_tag = sweep or (oi > 0 and delta_vol >= oi)
        p["bm_badge"] = blademap_badge(prem, otm, has_tag)

        # Exposición equivalente en dólares de subyacente (Delta-Adjusted Flow)
        if spot and spot > 0:
            delta_usd = round(delta_vol * 100 * abs_delta * spot)
        else:
            delta_usd = round(prem * abs_delta)
        p["delta_usd"] = delta_usd

        # 1. ¿Es cierre o rotación de posición? (ΔOI negativo significativo)
        is_closing = d_oi is not None and d_oi < 0
        if is_closing:
            p["ai_intent"] = "CLOSING"
            p["ai_label"] = "🔄 CIERRE / ROTACIÓN"
            p["ai_grade"] = "NOISE"
            p["ai_direction"] = "NEUTRAL"
            p["ai_keep"] = False
            p["ai_weight"] = 0.15
            p["ai_reason"] = f"OI cayendo ({d_oi:+,}): toma de beneficios o cierre, no apertura."
            continue

        # 2. ¿Sin sesgo compra/venta?
        if side == "unknown":
            if prem >= 1_000_000 and (oi == 0 or delta_vol >= oi):
                p["ai_intent"] = "BLOCK_NEUTRAL"
                p["ai_label"] = "📦 BLOQUE MIXTO"
                p["ai_grade"] = "SILVER"
                p["ai_direction"] = "NEUTRAL"
                p["ai_keep"] = False
                p["ai_weight"] = 0.0
                p["ai_reason"] = "Bloque institucional ejecutado a mitad de horquilla (posible cruce o spread)."
            else:
                p["ai_intent"] = "UNCLASSIFIED"
                p["ai_label"] = "⚪ SIN SESGO"
                p["ai_grade"] = "NOISE"
                p["ai_direction"] = "NEUTRAL"
                p["ai_keep"] = False
                p["ai_weight"] = 0.0
                p["ai_reason"] = "Ejecución en mitad del bid/ask sin agresión compradora ni vendedora."
            continue

        # 3. ¿Es cobertura institucional (Hedge)?
        # Compra de Puts bastante OTM (otm >= 3.5% o |delta| <= 0.28) cuando el flujo general es alcista
        is_far_otm_put = cp == "P" and side == "buy" and (
            (otm is not None and otm >= 3.5) or abs_delta <= 0.28
        )
        if is_far_otm_put and dominant_bull_tape and not sweep:
            p["ai_intent"] = "HEDGE"
            p["ai_label"] = "🛡️ COBERTURA PUT"
            p["ai_grade"] = "GOLDEN" if conv >= 75 else "SILVER"
            p["ai_direction"] = "HEDGE"
            p["ai_keep"] = conv >= 55
            p["ai_weight"] = 0.25
            p["ai_reason"] = (
                "Compra de Put fuera de dinero mientras domina el flujo alcista: "
                "protección de cartera (hedge), no apuesta bajista pura."
            )
            continue

        # 4. Flujo con dirección clara (Alcista vs Bajista)
        is_bull = (cp == "C" and side == "buy") or (cp == "P" and side == "sell")
        ai_dir = "ALCISTA" if is_bull else "BAJISTA"

        # Grado estilo BladeMap Pulse (WHALE / GOLDEN / SILVER / NOISE)
        if conv >= 82 and prem >= 250_000:
            grade = "WHALE"
        elif conv >= 70 or (conv >= 65 and sweep):
            grade = "GOLDEN"
        elif conv >= 55:
            grade = "SILVER"
        else:
            grade = "NOISE"

        if side == "buy":
            if grade == "WHALE":
                intent = "WHALE_BULL" if is_bull else "WHALE_BEAR"
                label = "🐋 BALLENA CALL" if is_bull else "🐋 BALLENA PUT"
                weight = 1.25
                reason = (
                    f"Apertura agresiva institucional ({'barrido' if sweep else 'bloque'}) "
                    f"al ask buscando movimiento hacia ${p['strike']:g}."
                )
            else:
                intent = "DIRECTIONAL_BULL" if is_bull else "DIRECTIONAL_BEAR"
                label = "🎯 APERTURA CALL" if is_bull else "🔻 APERTURA PUT"
                weight = 1.0
                reason = f"Compra direccional de {'calls' if cp == 'C' else 'puts'} pegada al ask."
        else:
            # Venta de prima (Income / Suelo o Techo institucional)
            intent = "INCOME_BULL" if is_bull else "INCOME_BEAR"
            label = "💰 VENTA PUT (SUELO)" if is_bull else "🧱 VENTA CALL (TECHO)"
            weight = 0.85
            reason = (
                f"Venta institucional de {'puts fijando suelo en' if is_bull else 'calls fijando techo en'} "
                f"${p['strike']:g} (cobro de prima)."
            )

        p["ai_intent"] = intent
        p["ai_label"] = label
        p["ai_grade"] = grade
        p["ai_direction"] = ai_dir
        p["ai_keep"] = grade != "NOISE"
        p["ai_weight"] = weight
        p["ai_reason"] = reason

    _detect_multi_leg_structures(prints)
    return prints


def _detect_multi_leg_structures(prints: list[dict[str, Any]]) -> None:
    """Detecta si un par de prints del mismo vencimiento forma un Spread o Risk Reversal."""
    by_exp: dict[str, list[dict[str, Any]]] = {}
    for p in prints:
        if p.get("side") in ("buy", "sell") and p.get("expiry"):
            by_exp.setdefault(p["expiry"], []).append(p)

    for exp, group in by_exp.items():
        if len(group) < 2:
            continue
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                vol_a, vol_b = a.get("delta_vol", 0), b.get("delta_vol", 0)
                if not vol_a or not vol_b:
                    continue
                ratio = min(vol_a, vol_b) / max(vol_a, vol_b)
                if ratio < 0.65:
                    continue
                # Vertical Spread: mismo C/P, distinto strike, lados opuestos (compra uno, vende otro)
                if (
                    a["call_put"] == b["call_put"]
                    and a["strike"] != b["strike"]
                    and a["side"] != b["side"]
                ):
                    kind = "CALL SPREAD" if a["call_put"] == "C" else "PUT SPREAD"
                    a.setdefault("ai_structure", f"{kind} {min(a['strike'], b['strike']):g}/{max(a['strike'], b['strike']):g}")
                    b.setdefault("ai_structure", a["ai_structure"])
                # Risk Reversal: Call y Put de distinto strike y lados opuestos
                elif (
                    a["call_put"] != b["call_put"]
                    and a["side"] != b["side"]
                ):
                    a.setdefault("ai_structure", f"RISK REVERSAL ({exp})")
                    b.setdefault("ai_structure", a["ai_structure"])


def _mark_sweeps(prints: list[dict[str, Any]], spot: float | None) -> None:
    """Marca sweep=True en los grupos de >= 3 prints (mismo sesgo, C/P, vto.)."""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for p in prints:
        if p["side"] == "unknown":
            continue
        groups.setdefault((p["side"], p["call_put"], p["expiry"]), []).append(p)
    for group in groups.values():
        if len(group) < SWEEP_MIN_PRINTS:
            continue
        strikes = [p["strike"] for p in group]
        if spot and spot > 0 and (max(strikes) - min(strikes)) / spot > SWEEP_MAX_SPREAD_PCT:
            continue
        for p in group:
            p["sweep"] = True


def detect_prints(
    rows: list[dict[str, Any]],
    prev_volumes: dict[str, int],
    prev_oi: dict[str, int] | None = None,
    spot: float | None = None,
) -> list[dict[str, Any]]:
    """Prints inusuales del scan, clasificados por IA y ordenados por prima descendente."""
    prev_oi = prev_oi or {}
    if spot is None and rows:
        spot = rows[0].get("spot")
    spot_change_pct = rows[0].get("spot_change_pct") if rows else None
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
        side = infer_side(r.get("bid"), r.get("ask"), r.get("last"), r.get("theo"))
        prev_oi_v = prev_oi.get(r["contract"])
        d_oi = r["open_interest"] - prev_oi_v if prev_oi_v is not None else None
        oi = r["open_interest"]
        prints.append(
            {
                "contract": r["contract"],
                "ticker": r.get("ticker", "MU"),
                "expiry": r["expiry"],
                "call_put": r["call_put"],
                "strike": r["strike"],
                "dte": r.get("dte"),
                "otm_pct": otm_pct(r["call_put"], r["strike"], spot),
                "side": side,  # buy / sell / unknown
                "delta_vol": delta_vol,
                "volume": r["volume"],
                "open_interest": oi,
                "d_oi": d_oi,
                "open_close": open_close_from_doi(d_oi),
                "is_new_position": bool(d_oi is not None and d_oi > 0),
                "premium": round(premium),
                "sweep": False,
                "delta": r.get("delta"),
                "gamma": r.get("gamma"),
                "iv": r.get("iv"),
                "theo": r.get("theo"),
            }
        )
    _mark_sweeps(prints, spot)
    for p in prints:
        oi = p["open_interest"]
        ratio = (p["delta_vol"] / oi) if oi > 0 else 1.0
        p["conviction"] = conviction(
            p["premium"], ratio, p["d_oi"], p["side"], p["sweep"]
        )
    classify_prints_ai(prints, spot=spot, spot_change_pct=spot_change_pct)
    prints.sort(key=lambda p: p["premium"], reverse=True)
    return prints[:MAX_PRINTS]


def merge_day_prints(
    day_prints: list[dict[str, Any]],
    new_prints: list[dict[str, Any]],
    now_iso: str,
) -> list[dict[str, Any]]:
    """Acumula los prints nuevos en los del día, por contrato."""
    by_contract = {p["contract"]: p for p in day_prints}
    for p in new_prints:
        old = by_contract.get(p["contract"])
        if old is None:
            p["times_seen"] = 1
            p["first_seen"] = now_iso
            p["last_seen"] = now_iso
            day_prints.append(p)
            by_contract[p["contract"]] = p
            continue
        old["times_seen"] = old.get("times_seen", 1) + 1
        old["last_seen"] = now_iso
        old["premium"] += p["premium"]
        old["delta_vol"] += p["delta_vol"]
        old["volume"] = p["volume"]
        old["open_interest"] = p["open_interest"]
        old["d_oi"] = p["d_oi"]
        old["open_close"] = open_close_from_doi(p["d_oi"])
        if old.get("side") == "unknown" and p.get("side") in ("buy", "sell"):
            old["side"] = p["side"]
        for k in ("delta", "gamma", "iv", "theo"):
            if p.get(k) is not None:
                old[k] = p[k]
        old["is_new_position"] = old.get("is_new_position") or p["is_new_position"]
        old["sweep"] = old.get("sweep") or p["sweep"]
        old["conviction"] = max(old.get("conviction", 0), p["conviction"])
    classify_prints_ai(day_prints)
    day_prints.sort(key=lambda p: p["premium"], reverse=True)
    return day_prints[:MAX_DAY_PRINTS]


def _signed(p: dict[str, Any]) -> float:
    """Signo del print para el veredicto. Sin sesgo -> 0 (no vota)."""
    side = p.get("side", "unknown")
    if side not in ("buy", "sell"):
        return 0.0
    bullish = (p.get("call_put") == "C" and side == "buy") or (
        p.get("call_put") == "P" and side == "sell"
    )
    if bullish:
        return 1.0
    return -1.0


def _signed_premium(p: dict[str, Any]) -> float:
    """Prima con signo (sin ponderar)."""
    return _signed(p) * float(p["premium"])


def _signed_weighted(p: dict[str, Any]) -> float:
    """Prima con signo, ponderada por convicción (prints viejos sin
    convicción cuentan al 100% para no romper su veredicto)."""
    return _signed(p) * float(p["premium"]) * (p.get("conviction", 100) / 100.0)


def _signed_ai_weighted(p: dict[str, Any]) -> float:
    """Prima ponderada por convicción Y por el multiplicador de intención IA."""
    w = float(p.get("ai_weight", 1.0))
    return _signed_weighted(p) * w


def stage(direction: str, confidence: int, n_voted: int, streak: int) -> tuple[str, str, int]:
    """Etapa: (clave, etiqueta, puntos del track de 3). Ver docstring."""
    if (
        direction != "NEUTRAL"
        and streak >= 5
        and confidence >= 60
        and n_voted >= 3
    ):
        return "CONFIRMED", "Confirmado", 3
    if direction != "NEUTRAL" and (streak >= 2 or n_voted >= 2):
        return "BUILDING", "Construyendo", 2
    return "EARLY", "Temprano", 1


def verdict(prints: list[dict[str, Any]], streak: int = 1) -> dict[str, Any]:
    """Veredicto ponderado por convicción + métricas del Filtro IA + etapa."""
    net_w = sum(_signed_weighted(p) for p in prints)
    net_u = sum(_signed_premium(p) for p in prints)
    net_ai = sum(_signed_ai_weighted(p) for p in prints)
    gross_w = sum(abs(_signed_weighted(p)) for p in prints)
    voted = [p for p in prints if p["side"] != "unknown"]
    ai_kept = [p for p in prints if p.get("ai_keep", p["side"] != "unknown")]

    if net_w >= VERDICT_THRESHOLD_USD:
        direction = "ALCISTA"
    elif net_w <= -VERDICT_THRESHOLD_USD:
        direction = "BAJISTA"
    else:
        direction = "NEUTRAL"

    agree = (abs(net_w) / gross_w) if gross_w > 0 else 0.0
    magnitude = min(1.0, abs(net_w) / 2_000_000)
    count = min(1.0, len(voted) / 5)
    confidence = round(50 * agree + 30 * magnitude + 20 * count)
    if direction == "NEUTRAL":
        confidence = round(confidence / 2)
    stage_key, stage_label, stage_dots = stage(
        direction, confidence, len(voted), streak
    )

    top = max(voted, key=lambda p: p["premium"]) if voted else (
        max(prints, key=lambda p: p["premium"]) if prints else None
    )
    if top is None:
        evidence = "Sin prints inusuales hoy."
    else:
        action = {"buy": "Compra", "sell": "Venta"}.get(top["side"], "Flujo")
        kind = "calls" if top["call_put"] == "C" else "puts"
        conv = f" (convicción {top.get('conviction', 100)})" if top.get("conviction") is not None else ""
        if top["side"] == "unknown":
            evidence = (
                f"Mayor print sin sesgo claro: {kind} strike {top['strike']:g} "
                f"vto. {top['expiry']} por ${top['premium']:,} de prima."
            )
        else:
            evidence = (
                f"{action} de {kind} strike {top['strike']:g} "
                f"vto. {top['expiry']} por ${top['premium']:,} de prima{conv}."
            )
    headline = {
        "ALCISTA": "Las instituciones están comprando",
        "BAJISTA": "Las instituciones están vendiendo",
        "NEUTRAL": "Sin dirección institucional clara",
    }[direction]
    return {
        "direction": direction,
        "headline": headline,
        "evidence": evidence,
        "net": round(net_w),
        "net_unweighted": round(net_u),
        "net_ai": round(net_ai),
        "confidence": confidence,
        "n_prints": len(prints),
        "n_voted": len(voted),
        "n_ai_kept": len(ai_kept),
        "n_ai_filtered": max(0, len(prints) - len(ai_kept)),
        "streak": streak,
        "stage": stage_key,
        "stage_label": stage_label,
        "stage_dots": stage_dots,
    }


# --- Vector ciclo de vida (estilo BladeMap: flip -> follow -> confirm) ---
# La etapa del veredicto (EARLY/BUILDING/CONFIRMED) ya existía y se
# basa en racha + confianza + nº de prints; el ciclo Vector es otra
# cosa: etapa por scans consecutivos en la misma dirección (1 = giro,
# 2 = construyendo, 3+ = confirmado) y frescura por minutos desde el
# giro. Ambos se muestran, con nombres distintos, para no mezclarlos.
VECTOR_FRESH_MIN = 15
VECTOR_ACTIVE_MIN = 45
VECTOR_AGING_MIN = 90

VECTOR_STAGE_LABELS = {
    "FLIP": "Giro (Flip)",
    "FOLLOW": "Construyendo (Follow)",
    "CONFIRM": "Confirmado (Confirm)",
    "NONE": "Sin dirección",
}

VECTOR_FRESHNESS_LABELS = {
    "FRESH": "Reciente",
    "ACTIVE": "Activo",
    "AGING": "Envejeciendo",
    "LATE": "Entrada tardía",
}


def _parse_iso(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso))
    except ValueError:
        return None


def vector_age_min(flipped_at: str | None, now_iso: str) -> float | None:
    """Minutos desde el giro (None si no hay marca temporal válida)."""
    a = _parse_iso(flipped_at)
    b = _parse_iso(now_iso)
    if a is None or b is None:
        return None
    return max(0.0, round((b - a).total_seconds() / 60.0, 1))


def vector_freshness(age_min: float | None) -> tuple[str | None, str | None]:
    """Tramo de frescura BladeMap por edad del giro."""
    if age_min is None:
        return None, None
    if age_min <= VECTOR_FRESH_MIN:
        return "FRESH", VECTOR_FRESHNESS_LABELS["FRESH"]
    if age_min <= VECTOR_ACTIVE_MIN:
        return "ACTIVE", VECTOR_FRESHNESS_LABELS["ACTIVE"]
    if age_min <= VECTOR_AGING_MIN:
        return "AGING", VECTOR_FRESHNESS_LABELS["AGING"]
    return "LATE", VECTOR_FRESHNESS_LABELS["LATE"]


def vector_update(prev: dict[str, Any] | None, direction: str, streak: int, now_iso: str) -> dict[str, Any]:
    """Ciclo Vector de un scan. El servidor pasa prev=None al cambiar
    de sesión para que la racha y las caídas no crucen días."""
    prev = prev or {}
    prev_dir = prev.get("direction")
    if direction == "NEUTRAL":
        stage, stage_label = "NONE", VECTOR_STAGE_LABELS["NONE"]
        flipped_at = now_iso
        drops = int(prev.get("stage_drops") or 0)
    else:
        # Cambio de dirección confirmado -> FLIP aunque la racha diga
        # otra cosa; sin previo conocido, manda la racha guardada
        # (un estado viejo con racha 3 ya viene confirmado).
        if prev_dir is not None and prev_dir != direction:
            stage = "FLIP"
        elif streak <= 1:
            stage = "FLIP"
        elif streak == 2:
            stage = "FOLLOW"
        else:
            stage = "CONFIRM"
        stage_label = VECTOR_STAGE_LABELS[stage]
        if prev_dir == direction and prev.get("flipped_at"):
            flipped_at = prev["flipped_at"]
        else:
            flipped_at = now_iso
        drops = int(prev.get("stage_drops") or 0)
        if prev_dir not in (None, "NEUTRAL", direction):
            drops += 1
    age = vector_age_min(flipped_at, now_iso) if direction != "NEUTRAL" else None
    fresh, fresh_label = vector_freshness(age)
    return {
        "direction": direction,
        "stage": stage,
        "stage_label": stage_label,
        "flipped_at": flipped_at,
        "age_min": age,
        "freshness": fresh,
        "freshness_label": fresh_label,
        "stage_drops": drops,
        "streak": streak,
    }


def vector_refresh(vector: dict[str, Any] | None, now_iso: str) -> dict[str, Any] | None:
    """Recalcula edad/frescura de un vector guardado sin cambiar su
    etapa (para estados viejos leídos sin scan nuevo)."""
    if not vector:
        return vector
    v = dict(vector)
    if v.get("direction") != "NEUTRAL" and v.get("flipped_at"):
        v["age_min"] = vector_age_min(v["flipped_at"], now_iso)
        fresh, fresh_label = vector_freshness(v["age_min"])
        v["freshness"] = fresh
        v["freshness_label"] = fresh_label
    return v


def gates(verdict_: dict[str, Any], delay_minutes: int | None,
          trend: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Cinco gates estilo BladeMap, honestos sobre lo que miden:
    - Frescura: la fuente declara su retraso (no se finge tiempo real).
    - Dirección: hay dirección no neutral.
    - Respaldo: al menos 2 prints votando o racha ≥ 2 scans.
    - Explicado: el veredicto trae evidencia distinta del texto vacío.
    - SMA horaria: pata Holy Grail — el spot debe estar del lado
      correcto de la SMA5 horaria de TradingView (alcista: por encima;
      bajista: por debajo). Sin tendencia, ok=None ("?" en la UI):
      bloquea el setup porque nada sin verificar confirma, pero no se
      presenta como un fallo.
    """
    out = []
    if delay_minutes is not None:
        out.append({"key": "fresh", "label": "Frescura", "ok": True,
                    "detail": f"Retraso declarado: {delay_minutes} min"})
    else:
        out.append({"key": "fresh", "label": "Frescura", "ok": False,
                    "detail": "Fuente sin retraso declarado"})
    direction = verdict_.get("direction", "NEUTRAL")
    out.append({"key": "direction", "label": "Dirección", "ok": direction != "NEUTRAL",
                "detail": direction})
    n_voted = int(verdict_.get("n_voted") or 0)
    streak = int(verdict_.get("streak") or 1)
    out.append({"key": "backing", "label": "Respaldo",
                "ok": n_voted >= 2 or streak >= 2,
                "detail": f"{n_voted} prints votando · racha {streak}"})
    ev = verdict_.get("evidence") or ""
    out.append({"key": "explained", "label": "Explicado",
                "ok": bool(ev) and ev != "Sin prints inusuales hoy.",
                "detail": ev[:80]})
    if trend and trend.get("position") in ("above", "below", "flat"):
        if direction == "ALCISTA":
            sma_ok: bool | None = trend["position"] == "above"
        elif direction == "BAJISTA":
            sma_ok = trend["position"] == "below"
        else:
            sma_ok = False
        pos_txt = {"above": "por encima", "below": "por debajo", "flat": "en"}[trend["position"]]
        detail = (f"Spot {pos_txt} SMA5 ${trend['sma5']:.2f} "
                  f"(TV 1h, barra {trend.get('last_bar_et') or '?'})")
        if trend.get("stale"):
            detail += " · dato obsoleto"
        out.append({"key": "sma", "label": "SMA horaria", "ok": sma_ok, "detail": detail})
    else:
        out.append({"key": "sma", "label": "SMA horaria", "ok": None,
                    "detail": (trend or {}).get("error")
                    or "Sin tendencia horaria (sidecar TV no disponible)"})
    return out


def setup_update(prev_setup: dict[str, Any] | None, direction: str, stage: str, gates_: list[dict[str, Any]]) -> dict[str, Any]:
    """Setup estilo Sentinel: misma clave dirección:etapa que el scan
    anterior suma al contador (plegado ×N) en vez de abrir otra
    tarjeta; clave distinta reinicia a 1. ready exige todos los
    gates en verde Y etapa Confirm — incluida la pata SMA horaria
    (un gate en "?" también bloquea: nada sin verificar confirma)."""
    key = f"{direction}:{stage}"
    prev = prev_setup or {}
    count = int(prev.get("count") or 0) + 1 if prev.get("key") == key else 1
    ready = bool(gates_) and all(g.get("ok") for g in gates_) and stage == "CONFIRM"
    return {"key": key, "count": count, "ready": ready,
            "label": "SETUP CONFIRMADO" if ready else "Setup en formación"}


def synthetic_oi(levels_: dict[str, Any] | None, prints: list[dict[str, Any]]) -> dict[str, Any] | None:
    """OI sintético estilo BladeMap: OI oficial de la cadena + flujo
    neto firmado de los prints del día por strike (compra suma,
    venta resta, sin sesgo no cuenta). Es un proxy, no OI oficial:
    solo mide cuánto empuja el flujo de hoy sobre cada muro."""
    if not levels_:
        return None

    def _as_float_map(official: dict) -> dict[float, int]:
        out: dict[float, int] = {}
        for k, v in (official or {}).items():
            try:
                out[float(k)] = int(v)
            except (TypeError, ValueError):
                continue
        return out

    syn_call = _as_float_map(levels_.get("call_oi") or {})
    syn_put = _as_float_map(levels_.get("put_oi") or {})
    for p in prints or []:
        side = p.get("side")
        if side not in ("buy", "sell"):
            continue
        dv = int(p.get("delta_vol") or 0)
        if dv <= 0:
            continue
        try:
            strike = float(p.get("strike") or 0)
        except (TypeError, ValueError):
            continue
        if not strike:
            continue
        signed = dv if side == "buy" else -dv
        if p.get("call_put") == "C":
            syn_call[strike] = syn_call.get(strike, 0) + signed
        elif p.get("call_put") == "P":
            syn_put[strike] = syn_put.get(strike, 0) + signed

    def _top(syn: dict[float, int], official: dict[float, int]) -> dict[str, Any] | None:
        if not syn:
            return None
        strike = max(syn.items(), key=lambda kv: kv[1])[0]
        return {"strike": strike, "oi": int(official.get(strike, 0)),
                "syn_oi": int(syn[strike])}

    return {
        "call": {f"{k:g}": v for k, v in syn_call.items()},
        "put": {f"{k:g}": v for k, v in syn_put.items()},
        "top_call": _top(syn_call, _as_float_map(levels_.get("call_oi") or {})),
        "top_put": _top(syn_put, _as_float_map(levels_.get("put_oi") or {})),
    }


def invalidation(
    direction: str, levels_: dict[str, Any] | None, spot: float | None
) -> dict[str, Any] | None:
    """Nivel opuesto MÁS CERCANO como invalidación (None si NEUTRAL/sin datos)."""
    if direction == "NEUTRAL" or not levels_ or not spot or spot <= 0:
        return None
    if direction == "ALCISTA":
        raw_map = levels_.get("put_oi") or {}
        by_strike = {float(k): int(v) for k, v in raw_map.items()}
        kind, arrow = "muro put", "down"
        near = [s for s in by_strike if s < spot]
        if near:
            strike = max(near)
            oi = by_strike[strike]
        else:
            wall = levels_["put_wall"]
            strike, oi = float(wall["strike"]), int(wall["oi"])
    else:
        raw_map = levels_.get("call_oi") or {}
        by_strike = {float(k): int(v) for k, v in raw_map.items()}
        kind, arrow = "muro call", "up"
        near = [s for s in by_strike if s > spot]
        if near:
            strike = min(near)
            oi = by_strike[strike]
        else:
            wall = levels_["call_wall"]
            strike, oi = float(wall["strike"]), int(wall["oi"])
    dist = (strike - spot) / spot * 100
    return {
        "price": strike,
        "arrow": arrow,
        "reason": f"{kind} OI {oi:,}",
        "distance_pct": round(dist, 1),
    }


def normalize_print(p: dict[str, Any], spot: float | None = None) -> dict[str, Any]:
    """Completa los campos nuevos en prints guardados por versiones viejas."""
    p.setdefault("d_oi", None)
    p.setdefault("is_new_position", False)
    p.setdefault("sweep", False)
    if p.get("open_close") is None:
        # OPRA trae su propia lectura (trade_direction); la cadena
        # usa ΔOI. No sobrescribir la que ya venga puesta.
        p["open_close"] = open_close_from_doi(p.get("d_oi"))
    p.setdefault("times_seen", 1)
    p.setdefault("first_seen", None)
    p.setdefault("last_seen", None)
    if p.get("conviction") is None:
        oi = p.get("open_interest") or 0
        ratio = (p.get("delta_vol", 0) / oi) if oi > 0 else 1.0
        p["conviction"] = conviction(
            p.get("premium", 0), ratio, p.get("d_oi"),
            p.get("side", "unknown"), bool(p.get("sweep")),
        )
    if p.get("ai_intent") is None:
        classify_prints_ai([p], spot=spot)
    return p


def levels(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Muro call, muro put, max pain + Lattice GEX real (Gamma Exposure de Dealers).

    Aprovecha la gamma por contrato devuelta por Cboe para calcular:
    - net_gex: exposición gamma neta de dealers en dólares (1% move)
    - gamma_regime: POSITIVE (amortigua/rango) o NEGATIVE (acelera/tendencial)
    - gamma_flip: strike donde el GEX cambia de signo cerca del spot
    - king_node: strike con mayor concentración absoluta de gamma/OI (imán)
    """
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

    # --- LATTICE: Dealer Gamma Exposure (GEX) real si Cboe trae gamma ---
    spot = rows[0].get("spot")
    gex_by_strike: dict[float, float] = {}
    abs_gex_by_strike: dict[float, float] = {}
    has_gamma = False
    for r in rows:
        g = r.get("gamma")
        oi = r.get("open_interest") or 0
        if g is None or g <= 0 or oi <= 0:
            continue
        has_gamma = True
        ref_spot = spot if (spot and spot > 0) else r["strike"]
        # GEX en $ por cada 1% de movimiento del subyacente
        contract_gex = oi * g * 100.0 * (ref_spot ** 2) * 0.01
        signed_gex = contract_gex if r["call_put"] == "C" else -contract_gex
        k = r["strike"]
        gex_by_strike[k] = gex_by_strike.get(k, 0.0) + signed_gex
        abs_gex_by_strike[k] = abs_gex_by_strike.get(k, 0.0) + contract_gex

    net_gex = round(sum(gex_by_strike.values())) if has_gamma else None
    king_node = None
    gamma_flip = None
    gamma_regime = "UNKNOWN"
    gamma_regime_label = "Sin griegas en fuente"

    if has_gamma and gex_by_strike:
        king_strike = max(abs_gex_by_strike.items(), key=lambda kv: kv[1])[0]
        king_node = {
            "strike": king_strike,
            "net_gex": round(gex_by_strike.get(king_strike, 0.0)),
        }
        if net_gex is not None:
            if net_gex > 0:
                gamma_regime = "POSITIVE"
                gamma_regime_label = "Gamma Positiva · Rebote en muros (estabilizador)"
            elif net_gex < 0:
                gamma_regime = "NEGATIVE"
                gamma_regime_label = "Gamma Negativa · Aceleración direccional"
            else:
                gamma_regime = "NEUTRAL"
                gamma_regime_label = "Gamma Neutral"

        # Buscar Gamma Flip: par de strikes adyacentes donde el GEX cambia de signo
        sorted_k = sorted(gex_by_strike.keys())
        flips: list[float] = []
        for a, b in zip(sorted_k, sorted_k[1:]):
            ga, gb = gex_by_strike[a], gex_by_strike[b]
            if (ga <= 0 < gb) or (ga >= 0 > gb):
                denom = abs(ga) + abs(gb)
                cross = a + (b - a) * (abs(ga) / denom) if denom > 0 else (a + b) / 2
                flips.append(round(cross, 1))
        if flips:
            if spot and spot > 0:
                gamma_flip = min(flips, key=lambda x: abs(x - spot))
            else:
                gamma_flip = flips[0]

    return {
        "expiry": weekly_expiry,
        "call_wall": {"strike": call_wall[0], "oi": call_wall[1]["call_oi"]},
        "put_wall": {"strike": put_wall[0], "oi": put_wall[1]["put_oi"]},
        "max_pain": {"strike": max_pain},
        "call_oi": {s: o["call_oi"] for s, o in by_strike.items() if o["call_oi"] > 0},
        "put_oi": {s: o["put_oi"] for s, o in by_strike.items() if o["put_oi"] > 0},
        "net_gex": net_gex,
        "gamma_regime": gamma_regime,
        "gamma_regime_label": gamma_regime_label,
        "gamma_flip": gamma_flip,
        "king_node": king_node,
    }


def book_stats(prints: list[dict[str, Any]]) -> dict[str, Any]:
    """Libro de la sesión desde los prints del día (flujo, no OI)."""
    calls = [p for p in prints if p["call_put"] == "C"]
    puts = [p for p in prints if p["call_put"] == "P"]

    def wavg(ps: list[dict[str, Any]]) -> float | None:
        tot = sum(p["premium"] for p in ps)
        if not tot:
            return None
        return sum(p["strike"] * p["premium"] for p in ps) / tot

    buckets: dict[str, dict[str, Any]] = {}
    for name, lo, hi in (("0-7", 0, 7), ("8-21", 8, 21), ("22-45", 22, 45)):
        in_bucket = [
            p for p in prints
            if p.get("dte") is not None and lo <= p["dte"] <= hi
        ]
        cp = sum(p["premium"] for p in in_bucket if p["call_put"] == "C")
        pp = sum(p["premium"] for p in in_bucket if p["call_put"] == "P")
        tot = cp + pp
        buckets[name] = {
            "total": tot,
            "call_premium": cp,
            "put_premium": pp,
            "call_share": (cp / tot) if tot else None,
        }
    new_ps = [p for p in prints if p.get("is_new_position")]
    call_premium = sum(p["premium"] for p in calls)
    put_premium = sum(p["premium"] for p in puts)
    net_delta_usd = sum(
        _signed(p) * float(p.get("delta_usd") or p.get("premium") or 0)
        for p in prints
        if p.get("ai_keep", True)
    )
    return {
        "net": call_premium - put_premium,
        "call_premium": call_premium,
        "put_premium": put_premium,
        "avg_strike": wavg(prints),
        "call_avg_strike": wavg(calls),
        "put_avg_strike": wavg(puts),
        "dte_buckets": buckets,
        "new_position_premium": sum(p["premium"] for p in new_ps),
        "new_position_count": len(new_ps),
        "net_delta_usd": round(net_delta_usd),
    }


def targets(
    prints: list[dict[str, Any]], levels_: dict[str, Any] | None
) -> dict[str, Any]:
    """Niveles que busca el flujo de hoy (no OI acumulado)."""
    def top_buy(call_put: str) -> dict[str, Any] | None:
        by_strike: dict[float, dict[str, Any]] = {}
        for p in prints:
            if p["call_put"] != call_put or p["side"] != "buy":
                continue
            slot = by_strike.setdefault(
                p["strike"], {"strike": p["strike"], "premium": 0, "n_prints": 0}
            )
            slot["premium"] += p["premium"]
            slot["n_prints"] += 1
        if not by_strike:
            return None
        return max(by_strike.values(), key=lambda s: s["premium"])

    hedge = None
    if levels_:
        hedge = {"strike": levels_["put_wall"]["strike"], "oi": levels_["put_wall"]["oi"]}
    return {"bull": top_buy("C"), "bear": top_buy("P"), "hedge": hedge}


def positioning_map(
    prints: list[dict[str, Any]],
    levels_: dict[str, Any] | None,
    verdict_: dict[str, Any],
    spot: float | None,
) -> dict[str, Any]:
    """ Sintetiza "Hacia dónde se están posicionando" al estilo BladeMap (Vector + Lattice).

    Calcula:
    - primary_target: El strike imán hacia donde converge el posicionamiento institucional
    - defense_floor / ceiling_wall: Suelo institucional y techo de resistencia
    - price_ladder: Nodos ordenados con posición porcentual (0-100%) para dibujar el mapa
    - thesis_es / subthesis_es / action_label: Explicación directa en español
    """
    direction = verdict_.get("direction", "NEUTRAL")
    ai_prints = [p for p in prints if p.get("ai_keep", p.get("side") != "unknown")]

    # Agrupar flujo direccional filtrado por IA por strike
    bull_strikes: dict[float, dict[str, Any]] = {}
    bear_strikes: dict[float, dict[str, Any]] = {}
    for p in ai_prints:
        s = float(p["strike"])
        w_prem = float(p["premium"]) * (p.get("conviction", 70) / 100.0) * float(p.get("ai_weight", 1.0))
        if p.get("ai_direction") == "ALCISTA" or _signed(p) > 0:
            slot = bull_strikes.setdefault(
                s, {"strike": s, "score": 0.0, "premium": 0, "n_prints": 0, "expiry": p.get("expiry")}
            )
            slot["score"] += w_prem
            slot["premium"] += p["premium"]
            slot["n_prints"] += 1
        elif p.get("ai_direction") == "BAJISTA" or _signed(p) < 0:
            slot = bear_strikes.setdefault(
                s, {"strike": s, "score": 0.0, "premium": 0, "n_prints": 0, "expiry": p.get("expiry")}
            )
            slot["score"] += w_prem
            slot["premium"] += p["premium"]
            slot["n_prints"] += 1

    top_bull = max(bull_strikes.values(), key=lambda x: x["score"]) if bull_strikes else None
    top_bear = max(bear_strikes.values(), key=lambda x: x["score"]) if bear_strikes else None

    if direction == "ALCISTA":
        primary = top_bull or top_bear
    elif direction == "BAJISTA":
        primary = top_bear or top_bull
    else:
        # En neutral, elegir el mayor entre bull/bear o el King Node
        if top_bull and top_bear:
            primary = top_bull if top_bull["score"] >= top_bear["score"] else top_bear
        else:
            primary = top_bull or top_bear

    target_strike = primary["strike"] if primary else (
        levels_["king_node"]["strike"] if (levels_ and levels_.get("king_node")) else (
            levels_["max_pain"]["strike"] if levels_ else None
        )
    )
    target_dist_pct = (
        round((target_strike - spot) / spot * 100, 1)
        if (target_strike and spot and spot > 0)
        else None
    )

    put_wall = levels_["put_wall"]["strike"] if levels_ else None
    call_wall = levels_["call_wall"]["strike"] if levels_ else None
    max_pain = levels_["max_pain"]["strike"] if levels_ else None
    king_strike = levels_["king_node"]["strike"] if (levels_ and levels_.get("king_node")) else None
    gamma_flip = levels_.get("gamma_flip") if levels_ else None
    inv = verdict_.get("invalidation")
    inv_strike = inv["price"] if inv else None

    # Construir escalera visual de precios (Price Ladder)
    raw_nodes: list[dict[str, Any]] = []
    if put_wall is not None:
        raw_nodes.append({"key": "support", "label": "Suelo Put", "price": put_wall, "kind": "support"})
    if spot is not None and spot > 0:
        raw_nodes.append({"key": "spot", "label": "Spot Actual", "price": round(spot, 2), "kind": "spot"})
    if target_strike is not None:
        raw_nodes.append({"key": "target", "label": "Objetivo IA", "price": target_strike, "kind": "target"})
    if call_wall is not None and call_wall != target_strike:
        raw_nodes.append({"key": "resistance", "label": "Muro Call", "price": call_wall, "kind": "resistance"})
    if gamma_flip is not None:
        raw_nodes.append({"key": "flip", "label": "Gamma Flip", "price": gamma_flip, "kind": "flip"})

    ladder: list[dict[str, Any]] = []
    if raw_nodes:
        prices = [n["price"] for n in raw_nodes]
        lo, hi = min(prices), max(prices)
        span = max(1.0, hi - lo)
        for n in sorted(raw_nodes, key=lambda x: x["price"]):
            pct = 50.0 if hi == lo else round(6.0 + 88.0 * ((n["price"] - lo) / span), 1)
            ladder.append({**n, "pct": pct})

    # Síntesis ejecutiva en español ("Hacia dónde se están posicionando")
    if direction == "ALCISTA" and target_strike:
        dist_txt = f" ({target_dist_pct:+.1f}%)" if target_dist_pct is not None else ""
        exp_txt = f" para el vto. {primary['expiry']}" if (primary and primary.get("expiry")) else ""
        thesis_es = f"Posicionamiento ALCISTA apuntando hacia ${target_strike:g}{dist_txt}{exp_txt}"
        subthesis_es = (
            f"El filtro IA detecta {len(ai_prints)} jugadas institucionales limpias "
            f"(descartando {verdict_.get('n_ai_filtered', 0)} prints de ruido/cierre). "
            f"Suelo defendido en ${put_wall:g}." if put_wall else
            f"Flujo institucional comprador concentrado en el strike ${target_strike:g}."
        )
        action_label = "LUZ VERDE ALCISTA · SEGUIR FLUJO"
    elif direction == "BAJISTA" and target_strike:
        dist_txt = f" ({target_dist_pct:+.1f}%)" if target_dist_pct is not None else ""
        exp_txt = f" para el vto. {primary['expiry']}" if (primary and primary.get("expiry")) else ""
        thesis_es = f"Posicionamiento BAJISTA presionando hacia ${target_strike:g}{dist_txt}{exp_txt}"
        subthesis_es = (
            f"Acumulación de puts y/o venta de calls con objetivo en ${target_strike:g}. "
            f"Techo de resistencia en ${call_wall:g}." if call_wall else
            f"Presión institucional vendedora hacia ${target_strike:g}."
        )
        action_label = "LUZ VERDE BAJISTA · PRESIÓN VENDEDORA"
    else:
        thesis_es = (
            f"Sin convicción direccional clara · Imán de equilibrio en ${target_strike:g}"
            if target_strike
            else "Modo Stand Down · Sin posicionamiento institucional claro hoy"
        )
        subthesis_es = (
            "Al estilo BladeMap: cuando el flujo institucional está equilibrado o es solo ruido retail, "
            "la mejor posición es no forzar operaciones."
        )
        action_label = "STAND DOWN · ESPERAR CONFIRMACIÓN"

    return {
        "direction": direction,
        "action_label": action_label,
        "thesis_es": thesis_es,
        "subthesis_es": subthesis_es,
        "target_strike": target_strike,
        "target_dist_pct": target_dist_pct,
        "target_expiry": primary.get("expiry") if primary else (levels_.get("expiry") if levels_ else None),
        "target_premium": primary.get("premium") if primary else 0,
        "target_prints": primary.get("n_prints") if primary else 0,
        "support_strike": put_wall,
        "resistance_strike": call_wall,
        "max_pain_strike": max_pain,
        "king_strike": king_strike,
        "gamma_flip": gamma_flip,
        "invalidation_strike": inv_strike,
        "ladder": ladder,
    }


def campaigns(prints: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    """Campañas de acumulación: contratos vistos >= 2 veces en el día."""
    cands = [p for p in prints if p.get("times_seen", 1) >= 2]
    cands.sort(key=lambda p: p["premium"], reverse=True)
    out: list[dict[str, Any]] = []
    for p in cands[:limit]:
        conv = p.get("conviction", 0)
        label = "FUERTE" if conv >= 70 else ("EN CURSO" if conv >= 40 else "TEMPRANO")
        out.append(
            {
                "contract": p["contract"],
                "call_put": p["call_put"],
                "strike": p["strike"],
                "expiry": p["expiry"],
                "n_prints": p.get("times_seen", 1),
                "total_premium": p["premium"],
                "first_seen": p.get("first_seen"),
                "last_seen": p.get("last_seen"),
                "conviction": conv,
                "label": label,
                "ai_label": p.get("ai_label"),
            }
        )
    return out


_LLM_CACHE: dict[str, str] = {}


def maybe_llm_synthesis(
    ticker: str,
    spot: float | None,
    verdict_: dict[str, Any],
    pos_map: dict[str, Any],
    levels_: dict[str, Any] | None,
    prints: list[dict[str, Any]],
) -> str | None:
    """Síntesis opcional con Gemini API si GEMINI_API_KEY está configurada en entorno/.env.

    Si no hay clave o falla la red, devuelve None sin bloquear ni romper nada
    (el motor usa automáticamente `pos_map['thesis_es']` y `pos_map['subthesis_es']`).
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key or not prints:
        return None
    top_contracts = "|".join(f"{p['contract']}:{p['premium']}" for p in prints[:5])
    cache_key = f"{ticker}:{verdict_.get('direction')}:{pos_map.get('target_strike')}:{top_contracts}"
    if cache_key in _LLM_CACHE:
        return _LLM_CACHE[cache_key]
    try:
        top_summary = [
            {
                "strike": p["strike"],
                "cp": p["call_put"],
                "expiry": p["expiry"],
                "side": p["side"],
                "premium": p["premium"],
                "intent": p.get("ai_label"),
                "d_oi": p.get("d_oi"),
            }
            for p in prints[:6]
            if p.get("ai_keep", True)
        ]
        prompt = (
            f"Actúa como analista institucional de opciones estilo BladeMap AI. "
            f"En máximo 2 frases directas en español, explica hacia dónde se están posicionando "
            f"las instituciones hoy en {ticker} (spot ${spot}) y qué nivel vigilar. "
            f"Veredicto: {verdict_.get('direction')}, Objetivo IA: ${pos_map.get('target_strike')}, "
            f"Soporte Put: ${pos_map.get('support_strike')}, Techo Call: ${pos_map.get('resistance_strike')}, "
            f"Régimen Gamma: {(levels_ or {}).get('gamma_regime_label')}. "
            f"Top flujo filtrado: {json.dumps(top_summary, ensure_ascii=False)}"
        )
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
        body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
        req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=4.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = (
            data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
            .strip()
        )
        if text:
            _LLM_CACHE[cache_key] = text
            return text
    except Exception:
        return None
    return None
