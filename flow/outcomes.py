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


def label_follow_through(
    underlying: str,
    entry_spot: float,
    entry_ts: datetime,
    spot_now: float,
    bearish: bool,
    atr_cache: dict[str, float | None] | None = None,
) -> tuple[int, float, float]:
    """Shared labeling: ATR floor + SPY beta adjustment.

    Returns (hit, raw_return, adj_return). Used by alert outcomes and by
    campaign outcomes so both measure edge the same way.
    """
    und = underlying.upper()
    raw_ret = spot_now / entry_spot - 1.0
    if atr_cache is None:
        atr_cache = {}
    if und not in atr_cache:
        try:
            atr_cache[und] = get_atr_pct(und)
        except Exception:  # noqa: BLE001 - best effort
            atr_cache[und] = None
    atr_pct = atr_cache[und]
    min_move = 0.5 * atr_pct if atr_pct else 0.004
    adj_ret = raw_ret
    try:
        spy_ret = spy_return_between(entry_ts.date().isoformat(),
                                     datetime.now(timezone.utc).date().isoformat())
        if spy_ret is not None:
            adj_ret = raw_ret - spy_ret  # beta ≈ 1
    except Exception:  # noqa: BLE001 - best effort
        pass
    return _thesis_hit(bearish, adj_ret, min_move), raw_ret, adj_ret


def decay_memory(conn: sqlite3.Connection, half_life_days: float = 30.0) -> None:
    """Exponential decay on the outcome memory (default 30-day half-life).

    Old regimes shouldn't steer today's scores forever: every bucket's
    (hits, n) decays by 0.5 ** (days_since_update / half_life).
    """
    if half_life_days <= 0:
        return
    now = datetime.now(timezone.utc)
    rows = conn.execute("SELECT bucket, hits, n, updated_at FROM inst_memory").fetchall()
    for r in rows:
        ts = _parse_ts(r["updated_at"]) if "updated_at" in r.keys() else None
        if ts is None:
            # legacy rows without timestamp: stamp them now, don't decay yet
            conn.execute(
                "UPDATE inst_memory SET updated_at = ? WHERE bucket = ?",
                (now.isoformat(), r["bucket"]),
            )
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
        if age_days < 1.0:
            continue
        factor = 0.5 ** (age_days / half_life_days)
        conn.execute(
            "UPDATE inst_memory SET hits = ?, n = ?, updated_at = ? WHERE bucket = ?",
            (round(float(r["hits"]) * factor, 2), round(float(r["n"]) * factor, 2),
             now.isoformat(), r["bucket"]),
        )
    conn.commit()


def resolve_outcomes(
    conn: sqlite3.Connection,
    spots: dict[str, float],
    half_life_days: float = 30.0,
) -> int:
    """
    Resolve alerts whose DTE-appropriate horizon has elapsed: ATR-normalized,
    SPY-adjusted follow-through plus OI persistence. Updates inst_memory so
    similar prints score higher or lower next time.
    """
    from flow.db import latest_oi_map

    decay_memory(conn, half_life_days=half_life_days)

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

        bearish = r["call_put"] == "P" or r["inst_label"] == "HEDGE"
        hit, raw_ret, adj_ret = label_follow_through(
            und, entry, ts, spot_now, bearish, atr_cache
        )

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
            INSERT INTO inst_memory (bucket, hits, n, updated_at) VALUES (?, ?, 1, ?)
            ON CONFLICT(bucket) DO UPDATE SET
                hits = hits + excluded.hits,
                n = n + 1,
                updated_at = excluded.updated_at
            """,
            (key, hit, now.isoformat()),
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
