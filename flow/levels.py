"""Strike-level positioning from the chain snapshot.

- GEX (dollar gamma per 1% move): gamma × OI × 100 × spot², calls +, puts −.
- OI walls: strikes with the largest call / put open interest.
- Gamma flip: strike where cumulative net GEX crosses zero.
- Max pain per expiry: strike minimizing total intrinsic payout.
- DTE buckets: where the premium/OI sits in time (0-7 / 8-21 / 22-45).

All from delayed snapshots — levels move slowly, so delayed data is fine here.
"""

from __future__ import annotations

from typing import Any


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


def strike_levels(rows: list[dict[str, Any]], underlying: str) -> list[dict[str, Any]]:
    """Per-strike aggregates for one underlying, across expiries."""
    und = underlying.upper()
    spot = 0.0
    by_key: dict[tuple[str, float], dict[str, float]] = {}
    for r in rows:
        if str(r.get("underlying") or "").upper() != und:
            continue
        strike = round(_safe(r.get("strike")), 2)
        if strike <= 0:
            continue
        expiry = str(r.get("expiry") or "")
        key = (expiry, strike)
        lv = by_key.setdefault(key, {
            "expiry": expiry, "strike": strike,
            "call_oi": 0, "put_oi": 0,
            "call_gex": 0.0, "put_gex": 0.0,
            "call_premium": 0.0, "put_premium": 0.0,
        })
        oi = int(_safe(r.get("open_interest")))
        vol = _safe(r.get("volume"))
        price = _safe(r.get("price"))
        spot = _safe(r.get("spot")) or spot
        gamma = r.get("gamma")
        if r.get("call_put") == "C":
            lv["call_oi"] += oi
            lv["call_premium"] += vol * price * 100.0
            if gamma is not None and oi > 0 and spot > 0:
                lv["call_gex"] += float(gamma) * oi * 100.0 * spot * spot
        elif r.get("call_put") == "P":
            lv["put_oi"] += oi
            lv["put_premium"] += vol * price * 100.0
            if gamma is not None and oi > 0 and spot > 0:
                lv["put_gex"] += float(gamma) * oi * 100.0 * spot * spot
    out = []
    for lv in by_key.values():
        lv["net_gex"] = lv["call_gex"] - lv["put_gex"]
        out.append(lv)
    return sorted(out, key=lambda l: (l["expiry"], l["strike"]))


def _walls(levels: list[dict[str, Any]]) -> dict[str, Any]:
    """Largest call-OI and put-OI strikes (all expiries pooled)."""
    call_wall = max(levels, key=lambda l: l["call_oi"], default=None)
    put_wall = max(levels, key=lambda l: l["put_oi"], default=None)
    return {
        "call_wall": {"strike": call_wall["strike"], "oi": call_wall["call_oi"],
                      "expiry": call_wall["expiry"]} if call_wall and call_wall["call_oi"] > 0 else None,
        "put_wall": {"strike": put_wall["strike"], "oi": put_wall["put_oi"],
                     "expiry": put_wall["expiry"]} if put_wall and put_wall["put_oi"] > 0 else None,
    }


