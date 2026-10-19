"""Shared market-data helpers: OHLC/ATR per symbol and SPY reference returns.

Used by outcomes (ATR-normalized, beta-adjusted labels) and vector
(confidence/invalidation). Results are cached in-process with a TTL so the
90s scan loop doesn't hammer Yahoo.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np

_TTL = 900.0  # 15 min
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _cached(key: str) -> dict[str, Any] | None:
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    return None


def _store(key: str, value: dict[str, Any]) -> dict[str, Any]:
    _cache[key] = (time.time(), value)
    return value


def fetch_ohlc(symbol: str, period: str = "3mo") -> dict[str, Any]:
    """Recent OHLC for trend / ATR. Best-effort; empty if unavailable."""
    key = f"ohlc:{symbol}:{period}"
    hit = _cached(key)
    if hit is not None:
        return hit
    try:
        import yfinance as yf

        hist = yf.Ticker(symbol).history(period=period, auto_adjust=True)
        if hist is None or hist.empty or len(hist) < 15:
            return _store(key, {"ok": False, "bars": 0})
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

        sma20 = float(closes.tail(20).mean())
        sma50 = float(closes.tail(50).mean()) if len(closes) >= 50 else sma20
        ret_5 = float(closes.iloc[-1] / closes.iloc[-6] - 1) if len(closes) >= 6 else 0.0
        ret_20 = float(closes.iloc[-1] / closes.iloc[-21] - 1) if len(closes) >= 21 else 0.0

        swing_low = float(lows.tail(10).min())
        swing_high = float(highs.tail(10).max())

        trend_z = 0.0
        if atr > 0:
            trend_z = ((spot - sma20) / atr) * 0.6 + ((sma20 - sma50) / atr) * 0.4

        return _store(
            key,
            {
                "ok": True,
                "bars": int(len(hist)),
                "spot": spot,
                "atr": atr,
                "atr_pct": atr / spot if spot > 0 else None,
                "sma20": sma20,
                "sma50": sma50,
                "ret_5": ret_5,
                "ret_20": ret_20,
                "trend_z": float(trend_z),
                "swing_low": swing_low,
                "swing_high": swing_high,
                "closes": closes,
            },
        )
    except Exception as exc:  # noqa: BLE001
        return _store(key, {"ok": False, "bars": 0, "error": str(exc)})


def get_atr_pct(symbol: str) -> float | None:
    """ATR as a fraction of spot (e.g. 0.023 = 2.3%). None if unavailable."""
    ohlc = fetch_ohlc(symbol)
    if not ohlc.get("ok"):
        return None
    return ohlc.get("atr_pct")


def spy_return_between(start_date: str, end_date: str) -> float | None:
    """SPY total return between two YYYY-MM-DD dates (inclusive lookup).

    Used to beta-adjust outcome labels: an alert that 'worked' only because
    the whole market ripped isn't evidence of institutional edge.
    """
    key = "spy:1y"
    hit = _cached(key)
    if hit is None:
        try:
            import yfinance as yf

            hist = yf.Ticker("SPY").history(period="1y", auto_adjust=True)
            if hist is None or hist.empty:
                return None
            closes = hist["Close"].astype(float)
            hit = _store(key, {"closes": closes})
        except Exception:  # noqa: BLE001
            return None
    closes = hit["closes"]
    try:
        idx = closes.index
        # nearest trading day on or before each date
        s = closes.loc[:start_date].iloc[-1]
        e = closes.loc[:end_date].iloc[-1]
        if s <= 0:
            return None
        return float(e / s - 1.0)
    except Exception:  # noqa: BLE001
        return None


def clear_cache() -> None:
    _cache.clear()
