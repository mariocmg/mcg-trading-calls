from __future__ import annotations

"""Cboe public delayed options chain (no API key)."""

from datetime import date, datetime, timezone
from typing import Any
from urllib.request import Request, urlopen
import json
import re

OCC_RE = re.compile(
    r"^(?P<root>[A-Z]+)(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<cp>[CP])(?P<strike>\d{8})$"
)


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        x = float(v)
        if x != x:  # NaN
            return None
        return x
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> int | None:
    f = _safe_float(v)
    if f is None:
        return None
    return int(f)


def _mid(bid: float | None, ask: float | None, last: float | None) -> float | None:
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    if last is not None and last > 0:
        return last
    if ask is not None and ask > 0:
        return ask
    if bid is not None and bid > 0:
        return bid
    return None


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
        "dte": (date(year, mm, dd) - date.today()).days,
    }


def _side(bid: float | None, ask: float | None, last: float | None) -> str:
    if last is None or bid is None or ask is None or ask <= bid:
        return "unknown"
    mid = (bid + ask) / 2.0
    if last >= ask * 0.98:
        return "buyish"
    if last <= bid * 1.02:
        return "sellish"
    return "buyish" if last >= mid else "sellish"


def fetch_cboe_chain(
    symbol: str,
    max_expiries: int = 4,
    min_dte: int = 0,
    max_dte: int = 45,
    max_moneyness: float = 0.12,
) -> list[dict[str, Any]]:
    sym = symbol.upper()
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
    req = Request(url, headers={"User-Agent": "FLOW/0.1 (+local research)"})
    with urlopen(req, timeout=45) as resp:
        payload = json.load(resp)

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
