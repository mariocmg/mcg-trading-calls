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

from flow.ml_positioning import analyze_alerts, extract_features, _safe, _prem

#: Confidence is clamped to [CONF_FLOOR, 96.5]. Be aware: with delayed chains
#: and a heuristic score, anything in the 52-65 ("LOW") band is essentially a
#: coin flip — size positions accordingly, don't treat it as an edge.
CONF_FLOOR = 52.0
CONF_CAP = 96.5


def _fetch_ohlc(symbol: str, period: str = "3mo") -> dict[str, Any]:
    """Recent OHLC for trend / ATR. Best-effort; empty if unavailable."""
    try:
        import yfinance as yf

        hist = yf.Ticker(symbol).history(period=period, auto_adjust=True)
        if hist is None or hist.empty or len(hist) < 15:
            return {"ok": False, "bars": 0}
        closes = hist["Close"].astype(float)
        highs = hist["High"].astype(float)
        lows = hist["Low"].astype(float)
        spot = float(closes.iloc[-1])

        # Wilder-ish ATR(14)
        prev_close = closes.shift(1)
        tr = np.maximum(
            highs - lows,
            np.maximum((highs - prev_close).abs(), (lows - prev_close).abs()),
        )
        atr = float(tr.tail(14).mean())

        # Trend: distance of spot vs SMA20 / SMA50 in ATR units
        sma20 = float(closes.tail(20).mean())
        sma50 = float(closes.tail(50).mean()) if len(closes) >= 50 else sma20
        ret_5 = float(closes.iloc[-1] / closes.iloc[-6] - 1) if len(closes) >= 6 else 0.0
        ret_20 = float(closes.iloc[-1] / closes.iloc[-21] - 1) if len(closes) >= 21 else 0.0

        # Swing levels (10-day)
        swing_low = float(lows.tail(10).min())
        swing_high = float(highs.tail(10).max())

        trend_z = 0.0
        if atr > 0:
            trend_z = ((spot - sma20) / atr) * 0.6 + ((sma20 - sma50) / atr) * 0.4

        return {
            "ok": True,
            "bars": int(len(hist)),
            "spot": spot,
            "atr": atr,
            "sma20": sma20,
            "sma50": sma50,
            "ret_5": ret_5,
            "ret_20": ret_20,
            "trend_z": float(trend_z),
            "swing_low": swing_low,
            "swing_high": swing_high,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "bars": 0, "error": str(exc)}


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
) -> dict[str, Any]:
    und = underlying.upper()
    scoped = [
        a for a in alerts if str(a.get("underlying", "")).upper() == und
    ]
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
    # Hedge dampening: hedges shrink conviction unless price strongly confirms later
    struct *= 1.0 - 0.55 * hedge

    trend_z = float(ohlc.get("trend_z") or 0.0) if ohlc.get("ok") else 0.0
    dir_raw = struct + 0.35 * np.tanh(trend_z)

    if no_flow:
        direction = "NEUTRAL"
        dir_raw = 0.0
        struct = 0.0
        flow_ok, flow_score = False, 0.0
        price_ok, price_score = False, 0.0
        persist_ok, persist_score = False, 0.0
        bits = 0
        stage, stage_n = "EARLY", 1
        conf = 0.0
        conf_label = "—"
        degraded = True
    else:
        # Need some call OR put premium structure; never invent bull from SMA alone.
        if abs(struct) < 0.03:
            direction = "NEUTRAL"
        elif abs(dir_raw) < 0.04 and mixed > 0.35:
            direction = "NEUTRAL"
        else:
            direction = "BULLISH" if dir_raw >= 0 else "BEARISH"

        if direction == "NEUTRAL":
            flow_ok, flow_score = False, 0.0
            price_ok, price_score = False, 0.0
            persist_ok, persist_score = False, 0.0
            bits = 0
            stage, stage_n = "EARLY", 1
            conf = float(max(0.0, min(55.0, 28.0 + 10.0 * min(1.0, n_alerts / 20.0))))
            conf_label = "LOW"
            degraded = True
        else:
            flow_ok, flow_score = _flow_alignment(feats, direction)
            price_ok, price_score = _price_confirmation(ohlc, direction)
            persist_ok, persist_score = _persistence(scoped, direction)

            bits = int(flow_ok) + int(price_ok) + int(persist_ok)
            if bits == 1 and abs(dir_raw) >= 0.25 and flow_score >= 0.35:
                bits = 2
            if hedge >= 0.5 and abs(struct) < 0.18:
                bits = min(bits, 2)
            if bits >= 3:
                stage, stage_n = "CONFIRMED", 3
            elif bits == 2:
                stage, stage_n = "BUILDING", 2
            else:
                stage, stage_n = "EARLY", 1

            data_quality = min(1.0, n_alerts / 40.0) * 0.5 + (
                0.5 if ohlc.get("ok") else 0.15
            )
            conf = (
                38.0
                + 22.0 * abs(dir_raw)
                + 14.0 * flow_score
                + 14.0 * price_score
                + 10.0 * persist_score
                + 8.0 * (stage_n / 3.0)
                + 10.0 * data_quality
                - 18.0 * hedge
                - 8.0 * mixed
            )
            conf = float(max(CONF_FLOOR, min(CONF_CAP, conf)))
            conf_label = "HIGH" if conf >= 78 else ("MEDIUM" if conf >= 65 else "LOW")
            degraded = (not ohlc.get("ok")) or n_alerts < 12 or hedge >= 0.55

    state = _state_copy(
        stage, direction, flow_ok, price_ok, hedge, no_flow=no_flow or direction == "NEUTRAL"
    )

    if spot is None or spot <= 0:
        inv = {"price": None, "arrow": "", "method": "n/a"}
        price_out = None
    elif direction == "NEUTRAL":
        inv = {"price": None, "arrow": "", "method": "n/a — sin tesis direccional"}
        price_out = round(spot, 2)
    else:
        inv = _invalidation(ohlc if ohlc.get("ok") else {"atr": spot * 0.02}, spot, direction)
        price_out = round(spot, 2)

    return {
        "symbol": und,
        "direction": direction,
        "stage": stage,
        "stage_fraction": f"{stage_n}/3",
        "stage_n": stage_n,
        "confidence": round(conf, 1) if direction != "NEUTRAL" or not no_flow else (0 if no_flow else round(conf, 1)),
        "confidence_label": conf_label,
        "confidence_note": (
            "LOW (52-65) ≈ coin flip on delayed data — not an edge"
            if direction != "NEUTRAL" and conf_label == "LOW"
            else None
        ),
        "state": state,
        "price": price_out,
        "invalidation": inv,
        "status": "DEGRADED" if degraded else "LIVE",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "components": {
            "dir_raw": round(float(dir_raw), 4),
            "struct": round(float(struct), 4),
            "trend_z": round(trend_z, 4),
            "flow_ok": flow_ok,
            "flow_score": round(flow_score, 3),
            "price_ok": price_ok,
            "price_score": round(price_score, 3),
            "persist_ok": persist_ok,
            "persist_score": round(persist_score, 3),
            "hedge": round(hedge, 3),
            "bull": round(bull, 3),
            "bear": round(bear, 3),
            "mixed": round(mixed, 3),
            "bits": bits,
            "alerts": n_alerts,
            "calls": n_calls,
            "puts": n_puts,
            "no_flow": no_flow,
            "ohlc_ok": bool(ohlc.get("ok")),
            "atr": round(float(ohlc.get("atr") or 0), 4) if ohlc.get("ok") else None,
        },
        "analysis_summary": analysis.summary,
        "drivers": analysis.drivers,
        "method": (
            "Vector = structure residual (post-hedge) + price ATR trend + "
            "alert persistence; invalidation = swing±1.5·ATR. "
            "Sin tape RTH → NEUTRAL (precio solo no define dirección)."
        ),
    }
