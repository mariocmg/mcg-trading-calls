"""Motor: detección explicable de posicionamiento institucional. Todo MU.

Sin ML y sin black-box, a propósito: la versión anterior puntuaba con un
GradientBoosting cuyas etiquetas de outcome eran en parte sintéticas y cuyo
razonamiento no se podía auditar. Aquí cada punto de convicción tiene un
motivo visible en el código y en el modal "¿Cómo se calcula?".

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

ΔOI Y POSICIÓN NUEVA — el snapshot guarda también el OI previo por contrato.
  d_oi = OI ahora − OI del snapshot anterior. es_nueva_posicion = d_oi > 0
  (con el volumen ya filtrado por las reglas 1–2, eso es "volumen
  significativo"). Esto separa apertura institucional nueva de volumen de
  cierre o churn: mucho volumen con OI plano o cayendo NO es posicionarse,
  es deshacer o rotar. OJO: el OI intradía de las cadenas delayed suele ser
  el del cierre anterior, así que el ΔOI del primer scan del día mide el
  cambio de posición entre sesiones, y dentro del día casi siempre es 0.

BARRIDOS — en un mismo scan, >= 3 prints con el mismo sesgo (compra/venta),
  mismo call/put y mismo vencimiento, con strikes en un rango <= 10% del
  spot, cuentan como UN barrido, no como N prints sueltos. Flag BARRIDO y
  bonus de convicción. (Un barrido real se ejecuta en segundos y casi
  siempre cae dentro de un mismo scan de 30 s.)

CONVICCIÓN 0–100 por print (pesos fijos, visibles, sin modelo):
  base 20 por pasar los filtros
  + prima: 10 × log10(prima / 50.000), tope 30 (50k -> 0, 500k -> 10, 5M -> 20)
  + vol/OI: ratio >= 2 -> 20; >= 1 -> 15; >= 0,5 -> 10 (OI 0 cuenta como 1,0)
  + ΔOI: posición nueva -> 15; cierre neto (d_oi < 0) -> −10; sin dato / 0 -> 0
  + sesgo claro (compra/venta) -> 10; desconocido -> 0
  + barrido -> 15
  Total recortado a 0–100.

VEREDICTO PONDERADO — neto ponderado = suma de primas con sesgo, cada una
  multiplicada por su convicción/100. Umbral igual que antes: >= +500.000 $
  -> ALCISTA, <= −500.000 $ -> BAJISTA; si no, NEUTRAL. Al ponderar, el
  umbral es más exigente que con el neto sin ponderar: un veredicto necesita
  prima grande, convicción alta, o ambas. Los prints sin sesgo se listan
  pero no votan.

CONFIANZA DEL HERO (0–100) — 50 × acuerdo (neto ponderado / bruto ponderado)
  + 30 × magnitud (neto ponderado / 2M $, tope 1) + 20 × nº de prints con
  sesgo (tope 5). Si el veredicto es NEUTRAL, se divide entre 2: un NEUTRAL
  nunca presume de confianza.

ETAPA (3 fases) — según convicción + persistencia del veredicto entre scans
  (streak = scans consecutivos con la misma dirección; a 30 s/scan, 5 scans
  son ~2,5 min, así que "Confirmado" aquí significa persistencia corta,
  no una tesis de días):
  Confirmado: dirección + streak >= 5 + confianza >= 60 + >= 3 prints con sesgo
  Construyendo: dirección + (streak >= 2 o >= 2 prints con sesgo)
  Temprano: resto (incluye todo NEUTRAL).

NIVELES (vencimiento más cercano, el semanal):
  muro call = strike con mayor OI en calls (resistencia)
  muro put  = strike con mayor OI en puts (soporte)
  max pain  = strike que minimiza el valor intrínseco total del OI
              (calls: OI × max(0, P−K) + puts: OI × max(0, K−P))
  NO hay gamma flip ni régimen gamma: la cadena no trae IV ni griegas, y
  no se inventan.

INVALIDACIÓN — el muro OI opuesto: si ALCISTA, el muro put (perderlo
  invalida la tesis, flecha abajo); si BAJISTA, el muro call (flecha
  arriba); NEUTRAL no tiene invalidación.

PRINTS DEL DÍA — cada contrato acumula sus reapariciones en scans
  posteriores (merge_day_prints): prima y Δvolumen se suman, times_seen
  sube, first_seen se conserva y last_seen se actualiza. Así las campañas
  (contratos vistos >= 2 veces) son acumulación real, no alertas sueltas.
"""