def _gamma_flip(levels: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Strike where cumulative net GEX (sorted by strike) crosses zero."""
    if not any(l["net_gex"] for l in levels):
        return None
    ordered = sorted(levels, key=lambda l: l["strike"])
    cum = 0.0
    prev = None
    for lv in ordered:
        prev_cum = cum
        cum += lv["net_gex"]
        if prev is not None and (prev_cum <= 0) != (cum <= 0) and cum != prev_cum:
            return {"strike": lv["strike"], "expiry": lv["expiry"]}
        prev = lv
    return None


def _max_pain(levels: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Max-pain strike per expiry (minimizes total intrinsic payout)."""
    by_exp: dict[str, list[dict[str, Any]]] = {}
    for lv in levels:
        by_exp.setdefault(lv["expiry"], []).append(lv)
    out: dict[str, dict[str, Any]] = {}
    for expiry, lvs in by_exp.items():
        strikes = sorted({l["strike"] for l in lvs})
        if not strikes:
            continue
        best, best_pain = None, None
        for k in strikes:
            pain = 0.0
            for l in lvs:
                pain += max(l["strike"] - k, 0) * l["call_oi"]
                pain += max(k - l["strike"], 0) * l["put_oi"]
            if best_pain is None or pain < best_pain:
                best, best_pain = k, pain
        if best is not None:
            out[expiry] = {"strike": best, "pain_dollars": round(best_pain or 0, 0)}
    return out


def dte_buckets(rows: list[dict[str, Any]], underlying: str) -> dict[str, dict[str, float]]:
    """Premium and OI by time bucket — where is the positioning in time?"""
    und = underlying.upper()
    buckets = {
        "0-7": (0, 7), "8-21": (8, 21), "22-45": (22, 45),
    }
    out: dict[str, dict[str, float]] = {
        name: {"call_premium": 0.0, "put_premium": 0.0, "call_oi": 0, "put_oi": 0}
        for name in buckets
    }
    for r in rows:
        if str(r.get("underlying") or "").upper() != und:
            continue
        try:
            dte = int(r.get("dte"))
        except (TypeError, ValueError):
            continue
        name = next((n for n, (lo, hi) in buckets.items() if lo <= dte <= hi), None)
        if name is None:
            continue
        b = out[name]
        prem = _safe(r.get("volume")) * _safe(r.get("price")) * 100.0
        oi = int(_safe(r.get("open_interest")))
        if r.get("call_put") == "C":
            b["call_premium"] += prem
            b["call_oi"] += oi
        elif r.get("call_put") == "P":
            b["put_premium"] += prem
            b["put_oi"] += oi
    for b in out.values():
        b["call_premium"] = round(b["call_premium"], 0)
        b["put_premium"] = round(b["put_premium"], 0)
        tot = b["call_premium"] + b["put_premium"]
        b["call_share"] = round(b["call_premium"] / tot, 3) if tot > 0 else None
    return out


def summarize_persisted(level_rows: list[dict[str, Any]], underlying: str) -> dict[str, Any]:
    """Walls / gamma flip / max pain recomputed from persisted strike rows."""
    levels = [
        {
            "expiry": r["expiry"],
            "strike": float(r["strike"]),
            "call_oi": int(r.get("call_oi") or 0),
            "put_oi": int(r.get("put_oi") or 0),
            "call_gex": float(r.get("call_gex") or 0),
            "put_gex": float(r.get("put_gex") or 0),
            "net_gex": float(r.get("net_gex") or 0),
        }
        for r in level_rows
    ]
    walls = _walls(levels)
    flip = _gamma_flip(levels)
    max_pain = _max_pain(levels)
    net_gex = sum(l["net_gex"] for l in levels)
    nearest_mp = None
    if max_pain:
        nearest_exp = sorted(max_pain.keys())[0]
        nearest_mp = {"expiry": nearest_exp, **max_pain[nearest_exp]}
    return {
        "underlying": underlying.upper(),
        "n_strikes": len(levels),
        "net_gex": round(net_gex, 0),
        "net_gex_millions": round(net_gex / 1e6, 2),
        **walls,
        "gamma_flip": flip,
        "max_pain_nearest": nearest_mp,
        "max_pain_by_expiry": max_pain,
    }


def compute_positioning_levels(
    rows: list[dict[str, Any]], underlying: str
) -> dict[str, Any]:
    """Full positioning picture for one underlying."""
    und = underlying.upper()
    levels = strike_levels(rows, underlying)
    walls = _walls(levels)
    flip = _gamma_flip(levels)
    max_pain = _max_pain(levels)
    buckets = dte_buckets(rows, underlying)

    net_gex = sum(l["net_gex"] for l in levels)
    # Nearest expiries' max pain first
    nearest_mp = None
    if max_pain:
        nearest_exp = sorted(max_pain.keys())[0]
        nearest_mp = {"expiry": nearest_exp, **max_pain[nearest_exp]}

    return {
        "underlying": und,
        "n_strikes": len(levels),
        "net_gex": round(net_gex, 0),
        "net_gex_millions": round(net_gex / 1e6, 2),
        **walls,
        "gamma_flip": flip,
        "max_pain_nearest": nearest_mp,
        "max_pain_by_expiry": max_pain,
        "dte_buckets": buckets,
        "levels": levels,  # full ladder for the API / DB persist
    }
