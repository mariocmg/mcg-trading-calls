"""Resolve DTE-appropriate follow-through and feed it back into the score.

Labels are designed to measure *edge*, not luck:
- ATR-normalized: a move smaller than 0.5× ATR(14) is noise, not a hit.
- Beta-adjusted: subtract the SPY return over the same window, so a thesis
  that "worked" only because the whole market ripped doesn't count.
- Horizon from DTE: 0-7 DTE → 1d, 8-14 → 2d, 15-30 → 3d, 31-45 → 4d, >45 → 5d.
- OI persistence: institutional accumulation keeps OI growing over the next
  sessions; retail lotto decays. Stored as outcome_oi_growth for future
  training and shown in evaluate/backtest.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from flow.institutional import bucket_key
from flow.market_data import get_atr_pct, spy_return_between


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


def _horizon_days(dte: Any) -> int:
    """Outcome horizon as a function of DTE: short-dated flow is judged fast.

    0-7 DTE → 1d, 8-14 → 2d, 15-30 → 3d, 31-45 → 4d, >45 → 5d.
    Judging 0DTE flow at 5 days (or 45-DTE flow at 1 day) is noise.
    """
    try:
        d = int(dte)
    except (TypeError, ValueError):
        return 3
    if d <= 7:
        return 1
    if d <= 14:
        return 2
    if d <= 30:
        return 3
    if d <= 45:
        return 4
    return 5
def _thesis_hit(bearish: bool, adj_ret: float, min_move: float) -> int:
    """1 if the beta-adjusted move clears the ATR noise floor in thesis direction."""
    if abs(adj_ret) < min_move:
        return 0
    if bearish:
        return 1 if adj_ret < 0 else 0
    return 1 if adj_ret > 0 else 0


def resolve_outcomes(conn: sqlite3.Connection, spots: dict[str, float]) -> int:
    """
    Resolve alerts whose DTE-appropriate horizon has elapsed: ATR-normalized,
    SPY-adjusted follow-through plus OI persistence. Updates inst_memory so
    similar prints score higher or lower next time.
    """
    from flow.db import latest_oi_map

    now = datetime.now(timezone.utc)
    rows = conn.execute(
        """
        SELECT id, underlying, call_put, inst_label, spot, dte, otm_pct,
               created_at, contract, open_interest
        FROM alerts
        WHERE outcome_hit IS NULL AND spot IS NOT NULL AND spot > 0
        """
    ).fetchall()
    if not rows:
        return 0

    oi_now = latest_oi_map(conn)
    atr_cache: dict[str, float | None] = {}
    n = 0
    for r in rows:
        ts = _parse_ts(r["created_at"])
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        horizon = _horizon_days(r["dte"])
        age_days = (now - ts).total_seconds() / 86400.0
        # Resolve once the horizon has elapsed (with a 2-day grace window
        # so weekend/off-hour alerts still resolve).
        if not (horizon <= age_days <= horizon + 2):
            continue
        und = str(r["underlying"] or "").upper()
        spot_now = float(spots.get(und) or 0)
        entry = float(r["spot"] or 0)
        if spot_now <= 0 or entry <= 0:
            continue

        raw_ret = spot_now / entry - 1.0

        # ATR noise floor (0.5× ATR); fallback to the old 0.4% if unavailable.
        if und not in atr_cache:
            try:
                atr_cache[und] = get_atr_pct(und)
            except Exception:  # noqa: BLE001 - best effort
                atr_cache[und] = None
        atr_pct = atr_cache[und]
        min_move = 0.5 * atr_pct if atr_pct else 0.004

        # Beta adjustment vs SPY over the same window.
        adj_ret = raw_ret
        try:
            spy_ret = spy_return_between(ts.date().isoformat(), now.date().isoformat())
            if spy_ret is not None:
                adj_ret = raw_ret - spy_ret  # beta ≈ 1
        except Exception:  # noqa: BLE001 - best effort
            pass

        bearish = r["call_put"] == "P" or r["inst_label"] == "HEDGE"
        hit = _thesis_hit(bearish, adj_ret, min_move)

        # OI persistence: is the position still being built?
        oi_growth = None
        try:
            oi_then = float(r["open_interest"] or 0)
            oi_today = float(oi_now.get(str(r["contract"]), 0))
            if oi_then > 0:
                oi_growth = round(oi_today / oi_then - 1.0, 4)
        except Exception:  # noqa: BLE001
            pass

        conn.execute(
            """
            UPDATE alerts
            SET outcome_hit = ?, outcome_return = ?, outcome_adj_return = ?,
                outcome_oi_growth = ?, outcome_horizon_days = ?, outcome_at = ?
            WHERE id = ?
            """,
            (hit, round(raw_ret, 5), round(adj_ret, 5), oi_growth, horizon,
             now.isoformat(), int(r["id"])),
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
