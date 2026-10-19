"""
Vector Board signal for a selected underlying.

Combines:
1) Options-structure regime (hedge vs directional) from ml_positioning
2) Directional residual (long-OTM calls/puts, skew after hedge dampening)
3) Price confirmation via recent OHLC (trend + ATR invalidation)
4) Persistence across recent vs older alert windows

This is still a proxy (delayed chains), but deliberately not "puts>calls => bear".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np

from flow.market_data import fetch_ohlc
from flow.ml_positioning import analyze_alerts, extract_features, _safe, _prem

#: Confidence is clamped to [CONF_FLOOR, 96.5]. Be aware: with delayed chains
#: and a heuristic score, anything in the 52-65 ("LOW") band is essentially a
#: coin flip — size positions accordingly, don't treat it as an edge.
CONF_FLOOR = 52.0
CONF_CAP = 96.5


def _fetch_ohlc(symbol: str, period: str = "3mo") -> dict[str, Any]:
    # Moved to flow.market_data (shared cache); kept here for compatibility.
    return fetch_ohlc(symbol, period=period)


def _spot_from_alerts(alerts: list[dict[str, Any]]) -> float | None:
    spots = [_safe(a.get("spot")) for a in alerts if _safe(a.get("spot")) > 0]
    return float(np.median(spots)) if spots else None


def _persistence(alerts: list[dict[str, Any]], direction: str) -> tuple[bool, float]:
    """
    Compare older vs newer half of alerts by created_at / id.
    Returns (persists, agreement_score 0..1).
    """
    if len(alerts) < 8:
        return False, 0.0

    ordered = sorted(
        alerts,
        key=lambda a: (str(a.get("created_at") or ""), int(a.get("id") or 0)),
    )
    mid = len(ordered) // 2
    older, newer = ordered[:mid], ordered[mid:]

    def lean(rows: list[dict[str, Any]]) -> float:
        c = sum(_prem(a) for a in rows if a.get("call_put") == "C")
        p = sum(_prem(a) for a in rows if a.get("call_put") == "P")
        t = c + p
        if t <= 0:
            return 0.0
        # positive => call lean
        return (c - p) / t

    o, n = lean(older), lean(newer)
    if direction == "BULLISH":
        ok = n > 0.02 and n >= o - 0.05
        score = max(0.0, min(1.0, (n + 1) / 2))
    else:
        ok = n < -0.02 and n <= o + 0.05
        score = max(0.0, min(1.0, (-n + 1) / 2))
    return ok, float(score)


def _flow_alignment(feats: dict[str, float], direction: str) -> tuple[bool, float]:
    call_long = feats.get("call_long_otm_share", 0.0)
    put_long = feats.get("put_long_otm_share", 0.0)
    skew = feats.get("premium_skew", 0.0)
    call_otm_i = feats.get("call_otm_intensity", 0.0)
    put_otm_i = feats.get("put_otm_intensity", 0.0)

    if direction == "BULLISH":
        score = (
            0.45 * max(0.0, skew)
            + 0.35 * max(0.0, call_long - put_long)
            + 0.20 * max(0.0, (call_otm_i - put_otm_i) / 20.0)
        )
        return score >= 0.08, float(min(1.0, score * 2.5))
    score = (
        0.45 * max(0.0, -skew)
        + 0.35 * max(0.0, put_long - call_long)
        + 0.20 * max(0.0, (put_otm_i - call_otm_i) / 20.0)
    )
    return score >= 0.08, float(min(1.0, score * 2.5))


def _price_confirmation(ohlc: dict[str, Any], direction: str) -> tuple[bool, float]:
    if not ohlc.get("ok"):
        return False, 0.0
    z = float(ohlc.get("trend_z") or 0.0)
    r5 = float(ohlc.get("ret_5") or 0.0)
    if direction == "BULLISH":
        ok = z > 0.15 or r5 > 0.01
        score = max(0.0, min(1.0, (z / 2.0) * 0.7 + max(0.0, r5) * 15))
        return ok, float(score)
    ok = z < -0.15 or r5 < -0.01
    score = max(0.0, min(1.0, (-z / 2.0) * 0.7 + max(0.0, -r5) * 15))
    return ok, float(score)


def _invalidation(ohlc: dict[str, Any], spot: float, direction: str) -> dict[str, Any]:
    atr = float(ohlc.get("atr") or spot * 0.02)
    if direction == "BULLISH":
        swing = float(ohlc.get("swing_low") or spot - 1.5 * atr)
        level = max(swing, spot - 1.5 * atr)
        # never above spot for bull invalidation
        level = min(level, spot * 0.999)
        return {
            "price": round(level, 2),
            "arrow": "down",
            "method": "max(10d swing low, spot-1.5·ATR)",
            "atr": round(atr, 4),
            "distance_pct": round((level / spot - 1) * 100, 2),
        }
    swing = float(ohlc.get("swing_high") or spot + 1.5 * atr)
    level = min(swing, spot + 1.5 * atr)
    level = max(level, spot * 1.001)
    return {
        "price": round(level, 2),
        "arrow": "up",
        "method": "min(10d swing high, spot+1.5·ATR)",
        "atr": round(atr, 4),
        "distance_pct": round((level / spot - 1) * 100, 2),
    }


def _gex_check(
    levels: list[dict[str, Any]] | None, spot: float | None, direction: str
) -> tuple[bool | None, float, str]:
    """Does the gamma landscape support the direction?

    BULLISH wants put support below (put wall / +GEX); BEARISH wants call
    resistance above (call wall / −GEX). Returns (ok|None if no data, score, detail).
    """
    if not levels or spot is None or spot <= 0:
        return None, 0.0, "sin niveles"
    call_wall = None
    put_wall = None
    net_gex = 0.0
    for lv in levels:
        net_gex += float(lv.get("net_gex") or 0)
        if lv.get("call_oi") and (call_wall is None or lv["call_oi"] > call_wall["call_oi"]):
            call_wall = lv
        if lv.get("put_oi") and (put_wall is None or lv["put_oi"] > put_wall["put_oi"]):
            put_wall = lv
    if direction == "BULLISH":
        # support below spot
        candidates = [lv for lv in levels if lv["strike"] < spot and float(lv.get("put_oi") or 0) > 0]
        wall = max(candidates, key=lambda l: l["put_oi"], default=None)
        dist = (spot - wall["strike"]) / spot if wall else 1.0
        ok = bool(wall and dist <= 0.04) or net_gex > 0
        detail = (
            f"put wall {wall['strike']:g} ({dist:.1%} debajo)" if wall
            else ("GEX neta positiva" if net_gex > 0 else "sin soporte put cercano")
        )
        score = 1.0 - min(dist / 0.04, 1.0) if wall else (0.6 if net_gex > 0 else 0.2)
        return ok, float(max(0.0, min(1.0, score))), detail
    # BEARISH: resistance above spot
    candidates = [lv for lv in levels if lv["strike"] > spot and float(lv.get("call_oi") or 0) > 0]
    wall = max(candidates, key=lambda l: l["call_oi"], default=None)
    dist = (wall["strike"] - spot) / spot if wall else 1.0
    ok = bool(wall and dist <= 0.04) or net_gex < 0
    detail = (
        f"call wall {wall['strike']:g} ({dist:.1%} arriba)" if wall
        else ("GEX neta negativa" if net_gex < 0 else "sin resistencia call cercana")
    )
    score = 1.0 - min(dist / 0.04, 1.0) if wall else (0.6 if net_gex < 0 else 0.2)
    return ok, float(max(0.0, min(1.0, score))), detail


def _structure_check(
    scoped: list[dict[str, Any]], direction: str
) -> tuple[bool, float, str]:
    """Directional combos (risk reversals, spreads) and block prints."""
    from flow.combos import detect_combos

    combos = [c for c in detect_combos(scoped) if c.direction == direction]
    n_blocks = sum(
        1
        for a in scoped
        if a.get("is_block")
        and (
            (direction == "BULLISH" and a.get("call_put") == "C")
            or (direction == "BEARISH" and a.get("call_put") == "P")
        )
    )
    parts = []
    if combos:
        best = max(combos, key=lambda c: c.confidence)
        parts.append(f"{best.label_es} (conf {best.confidence:.0%})")
    if n_blocks:
        parts.append(f"{n_blocks} block{'s' if n_blocks > 1 else ''}")
    ok = bool(combos or n_blocks)
    score = min(1.0, 0.5 * len(combos) + 0.4 * min(n_blocks, 3) + (0.2 if ok else 0.0))
    return ok, float(score), "; ".join(parts) if parts else "sin combos ni bloques direccionales"


def _stage_from_checks(n_ok: int) -> tuple[str, int]:
    if n_ok >= 4:
        return "CONFIRMED", 3
    if n_ok >= 2:
        return "BUILDING", 2
    return "EARLY", 1


def _calibrated_confidence(
    evidence: float,
    direction: str,
    n_ok: int,
    conn=None,
) -> tuple[float, str]:
    """Map 0..1 evidence to 0..100 confidence, empirically calibrated when possible.

    If ≥20 resolved outcomes exist for this direction, blend the empirical
    hit-rate (Laplace-smoothed) with the evidence score. Otherwise use the
    evidence mapping alone (documented heuristic).
    """
    evidence_prob = 0.5 + 0.47 * max(0.0, min(1.0, evidence))
    note = "heurística (sin suficientes outcomes)"
    if conn is not None:
        try:
            from flow.db import calibration_lookup

            hits, n = calibration_lookup(conn, direction, n_ok)
            if n >= 20:
                emp = (hits + 2.0) / (n + 4.0)
                evidence_prob = 0.6 * emp + 0.4 * evidence_prob
                note = f"calibrada: hit-rate {emp:.0%} en {n} setups {direction} pasados"
        except Exception:
            pass
    conf = float(max(CONF_FLOOR, min(CONF_CAP, 100.0 * evidence_prob)))
    return conf, note


def _invalidation_v2(
    ohlc: dict[str, Any],
    spot: float,
    direction: str,
    levels: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Price invalidation: nearest opposing OI wall if close, else ATR.

    Plus a time horizon: the expiry holding the most directional OI.
    """
    atr = float(ohlc.get("atr") or spot * 0.02)
    wall = None
    wall_kind = ""
    if levels and spot > 0:
        if direction == "BULLISH":
            cands = [lv for lv in levels if lv["strike"] < spot and float(lv.get("put_oi") or 0) > 0]
            if cands:
                wall = max(cands, key=lambda l: l["put_oi"])
                wall_kind = "put_wall"
        else:
            cands = [lv for lv in levels if lv["strike"] > spot and float(lv.get("call_oi") or 0) > 0]
            if cands:
                wall = max(cands, key=lambda l: l["call_oi"])
                wall_kind = "call_wall"

    wall_close = False
    if wall:
        dist = abs(wall["strike"] - spot) / spot
        wall_close = dist <= 0.05

    if wall_close and wall:
        level = float(wall["strike"])
        method = f"{wall_kind}@{wall['strike']:g} (máx OI)"
        reason = (
            f"Soporte institucional en {wall['strike']:g}" if direction == "BULLISH"
            else f"Resistencia institucional en {wall['strike']:g}"
        )
        arrow = "down" if direction == "BULLISH" else "up"
    else:
        # ATR fallback (previous behavior)
        if direction == "BULLISH":
            swing = float(ohlc.get("swing_low") or spot - 1.5 * atr)
            level = min(max(swing, spot - 1.5 * atr), spot * 0.999)
            arrow = "down"
        else:
            swing = float(ohlc.get("swing_high") or spot + 1.5 * atr)
            level = max(min(swing, spot + 1.5 * atr), spot * 1.001)
            arrow = "up"
        method = "max(10d swing, spot∓1.5·ATR)"
        reason = "Sin muro cercano: swing 10d / ATR"

    # Time horizon: expiry with the most directional OI
    horizon = None
    horizon_reason = ""
    if levels:
        by_exp: dict[str, float] = {}
        for lv in levels:
            key = "put_oi" if direction == "BULLISH" else "call_oi"
            by_exp[lv["expiry"]] = by_exp.get(lv["expiry"], 0) + float(lv.get(key) or 0)
        if by_exp:
            best_exp = max(by_exp, key=lambda e: by_exp[e])
            horizon = best_exp
            horizon_reason = f"Vencimiento con más OI {'put' if direction == 'BULLISH' else 'call'}"

    return {
        "price": round(level, 2),
        "arrow": arrow,
        "method": method,
        "reason": reason,
        "atr": round(atr, 4),
        "distance_pct": round((level / spot - 1) * 100, 2),
        "time": horizon,
        "time_reason": horizon_reason,
    }