from __future__ import annotations

import math
from typing import Any

# --- Umbrales: simples, documentados, en un solo sitio. ---
MIN_PREMIUM_USD = 50_000      # prima mínima para que un print cuente
MIN_DELTA_VOL = 50            # contratos mínimos entre scans
MIN_VOL_OI_RATIO = 0.5        # Δvolumen frente a OI
VERDICT_THRESHOLD_USD = 500_000  # neto ponderado para ALCISTA / BAJISTA
MAX_PRINTS = 15               # prints nuevos por scan (antes de acumular)
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


def _mark_sweeps(prints: list[dict[str, Any]], spot: float | None) -> None:
    """Marca sweep=True en los grupos de >= 3 prints (mismo sesgo, C/P, vto.).

    Los strikes del grupo deben caber en un rango <= 10% del spot (si no
    hay spot, no se exige rango). Los prints sin sesgo no forman barridos:
    sin dirección atribuible no hay barrido que contar.
    """
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
    """Prints inusuales del scan, ordenados por prima descendente (máx. 15).

    prev_volumes / prev_oi: {contrato: valor} del snapshot anterior. Si un
    contrato no está en el snapshot (primer scan del día), Δvolumen =
    volumen del día y d_oi = None (sin referencia previa).
    """
    prev_oi = prev_oi or {}
    if spot is None and rows:
        spot = rows[0].get("spot")
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
        prev_oi_v = prev_oi.get(r["contract"])
        d_oi = r["open_interest"] - prev_oi_v if prev_oi_v is not None else None
        oi = r["open_interest"]
        ratio = (delta_vol / oi) if oi > 0 else 1.0
        prints.append(
            {
                "contract": r["contract"],
                "expiry": r["expiry"],
                "call_put": r["call_put"],
                "strike": r["strike"],
                "dte": r.get("dte"),
                "otm_pct": otm_pct(r["call_put"], r["strike"], spot),
                "side": side,  # buy / sell / unknown (probabilístico)
                "delta_vol": delta_vol,
                "volume": r["volume"],
                "open_interest": oi,
                "d_oi": d_oi,
                "is_new_position": bool(d_oi is not None and d_oi > 0),
                "premium": round(premium),
                "sweep": False,
            }
        )
    _mark_sweeps(prints, spot)
    for p in prints:
        oi = p["open_interest"]
        ratio = (p["delta_vol"] / oi) if oi > 0 else 1.0
        p["conviction"] = conviction(
            p["premium"], ratio, p["d_oi"], p["side"], p["sweep"]
        )
    prints.sort(key=lambda p: p["premium"], reverse=True)
    return prints[:MAX_PRINTS]


def merge_day_prints(
    day_prints: list[dict[str, Any]],
    new_prints: list[dict[str, Any]],
    now_iso: str,
) -> list[dict[str, Any]]:
    """Acumula los prints nuevos en los del día, por contrato.

    Un contrato que reaparece suma prima y Δvolumen, sube times_seen y
    actualiza last_seen / volumen / OI / d_oi; conserva first_seen, el
    sesgo original y la mayor convicción vista. Los flags son OR: si alguna
    aparición fue barrido o posición nueva, el acumulado lo refleja.
    """
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
        old["is_new_position"] = old.get("is_new_position") or p["is_new_position"]
        old["sweep"] = old.get("sweep") or p["sweep"]
        old["conviction"] = max(old.get("conviction", 0), p["conviction"])
    day_prints.sort(key=lambda p: p["premium"], reverse=True)
    return day_prints[:MAX_DAY_PRINTS]


def _signed(p: dict[str, Any]) -> float:
    """Signo del print para el veredicto. Sin sesgo -> 0 (no vota)."""
    if p["side"] == "unknown":
        return 0.0
    bullish = (p["call_put"] == "C" and p["side"] == "buy") or (
        p["call_put"] == "P" and p["side"] == "sell"
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
    """Veredicto ponderado por convicción + confianza + etapa."""
    net_w = sum(_signed_weighted(p) for p in prints)
    net_u = sum(_signed_premium(p) for p in prints)
    gross_w = sum(abs(_signed_weighted(p)) for p in prints)
    voted = [p for p in prints if p["side"] != "unknown"]
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
        "confidence": confidence,
        "n_prints": len(prints),
        "n_voted": len(voted),
        "streak": streak,
        "stage": stage_key,
        "stage_label": stage_label,
        "stage_dots": stage_dots,
    }


