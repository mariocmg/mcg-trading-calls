"""Cboe public delayed options chain (no API key)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
import re

from flow.util import dte_from_expiry, http_get_json, mid, quote_side_bias, safe_float, safe_int

OCC_RE = re.compile(
    r"^(?P<root>[A-Z]+)(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<cp>[CP])(?P<strike>\d{8})$"
)

# Backwards-compatible aliases (moved to flow.util)
_safe_float = safe_float
_safe_int = safe_int
_mid = mid


def _parse_occ(symbol: str) -> dict[str, Any] | None:
    m = OCC_RE.match(symbol.replace(" ", ""))
    if not m:
        return None
    yy, mm, dd = int(m["yy"]), int(m["mm"]), int(m["dd"])
    year = 2000 + yy
    expiry = f"{year:04d}-{mm:02d}-{dd:02d}"
    strike = int(m["strike"]) / 1000.0
    return {
        "expiry": expiry,
        "call_put": m["cp"],
        "strike": strike,
        # DTE measured in America/New_York (options session date), not server local time
        "dte": dte_from_expiry(expiry),
    }


def _side(bid: float | None, ask: float | None, last: float | None) -> str:
    # Kept as a thin alias; logic lives in flow.util.quote_side_bias
    return quote_side_bias(bid, ask, last)


def _aggression(bid: float | None, ask: float | None, last: float | None) -> float:
    from flow.util import quote_aggression

    return quote_aggression(bid, ask, last)[1]


def fetch_cboe_chain(
    symbol: str,
    max_expiries: int = 4,
    min_dte: int = 0,
    max_dte: int = 45,
    max_moneyness: float = 0.12,
) -> list[dict[str, Any]]:
    sym = symbol.upper()
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
    # Retries with backoff; tighter timeout so a hung CDN can't stall the scan loop
    payload = http_get_json(url, timeout=15.0, attempts=3)

    data = payload.get("data") or {}
    spot = _safe_float(data.get("current_price"))
    options = data.get("options") or []
    if not options:
        return []

    under_ltt = data.get("last_trade_time")
    # Collect nearby expiries ranked by DTE
    by_exp: dict[str, list[dict[str, Any]]] = {}
    for raw in options:
        occ = str(raw.get("option") or "")
        parsed = _parse_occ(occ)
        if not parsed:
            continue
        dte = parsed["dte"]
        if dte < min_dte or dte > max_dte:
            continue
        strike = parsed["strike"]
        if spot and spot > 0 and max_moneyness > 0:
            if abs(strike - spot) / spot > max_moneyness:
                continue
        bid = _safe_float(raw.get("bid"))
        ask = _safe_float(raw.get("ask"))
        last = _safe_float(raw.get("last_trade_price"))
        price = _mid(bid, ask, last)
        vol = _safe_int(raw.get("volume")) or 0
        oi = _safe_int(raw.get("open_interest")) or 0
        ltt = raw.get("last_trade_time")
        row = {
            "underlying": sym,
            "expiry": parsed["expiry"],
            "contract": occ,
            "call_put": parsed["call_put"],
            "strike": strike,
            "last": last,
            "bid": bid,
            "ask": ask,
            "price": price,
            "volume": vol,
            "open_interest": oi,
            "implied_volatility": _safe_float(raw.get("iv")),
            "gamma": _safe_float(raw.get("gamma")),
            "dte": dte,
            "spot": spot,
            "side_bias": _side(bid, ask, last),
            "side_conf": _aggression(bid, ask, last),
            "last_trade_time": str(ltt) if ltt else None,
            "underlying_last_trade_time": str(under_ltt) if under_ltt else None,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": "cboe",
        }
        by_exp.setdefault(parsed["expiry"], []).append(row)

    expiries = sorted(by_exp.keys(), key=lambda e: date.fromisoformat(e))[: max(1, max_expiries)]
    rows: list[dict[str, Any]] = []
    for exp in expiries:
        rows.extend(by_exp[exp])
    return rows


def cboe_chain_is_stale(rows: list[dict[str, Any]]) -> bool:
    """True when Cboe delayed CDN has no prints from today's ET session."""
    from flow.market_hours import is_current_session_trade, session_date_et

    if not rows:
        return True
    under = rows[0].get("underlying_last_trade_time")
    if under and is_current_session_trade(under):
        return False
    # Any option trade stamped today in RTH?
    for r in rows:
        ltt = r.get("last_trade_time")
        if ltt and is_current_session_trade(ltt):
            return False
    # If underlying stamp is a prior calendar day → stale
    from flow.market_hours import parse_et_timestamp

    dt = parse_et_timestamp(under) if under else None
    if dt is not None and dt.date() < session_date_et():
        return True
    # No usable trade stamps while session is open → treat as stale
    return True
