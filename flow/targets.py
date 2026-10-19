"""Institutional target levels: what strikes are desks aiming at?

The gamma panel shows where hedges *are* (open interest). This answers the
complementary question from *today's flow*: where is institutional premium
going — which strikes are they buying calls at (upside objectives), puts at
(downside objectives), and where are they paying for protection (hedges)?

Strikes are clustered in ~2% bands so a desk working 950/955/960 prints as
one level, with aggregated premium, print count and conviction.
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


def _classify(alert: dict[str, Any]) -> str:
    """bull_target | bear_target | hedge | ignore"""
    label = str(alert.get("inst_label") or "")
    cp = str(alert.get("call_put") or "")
    if label == "HEDGE":
        return "hedge"
    if cp == "C":
        return "bull_target"
    if cp == "P":
        return "bear_target"
    return "ignore"


def _cluster(
    rows: list[dict[str, Any]], band_pct: float = 0.02, top_n: int = 3
) -> list[dict[str, Any]]:
    """Group nearby strikes into levels, ranked by institutional premium."""
    if not rows:
        return []
    ordered = sorted(rows, key=lambda a: _safe(a.get("strike")))
    groups: list[list[dict[str, Any]]] = []
    for a in ordered:
        s = _safe(a.get("strike"))
        if s <= 0:
            continue
        if groups and abs(s - _safe(groups[-1][-1].get("strike"))) / s <= band_pct:
            groups[-1].append(a)
        else:
            groups.append([a])
    out = []
    for g in groups:
        prem = sum(_safe(a.get("premium_usd")) for a in g)
        if prem <= 0:
            continue
        strikes = [_safe(a.get("strike")) for a in g]
        wavg = sum(_safe(a.get("strike")) * _safe(a.get("premium_usd")) for a in g) / prem
        scores = [_safe(a.get("inst_score")) for a in g if _safe(a.get("inst_score")) > 0]
        confs = [_safe(a.get("side_conf")) for a in g]
        n_blocks = sum(1 for a in g if a.get("is_block"))
        combos = sorted({str(a.get("combo_type")) for a in g if a.get("combo_type")})
        spots = [_safe(a.get("spot")) for a in g if _safe(a.get("spot")) > 0]
        spot = sum(spots) / len(spots) if spots else None
        out.append(
            {
                "strike_lo": round(min(strikes), 2),
                "strike_hi": round(max(strikes), 2),
                "strike": round(wavg, 2),
                "premium_usd": round(prem, 0),
                "n_prints": len(g),
                "n_blocks": n_blocks,
                "avg_score": round(sum(scores) / len(scores), 3) if scores else None,
                "avg_side_conf": round(sum(confs) / len(confs), 2) if confs else None,
                "combos": combos,
                "otm_pct": round((wavg / spot - 1) * 100, 1) if spot else None,
            }
        )
    out.sort(key=lambda l: l["premium_usd"], reverse=True)
    return out[:top_n]


def institutional_targets(
    alerts: list[dict[str, Any]],
    underlying: str = "MU",
    top_n: int = 3,
    band_pct: float = 0.02,
) -> dict[str, Any]:
    """Bull/bear/hedge target levels from institutional flow."""
    und = underlying.upper()
    scoped = [
        a for a in alerts
        if str(a.get("underlying") or "").upper() == und and _safe(a.get("strike")) > 0
        and _classify(a) != "ignore"
    ]
    buckets: dict[str, list[dict[str, Any]]] = {
        "bull_target": [], "bear_target": [], "hedge": [],
    }
    for a in scoped:
        key = _classify(a)
        if key in buckets:
            buckets[key].append(a)
    return {
        "underlying": und,
        "n_alerts": len(scoped),
        "bull_targets": _cluster(buckets["bull_target"], band_pct, top_n),
        "bear_targets": _cluster(buckets["bear_target"], band_pct, top_n),
        "hedge_levels": _cluster(buckets["hedge"], band_pct, top_n),
    }