def _state_copy(
    stage: str,
    direction: str,
    flow_ok: bool,
    price_ok: bool,
    hedge: float,
    *,
    no_flow: bool = False,
) -> dict[str, str]:
    if no_flow or direction == "NEUTRAL":
        return {
            "title": "Sin señal",
            "detail": "Sin calls/puts institucionales RTH — la IA no ve tesis de desk",
            "badge": "NO FLOW",
            "badge_kind": "noisy",
            "direction": "NEUTRAL",
        }
    if stage == "CONFIRMED":
        title = "Confirmed"
        detail = "Clean continuation" if price_ok and flow_ok else "Structure holding"
        badge = "FLOW ALIGNED" if flow_ok else "PRICE LED"
        badge_kind = "aligned"
    elif stage == "BUILDING":
        title = "Building"
        detail = "Follow-through holding" if price_ok else "Flow building, price lagging"
        badge = "FLOW SUPPORTIVE" if flow_ok else "WATCH PRICE"
        badge_kind = "supportive"
    else:
        title = "Early"
        if hedge >= 0.45:
            detail = "Hedge-heavy tape — directional edge thin"
        else:
            detail = "Reversal risk elevated"
        badge = "FLOW NOISY" if not flow_ok else "FLOW NASCENT"
        badge_kind = "noisy"

    return {
        "title": title,
        "detail": detail,
        "badge": badge,
        "badge_kind": badge_kind,
        "direction": direction,
    }


