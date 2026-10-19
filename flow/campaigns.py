"""Accumulation campaigns: link same-contract prints across scans.

A desk working an order over hours looks like repeated prints on the same
contract. Instead of N independent alerts, campaigns group them into one
accumulation story with growing premium, print count and conviction.

A campaign stays ACTIVE while it gets new prints (within `max_age_hours`);
otherwise it closes. Each alert carries its campaign_id.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from flow import db


def link_alerts(
    conn: sqlite3.Connection,
    alerts: list[dict[str, Any]],
    max_age_hours: float = 6.0,
) -> list[dict[str, Any]]:
    """Attach each alert to its campaign (creating one when needed)."""
    for a in alerts:
        contract = str(a.get("contract") or "")
        if not contract:
            continue
        camp = db.find_open_campaign(conn, contract, max_age_hours=max_age_hours)
        if camp is None:
            cid = db.create_campaign(conn, a)
        else:
            cid = int(camp["id"])
            db.touch_campaign(conn, cid, a)
        a["campaign_id"] = cid
    if alerts:
        conn.commit()
    return alerts


def close_stale(conn: sqlite3.Connection, max_age_hours: float = 6.0) -> int:
    return db.close_stale_campaigns(conn, max_age_hours=max_age_hours)


def active_summary(
    conn: sqlite3.Connection, underlying: str | None = None, limit: int = 10
) -> list[dict[str, Any]]:
    """Top active campaigns, with a conviction readout."""
    camps = db.list_campaigns(conn, underlying=underlying, status="ACTIVE", limit=limit)
    out = []
    for c in camps:
        premium = float(c.get("total_premium") or 0)
        conviction = "BUILDING"
        if c.get("n_prints", 0) >= 5 and premium >= 500_000:
            conviction = "ACCUMULATING"
        elif c.get("n_prints", 0) >= 3:
            conviction = "REPEATING"
        out.append({**c, "conviction": conviction})
    return out
