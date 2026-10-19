from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd
import yfinance as yf

from flow.util import dte_from_expiry, mid, quote_side_bias, safe_float, safe_int, with_retries

# Backwards-compatible aliases (moved to flow.util)
_mid = mid
_safe_int = safe_int
_safe_float = safe_float


def _dte(expiry: str) -> int | None:
    # DTE measured in America/New_York (options session date), not server local time
    return dte_from_expiry(expiry)


def _side_from_chain(row: pd.Series) -> str:
    """Weak bias from delayed chain: last vs mid. Not true tape aggression."""
    last = safe_float(row.get("lastPrice"))
    bid = safe_float(row.get("bid"))
    ask = safe_float(row.get("ask"))
    return quote_side_bias(bid, ask, last)


def _aggression_from_chain(row: pd.Series) -> float:
    from flow.util import quote_aggression

    return quote_aggression(
        safe_float(row.get("bid")), safe_float(row.get("ask")), safe_float(row.get("lastPrice"))
    )[1]


def fetch_chain(
    symbol: str,
    max_expiries: int = 4,
    min_dte: int = 0,
    max_dte: int = 45,
    max_moneyness: float = 0.12,
) -> list[dict[str, Any]]:
    """Pull delayed options chain rows for nearby expiries via Yahoo Finance.

    Network failures are swallowed and reported as an empty chain (the caller
    treats this as "no data" instead of crashing with a traceback).
    """
    ticker = yf.Ticker(symbol)
    spot = None
    try:
        fast = with_retries(lambda: ticker.fast_info, attempts=2, base_delay=1.0)
        spot = float(getattr(fast, "last_price", None) or fast.get("lastPrice") or 0) or None
    except Exception:
        spot = None
    if spot is None:
        try:
            hist = with_retries(lambda: ticker.history(period="1d"), attempts=2, base_delay=1.0)
            if hist is not None and not hist.empty:
                spot = float(hist["Close"].iloc[-1])
        except Exception:
            spot = None

    # A Yahoo hiccup here used to kill the whole CLI with a traceback.
    try:
        expiries = list(with_retries(lambda: ticker.options or [], attempts=3, base_delay=1.5))
    except Exception:
        return []
    if not expiries:
        return []

    rows: list[dict[str, Any]] = []
    used = 0
    for expiry in expiries:
        dte = _dte(expiry)
        if dte is None or dte < min_dte or dte > max_dte:
            continue
        if used >= max_expiries:
            break
        used += 1
        try:
            chain = with_retries(
                lambda e=expiry: ticker.option_chain(e), attempts=2, base_delay=1.0
            )
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
                        "side_conf": _aggression_from_chain(r),
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