def invalidation(
    direction: str, levels_: dict[str, Any] | None, spot: float | None
) -> dict[str, Any] | None:
    """Nivel opuesto MÁS CERCANO como invalidación (None si NEUTRAL/sin datos).

    BAJISTA: el strike call con OI más cercano POR ENCIMA del spot (perderlo
    al alza invalida); ALCISTA: el strike put con OI más cercano POR DEBAJO.
    Si no hay ningún nivel en ese lado (p. ej. niveles viejos sin mapa por
    strike), se usa el muro de mayor OI como respaldo.
    """
    if direction == "NEUTRAL" or not levels_ or not spot or spot <= 0:
        return None
    if direction == "ALCISTA":
        by_strike = levels_.get("put_oi") or {}
        kind, arrow = "muro put", "down"
        near = [s for s in by_strike if s < spot]
        if near:
            strike = max(near)
            oi = by_strike[strike]
        else:
            wall = levels_["put_wall"]
            strike, oi = wall["strike"], wall["oi"]
    else:
        by_strike = levels_.get("call_oi") or {}
        kind, arrow = "muro call", "up"
        near = [s for s in by_strike if s > spot]
        if near:
            strike = min(near)
            oi = by_strike[strike]
        else:
            wall = levels_["call_wall"]
            strike, oi = wall["strike"], wall["oi"]
    dist = (strike - spot) / spot * 100
    return {
        "price": strike,
        "arrow": arrow,
        "reason": f"{kind} OI {oi:,}",
        "distance_pct": round(dist, 1),
    }


def normalize_print(p: dict[str, Any]) -> dict[str, Any]:
    """Completa los campos nuevos en prints guardados por versiones viejas.

    Los prints de la versión simple (sin convicción, ΔOI ni flags) se
    actualizan: la convicción se recalcula desde sus campos guardados y
    el resto toma valores neutros. Así la tape no muestra huecos.
    """
    p.setdefault("d_oi", None)
    p.setdefault("is_new_position", False)
    p.setdefault("sweep", False)
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
    return p


def levels(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Muro call, muro put y max pain del vencimiento más cercano. Nada más.

    Además del muro (máximo OI), devuelve el mapa OI por strike de cada
    lado: la invalidación necesita el nivel opuesto MÁS CERCANO al spot,
    que no siempre es el de mayor OI.
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
    return {
        "expiry": weekly_expiry,
        "call_wall": {"strike": call_wall[0], "oi": call_wall[1]["call_oi"]},
        "put_wall": {"strike": put_wall[0], "oi": put_wall[1]["put_oi"]},
        "max_pain": {"strike": max_pain},
        "call_oi": {s: o["call_oi"] for s, o in by_strike.items() if o["call_oi"] > 0},
        "put_oi": {s: o["put_oi"] for s, o in by_strike.items() if o["put_oi"] > 0},
    }


def book_stats(prints: list[dict[str, Any]]) -> dict[str, Any]:
    """Libro de la sesión desde los prints del día (flujo, no OI).

    Neto = prima de calls − prima de puts. Precio medio = strike ponderado
    por prima. Buckets DTE con prima total y % de calls. "Posición nueva" =
    prima de prints con ΔOI > 0 (apertura, no cierre/churn).
    """
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
    }


def targets(
    prints: list[dict[str, Any]], levels_: dict[str, Any] | None
) -> dict[str, Any]:
    """Niveles que busca el flujo de hoy (no OI acumulado).

    🎯 strike con más prima de COMPRA de calls; 🔻 strike con más prima de
    COMPRA de puts; 🛡 muro put (soporte = nivel de cobertura).
    """
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


def campaigns(prints: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    """Campañas de acumulación: contratos vistos >= 2 veces en el día.

    Ordenados por prima total. La etiqueta sale de la convicción: >= 70
    FUERTE, >= 40 EN CURSO, resto TEMPRANO.
    """
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
            }
        )
    return out
