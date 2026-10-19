"""US equity options session helpers (regular hours)."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OPEN = time(9, 30)
CLOSE = time(16, 0)


def now_et() -> datetime:
    return datetime.now(ET)


def us_equity_options_open(when: datetime | None = None) -> bool:
    """True during Mon–Fri 09:30–16:00 America/New_York (regular equity options)."""
    if when is None:
        dt = now_et()
    elif when.tzinfo is None:
        # Naive timestamps from Cboe last_trade_time are Eastern wall-clock
        dt = when.replace(tzinfo=ET)
    else:
        dt = when.astimezone(ET)
    if dt.weekday() >= 5:
        return False
    t = dt.time()
    return OPEN <= t <= CLOSE


def parse_et_timestamp(value: Any) -> datetime | None:
    """Parse Cboe-like '2026-09-22T15:58:10' or ISO with Z as Eastern / aware UTC."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(ET)
        if "+" in s[10:] or s.count("-") > 2:
            return datetime.fromisoformat(s).astimezone(ET)
        # Naive → treat as America/New_York wall clock (Cboe last_trade_time)
        return datetime.fromisoformat(s).replace(tzinfo=ET)
    except ValueError:
        return None


def is_rth_timestamp(value: Any) -> bool:
    dt = parse_et_timestamp(value)
    if dt is None:
        return False
    return us_equity_options_open(dt)


def session_date_et(when: datetime | None = None) -> date:
    """Calendar date in America/New_York for the given instant."""
    dt = when or now_et()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    else:
        dt = dt.astimezone(ET)
    return dt.date()


def is_current_session_trade(value: Any) -> bool:
    """True if stamp is RTH on today's ET session date (not a prior day close)."""
    dt = parse_et_timestamp(value)
    if dt is None:
        return False
    if not us_equity_options_open(dt):
        return False
    return dt.date() == session_date_et()


def market_status() -> dict[str, Any]:
    dt = now_et()
    open_now = us_equity_options_open(dt)
    return {
        "open": open_now,
        "session": "RTH",
        "tz": "America/New_York",
        "local_time": dt.isoformat(),
        "session_date": session_date_et(dt).isoformat(),
        "hours": "09:30–16:00 ET Mon–Fri",
        "label": "OPEN" if open_now else "CLOSED",
    }


def to_iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return dt.astimezone(timezone.utc).isoformat()
