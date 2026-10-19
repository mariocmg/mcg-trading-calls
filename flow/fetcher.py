from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pandas as pd
import yfinance as yf


def _mid(bid: float | None, ask: float | None, last: float | None) -> float | None:
    try:
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            return (float(bid) + float(ask)) / 2.0
        if last is not None and last > 0:
            return float(last)
        if ask is not None and ask > 0:
            return float(ask)
        if bid is not None and bid > 0:
            return float(bid)
    except (TypeError, ValueError):
        return None
    return None


def _safe_int(v: Any) -> int | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _safe_float(v: Any) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _dte(expiry: str) -> int:
    exp = date.fromisoformat(expiry)
    return (exp - date.today()).days


def _side_from_chain(row: pd.Series) -> str:
    """Weak bias from delayed chain: last vs mid. Not true tape aggression."""
    last = _safe_float(row.get("lastPrice"))
    bid = _safe_float(row.get("bid"))
    ask = _safe_float(row.get("ask"))
    if last is None or bid is None or ask is None or ask <= bid:
        return "unknown"
    mid = (bid + ask) / 2.0
    if last >= ask * 0.98:
        return "buyish"
    if last <= bid * 1.02:
        return "sellish"
    if last >= mid:
        return "buyish"
    return "sellish"


def fetch_chain(
    symbol: str,
    max_expiries: int = 4,
    min_dte: int = 0,
    max_dte: int = 45,
    max_moneyness: float = 0.12,
) -> list[dict[str, Any]]:
    """Pull delayed options chain rows for nearby expiries via Yahoo Finance."""
    ticker = yf.Ticker(symbol)
    spot = None
    try:
        fast = ticker.fast_info
        spot = float(getattr(fast, "last_price", None) or fast.get("lastPrice") or 0) or None
    except Exception:
        spot = None
    if spot is None:
        try:
            hist = ticker.history(period="1d")
            if not hist.empty:
                spot = float(hist["Close"].iloc[-1])
        except Exception:
            spot = None

    expiries = list(ticker.options or [])
    if not expiries:
        return []

    rows: list[dict[str, Any]] = []
    used = 0
    for expiry in expiries:
        dte = _dte(expiry)
        if dte < min_dte or dte > max_dte:
            continue
        if used >= max_expiries:
            break
        used += 1
        try:
            chain = ticker.option_chain(expiry)
        except Exception:
            continue

        for call_put, frame in (("C", chain.calls), ("P", chain.puts)):
            if frame is None or frame.empty:
                continue
            for _, r in frame.iterrows():
                contract = str(r.get("contractSymbol") or "")
                if not contract:
                    continue
                strike = float(r["strike"])
                if spot and spot > 0 and max_moneyness > 0:
                    if abs(strike - spot) / spot > max_moneyness:
                        continue
                bid = _safe_float(r.get("bid"))
                ask = _safe_float(r.get("ask"))
                last = _safe_float(r.get("lastPrice"))
                price = _mid(bid, ask, last)
                vol = _safe_int(r.get("volume")) or 0
                oi = _safe_int(r.get("openInterest")) or 0
                rows.append(
                    {
                        "underlying": symbol.upper(),
                        "expiry": expiry,
                        "contract": contract,
                        "call_put": call_put,
                        "strike": strike,
                        "last": last,
                        "bid": bid,
                        "ask": ask,
                        "price": price,
                        "volume": vol,
                        "open_interest": oi,
                        "implied_volatility": _safe_float(r.get("impliedVolatility")),
                        "gamma": _safe_float(r.get("gamma")),
                        "dte": dte,
                        "spot": spot,
                        "side_bias": _side_from_chain(r),
                        "last_trade_time": None,
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                        "source": "yahoo",
                    }
                )
    return rows


def fetch_watchlist(
    symbols: list[str],
    max_expiries: int = 4,
    min_dte: int = 0,
    max_dte: int = 45,
    max_moneyness: float = 0.12,
    provider: str = "cboe",
) -> list[dict[str, Any]]:
    provider = (provider or "cboe").lower()
    all_rows: list[dict[str, Any]] = []
    for sym in symbols:
        if provider == "yahoo":
            all_rows.extend(
                fetch_chain(
                    sym,
                    max_expiries=max_expiries,
                    min_dte=min_dte,
                    max_dte=max_dte,
                    max_moneyness=max_moneyness,
                )
            )
            continue

        from flow.cboe import cboe_chain_is_stale, fetch_cboe_chain

        rows: list[dict[str, Any]] = []
        try:
            rows = fetch_cboe_chain(
                sym,
                max_expiries=max_expiries,
                min_dte=min_dte,
                max_dte=max_dte,
                max_moneyness=max_moneyness,
            )
        except Exception:
            rows = []

        if (not rows) or cboe_chain_is_stale(rows):
            yrows = fetch_chain(
                sym,
                max_expiries=max_expiries,
                min_dte=min_dte,
                max_dte=max_dte,
                max_moneyness=max_moneyness,
            )
            for r in yrows:
                r["fallback_reason"] = "cboe_stale"
            all_rows.extend(yrows)
        else:
            all_rows.extend(rows)
    return all_rows
