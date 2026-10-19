"""Positioning pieces adapted from an open flow engine, without a trade tape.

- Decomposed score: vol/OI, urgency, repetition, aggression. Missing parts
  are left out and the remaining weights are renormalized.
- Opening: day volume above open interest.
- Session net premium and net gamma by strike from the chain snapshot.
"""

from __future__ import annotations

import math
from typing import Any

WEIGHTS = {
    "vol_oi": 0.34,
    "urgency": 0.22,
    "repetition": 0.18,
    "aggression": 0.26,
}


def _safe(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        x = float(v)
        if x != x or x in (float("inf"), float("-inf")):
            return default
        return x
    except (TypeError, ValueError):
        return default


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def opening_likely(row: dict[str, Any]) -> bool:
    vol = _safe(row.get("volume"))
    oi = _safe(row.get("open_interest"))
    return oi > 0 and vol > oi


def decompose_score(row: dict[str, Any], cluster_n: int = 1) -> dict[str, Any]:
    """Return 0–1 score plus the parts that actually had an input."""
    parts: dict[str, float | None] = {}

    vol = _safe(row.get("volume"))
    oi = _safe(row.get("open_interest"))
    if oi > 0 and vol > 0:
        ratio = vol / oi
        parts["vol_oi"] = _clip01(math.log1p(ratio) / math.log1p(4.0))
    else:
        parts["vol_oi"] = None

    dte = row.get("dte")
    otm = row.get("otm_pct")
    if dte is None and otm is None:
        parts["urgency"] = None
    else:
        dte_f = max(_safe(dte, 21.0), 0.0)
        otm_f = max(_safe(otm), 0.0)
        dte_factor = 1.0 / (1.0 + dte_f / 7.0)
        otm_factor = _clip01(otm_f / 12.0)
        parts["urgency"] = _clip01(0.6 * dte_factor + 0.4 * otm_factor)

    hits = max(int(cluster_n or 1) - 1, 0)
    parts["repetition"] = _clip01(math.log1p(hits) / math.log1p(4.0))

    side = str(row.get("side_bias") or "unknown")
    bid = row.get("bid")
    ask = row.get("ask")
    if side == "buyish" and bid is not None and ask is not None:
        parts["aggression"] = 1.0
    elif side == "sellish" and bid is not None and ask is not None:
        parts["aggression"] = 0.35
    else:
        parts["aggression"] = None

    active = {k: v for k, v in parts.items() if v is not None}
    weight_sum = sum(WEIGHTS[k] for k in active) or 1.0
    score = sum(WEIGHTS[k] / weight_sum * float(active[k]) for k in active)
    return {
        "score": round(_clip01(score), 3),
        "parts": {k: (None if v is None else round(float(v), 3)) for k, v in parts.items()},
        "missing": [k for k, v in parts.items() if v is None],
        "opening": opening_likely(row),
    }


def _wavg(weight_strike: list[tuple[float, float]]) -> float | None:
    w = sum(p for p, _ in weight_strike)
    if w <= 0:
        return None
    return sum(p * s for p, s in weight_strike) / w


def session_premium(rows: list[dict[str, Any]], underlying: str) -> dict[str, Any]:
    """Session premium plus premium-weighted average strike (where they position)."""
    und = underlying.upper()
    call_p = 0.0
    put_p = 0.0
    call_ws: list[tuple[float, float]] = []
    put_ws: list[tuple[float, float]] = []
    for r in rows:
        if str(r.get("underlying") or "").upper() != und:
            continue
        vol = max(_safe(r.get("volume")), 0.0)
        price = max(_safe(r.get("price")), 0.0)
        strike = _safe(r.get("strike"))
        if vol <= 0 or strike <= 0:
            continue
        prem = vol * price * 100.0
        if r.get("call_put") == "C":
            call_p += prem
            call_ws.append((prem, strike))
        elif r.get("call_put") == "P":
            put_p += prem
            put_ws.append((prem, strike))
    call_avg = _wavg(call_ws)
    put_avg = _wavg(put_ws)
    both_avg = _wavg(call_ws + put_ws)
    return {
        "call_premium": round(call_p, 2),
        "put_premium": round(put_p, 2),
        "net_premium": round(call_p - put_p, 2),
        "call_avg_strike": round(call_avg, 2) if call_avg is not None else None,
        "put_avg_strike": round(put_avg, 2) if put_avg is not None else None,
        "avg_strike": round(both_avg, 2) if both_avg is not None else None,
    }


def gex_by_strike(rows: list[dict[str, Any]], underlying: str) -> dict[str, Any]:
    """Net gamma by strike. Calls add, puts subtract. Needs gamma on the chain."""
    und = underlying.upper()
    by_strike: dict[float, float] = {}
    spot = 0.0
    used = 0
    for r in rows:
        if str(r.get("underlying") or "").upper() != und:
            continue
        gamma = r.get("gamma")
        if gamma is None:
            continue
        g = _safe(gamma)
        if g == 0:
            continue
        oi = _safe(r.get("open_interest"))
        if oi <= 0:
            continue
        spot = _safe(r.get("spot")) or spot
        sign = 1.0 if r.get("call_put") == "C" else -1.0
        strike = round(_safe(r.get("strike")), 4)
        by_strike[strike] = by_strike.get(strike, 0.0) + sign * g * oi * 100.0 * max(spot, 1.0)
        used += 1
    if not by_strike:
        return {"ok": False, "reason": "sin gamma en la cadena", "strike": None, "gex": None}
    strike, gex = max(by_strike.items(), key=lambda kv: abs(kv[1]))
    return {
        "ok": True,
        "strike": strike,
        "gex": round(gex, 2),
        "side": "CALL" if gex >= 0 else "PUT",
        "contracts": used,
    }


def summarize(rows: list[dict[str, Any]], underlying: str) -> dict[str, Any]:
    prem = session_premium(rows, underlying)
    gex = gex_by_strike(rows, underlying)
    return {"underlying": underlying.upper(), **prem, "gex": gex}
