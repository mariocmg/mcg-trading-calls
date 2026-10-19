"""Resolve 1–5 day price follow-through and feed it back into the AI score."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from flow.institutional import bucket_key


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    s = str(value).strip()
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def load_memory(conn: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    rows = conn.execute("SELECT bucket, hits, n FROM inst_memory").fetchall()
    return {str(r["bucket"]): (int(r["hits"]), int(r["n"])) for r in rows}


def resolve_outcomes(conn: sqlite3.Connection, spots: dict[str, float]) -> int:
    """
    Alerts aged 1–5 days: call thesis hits if spot rose, put/hedge if it fell.
    Updates inst_memory so similar prints score higher or lower next time.
    """
    now = datetime.now(timezone.utc)
    lo = now - timedelta(days=6)
    hi = now - timedelta(days=1)
    rows = conn.execute(
        """
        SELECT id, underlying, call_put, inst_label, spot, dte, otm_pct, created_at
        FROM alerts
        WHERE outcome_hit IS NULL AND spot IS NOT NULL AND spot > 0
        """
    ).fetchall()
    n = 0
    for r in rows:
        ts = _parse_ts(r["created_at"])
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if not (lo <= ts <= hi):
            continue
        und = str(r["underlying"] or "").upper()
        spot_now = float(spots.get(und) or 0)
        entry = float(r["spot"] or 0)
        if spot_now <= 0 or entry <= 0:
            continue
        ret = spot_now / entry - 1.0
        bearish = r["call_put"] == "P" or r["inst_label"] == "HEDGE"
        if abs(ret) < 0.004:
            hit = 0
        elif bearish:
            hit = 1 if ret < 0 else 0
        else:
            hit = 1 if ret > 0 else 0
        conn.execute(
            """
            UPDATE alerts
            SET outcome_hit = ?, outcome_return = ?, outcome_at = ?
            WHERE id = ?
            """,
            (hit, round(ret, 5), now.isoformat(), int(r["id"])),
        )
        key = bucket_key(
            {
                "call_put": r["call_put"],
                "dte": r["dte"],
                "otm_pct": r["otm_pct"],
                "inst_label": r["inst_label"],
            }
        )
        conn.execute(
            """
            INSERT INTO inst_memory (bucket, hits, n) VALUES (?, ?, 1)
            ON CONFLICT(bucket) DO UPDATE SET
                hits = hits + excluded.hits,
                n = n + 1
            """,
            (key, hit),
        )
        n += 1
    if n:
        conn.commit()
    return n


def spots_from_rows(rows: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in rows:
        und = str(r.get("underlying") or "").upper()
        try:
            spot = float(r.get("spot") or 0)
        except (TypeError, ValueError):
            continue
        if und and spot > 0:
            out[und] = spot
    return out