@dataclass
class VectorResult:
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return self.data


def compute_vector(
    alerts: list[dict[str, Any]],
    underlying: str = "MU",
    conn=None,
    levels: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Vector Board signal.

    Evidence checklist (5 checks): flow alignment, price confirmation,
    persistence, gamma landscape, directional structure (combos/blocks).
    Stage = how many checks pass. Confidence = evidence mapped to 0-100,
    empirically calibrated against resolved outcomes when ≥20 exist.
    Invalidation = nearest opposing OI wall (else ATR) + time horizon.
    """
    und = underlying.upper()
    scoped = [a for a in alerts if str(a.get("underlying", "")).upper() == und]
    analysis = analyze_alerts(scoped, und)
    feats = extract_features(scoped)
    ohlc = _fetch_ohlc(und)

    spot = None
    if ohlc.get("ok"):
        spot = float(ohlc["spot"])
    if spot is None:
        spot = _spot_from_alerts(scoped)

    n_alerts = len(scoped)
    n_calls = sum(1 for a in scoped if a.get("call_put") == "C")
    n_puts = sum(1 for a in scoped if a.get("call_put") == "P")
    # Directional thesis needs options tape — price trend alone is not enough.
    no_flow = n_alerts == 0 or (n_calls == 0 and n_puts == 0)

    probs = analysis.probabilities
    hedge = float(probs.get("HEDGING", 0.0))
    bull = float(probs.get("BULLISH", 0.0))
    bear = float(probs.get("BEARISH", 0.0))
    mixed = float(probs.get("MIXED", 0.0))

    # Directional residual: ignore pure hedge mass
    struct = (bull - bear) + 0.35 * (
        feats.get("call_long_otm_share", 0.0) - feats.get("put_long_otm_share", 0.0)
    )
    struct += 0.25 * feats.get("premium_skew", 0.0)
    struct *= 1.0 - 0.55 * hedge

    trend_z = float(ohlc.get("trend_z") or 0.0) if ohlc.get("ok") else 0.0
    # Gamma regime conditions the thesis: long-gamma dampens trends
    # (mean-reversion, walls hold), short-gamma amplifies them.
    from flow.levels import gamma_regime

    regime = gamma_regime(levels or [])
    trend_w = float(regime.get("trend_weight", 1.0))
    dir_raw = struct + 0.35 * float(np.tanh(trend_z)) * trend_w

    if no_flow:
        direction = "NEUTRAL"
    elif abs(struct) < 0.03:
        direction = "NEUTRAL"
    elif abs(dir_raw) < 0.04 and mixed > 0.35:
        direction = "NEUTRAL"
    else:
        direction = "BULLISH" if dir_raw >= 0 else "BEARISH"

    checks: list[dict[str, Any]] = []
    if direction == "NEUTRAL":
        stage, stage_n = "EARLY", 1
        conf = 0.0 if no_flow else float(max(0.0, min(55.0, 28.0 + 10.0 * min(1.0, n_alerts / 20.0))))
        conf_label = "—" if no_flow else "LOW"
        conf_note = None
        degraded = True
        inv = {"price": None, "arrow": "", "method": "n/a — sin tesis direccional"}
        price_out = round(spot, 2) if spot else None
    else:
        flow_ok, flow_score = _flow_alignment(feats, direction)
        checks.append({"key": "flow", "label": "Alineación del flujo",
                       "ok": flow_ok, "score": round(flow_score, 3),
                       "detail": f"skew direccional + OTM largo ({flow_score:.0%})"})
        price_ok, price_score = _price_confirmation(ohlc, direction)
        checks.append({"key": "price", "label": "Confirmación de precio",
                       "ok": price_ok, "score": round(price_score, 3),
                       "detail": f"trend_z {trend_z:+.2f} ATR"})
        persist_ok, persist_score = _persistence(scoped, direction)
        checks.append({"key": "persistence", "label": "Persistencia",
                       "ok": persist_ok, "score": round(persist_score, 3),
                       "detail": "el lean se mantiene en la 2ª mitad de alertas"})
        gex_ok, gex_score, gex_detail = _gex_check(levels, spot, direction)
        if gex_ok is not None:
            checks.append({"key": "gex", "label": "Soporte gamma",
                           "ok": gex_ok, "score": round(gex_score, 3), "detail": gex_detail})
        struct_ok, struct_score, struct_detail = _structure_check(scoped, direction)
        checks.append({"key": "structure", "label": "Estructura (combos/bloques)",
                       "ok": struct_ok, "score": round(struct_score, 3), "detail": struct_detail})

        n_ok = sum(1 for c in checks if c["ok"])
        # Hedge overhang caps the stage: hedges shrink conviction
        if hedge >= 0.5 and abs(struct) < 0.18:
            n_ok = min(n_ok, 2)
        stage, stage_n = _stage_from_checks(n_ok)

        weights = {"flow": 0.25, "price": 0.25, "persistence": 0.20,
                   "gex": 0.15, "structure": 0.15}
        wsum = sum(weights[c["key"]] for c in checks) or 1.0
        evidence = sum(weights[c["key"]] * c["score"] for c in checks) / wsum
        # Hedge/mixed discount on the evidence itself
        evidence *= 1.0 - 0.35 * hedge - 0.15 * mixed
        evidence = max(0.0, min(1.0, evidence))

        conf, conf_note = _calibrated_confidence(evidence, direction, n_ok, conn=conn)
        conf_label = "HIGH" if conf >= 78 else ("MEDIUM" if conf >= 65 else "LOW")
        degraded = (not ohlc.get("ok")) or n_alerts < 12 or hedge >= 0.55

        if spot is None or spot <= 0:
            inv = {"price": None, "arrow": "", "method": "n/a"}
            price_out = None
        else:
            inv = _invalidation_v2(ohlc if ohlc.get("ok") else {"atr": spot * 0.02},
                                  spot, direction, levels)
            price_out = round(spot, 2)

    state = _state_copy(
        stage, direction,
        next((c["ok"] for c in checks if c["key"] == "flow"), False),
        next((c["ok"] for c in checks if c["key"] == "price"), False),
        hedge, no_flow=no_flow or direction == "NEUTRAL",
    )

    return {
        "symbol": und,
        "direction": direction,
        "stage": stage,
        "stage_fraction": f"{stage_n}/3",
        "stage_n": stage_n,
        "checks": checks,
        "checks_ok": sum(1 for c in checks if c["ok"]),
        "confidence": round(conf, 1),
        "confidence_label": conf_label,
        "confidence_note": conf_note if direction != "NEUTRAL" else None,
        "state": state,
        "price": price_out,
        "invalidation": inv,
        "status": "DEGRADED" if degraded else "LIVE",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "components": {
            "dir_raw": round(float(dir_raw), 4),
            "struct": round(float(struct), 4),
            "trend_z": round(trend_z, 4),
            "hedge": round(hedge, 3),
            "bull": round(bull, 3),
            "bear": round(bear, 3),
            "mixed": round(mixed, 3),
            "alerts": n_alerts,
            "calls": n_calls,
            "puts": n_puts,
            "no_flow": no_flow,
            "ohlc_ok": bool(ohlc.get("ok")),
            "atr": round(float(ohlc.get("atr") or 0), 4) if ohlc.get("ok") else None,
        },
        "analysis_summary": analysis.summary,
        "drivers": analysis.drivers,
        "gamma_regime": regime,
        "method": (
            "Vector = checklist de 5 evidencias (flujo, precio, persistencia, gamma, "
            "estructura); etapa = nº de checks; confianza calibrada con outcomes "
            "reales cuando hay ≥20; invalidación = muro OI opuesto o ATR + horizonte temporal. "
            f"Régimen gamma {regime['regime']}: peso de tendencia ×{trend_w}."
        ),
    }
