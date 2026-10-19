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


def resolve_outcomes(
    conn: sqlite3.Connection,
    spots: dict[str, float],
    horizon_days: float = 3.0,
) -> int:
    """Did the desk's campaign work? Resolve campaigns whose accumulation
    ended >= horizon_days ago, using the same ATR/beta-adjusted labeling
    as alert outcomes."""
    from datetime import datetime, timezone

    from flow.outcomes import label_follow_through

    now = datetime.now(timezone.utc)
    rows = conn.execute(
        """
        SELECT * FROM campaigns
        WHERE outcome_hit IS NULL
          AND entry_spot IS NOT NULL AND entry_spot > 0
          AND updated_at <= datetime('now', ?)
        """,
        (f"-{horizon_days} days",),
    ).fetchall()
    if not rows:
        return 0
    atr_cache: dict[str, float | None] = {}
    n = 0
    for r in rows:
        c = dict(r)
        und = str(c.get("underlying") or "").upper()
        spot_now = float(spots.get(und) or 0)
        entry = float(c.get("entry_spot") or 0)
        if spot_now <= 0 or entry <= 0:
            continue
        try:
            started = datetime.fromisoformat(str(c.get("started_at")).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        bearish = str(c.get("direction") or "") == "BEARISH"
        hit, raw_ret, _adj = label_follow_through(
            und, entry, started, spot_now, bearish, atr_cache
        )
        conn.execute(
            """
            UPDATE campaigns
            SET outcome_hit = ?, outcome_return = ?, outcome_at = ?
            WHERE id = ?
            """,
            (hit, round(raw_ret, 5), now.isoformat(), int(c["id"])),
        )
        n += 1
    if n:
        conn.commit()
    return n


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
