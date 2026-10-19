"""Shared small helpers: safe numeric parsing, quote mid, ET dates, HTTP with retries."""

from __future__ import annotations

import json
import math
import random
import time
from datetime import date
from typing import Any, Callable, TypeVar
from urllib.request import Request, urlopen


def safe_float(v: Any) -> float | None:
    """float(v) or None. Also treats NaN/inf as None."""
    try:
        if v is None:
            return None
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except (TypeError, ValueError):
        return None


def safe_int(v: Any) -> int | None:
    f = safe_float(v)
    if f is None:
        return None
    return int(f)


def mid(bid: float | None, ask: float | None, last: float | None) -> float | None:
    """Best-effort quote mid: (bid+ask)/2, else last, ask, bid."""
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


def quote_side_bias(bid: float | None, ask: float | None, last: float | None) -> str:
    """Weak bias from a delayed quote: last vs mid. Not true tape aggression."""
    if last is None or bid is None or ask is None or ask <= bid:
        return "unknown"
    m = (bid + ask) / 2.0
    if last >= ask * 0.98:
        return "buyish"
    if last <= bid * 1.02:
        return "sellish"
    return "buyish" if last >= m else "sellish"


def quote_aggression(
    bid: float | None, ask: float | None, last: float | None
) -> tuple[str, float]:
    """Continuous side read with uncertainty: (bias, confidence 0..1).

    A print glued to the ask with a tight spread is strong evidence of buying;
    a mid print with a wide spread is noise. Confidence penalizes both wide
    spreads (stale/illiquid quotes) and mid-print ambiguity, so downstream
    logic can weight the read instead of treating it as certain.
    """
    if last is None or bid is None or ask is None or ask <= bid or bid <= 0:
        return "unknown", 0.0
    spread = ask - bid
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return "unknown", 0.0
    spread_pct = spread / mid
    pos = (last - bid) / spread  # 0 = at bid, 1 = at ask
    if pos >= 0.75:
        bias = "buyish"
    elif pos <= 0.25:
        bias = "sellish"
    else:
        bias = "neutral"
    tight = max(0.0, 1.0 - spread_pct / 0.04)  # full marks while spread < 4%
    edge = abs(pos - 0.5) * 2.0               # 0 at mid, 1 touching bid/ask
    conf = round(tight * (0.35 + 0.65 * edge), 3)
    return bias, conf


def et_today() -> date:
    """Calendar date in America/New_York (options session date)."""
    from flow.market_hours import session_date_et

    return session_date_et()


def dte_from_expiry(expiry_iso: str) -> int | None:
    """Days to expiry from an ISO date, measured in ET (not server local time)."""
    try:
        exp = date.fromisoformat(expiry_iso)
    except ValueError:
        return None
    return (exp - et_today()).days


T = TypeVar("T")


def with_retries(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 8.0,
    label: str = "request",
) -> T:
    """Run fn() with exponential backoff + jitter. Raises the last exception."""
    last: BaseException | None = None
    for i in range(max(1, attempts)):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - retried, then re-raised
            last = exc
            if i < attempts - 1:
                delay = min(max_delay, base_delay * (2**i)) * (0.7 + 0.6 * random.random())
                time.sleep(delay)
    assert last is not None
    raise last


def http_get_json(
    url: str,
    *,
    timeout: float = 15.0,
    attempts: int = 3,
    user_agent: str = "FLOW/0.1 (+local research)",
) -> Any:
    """GET JSON over HTTP with retries/backoff. Raises on final failure."""

    def _do() -> Any:
        req = Request(url, headers={"User-Agent": user_agent})
        with urlopen(req, timeout=timeout) as resp:
            return json.load(resp)

    return with_retries(_do, attempts=attempts, label=url)
