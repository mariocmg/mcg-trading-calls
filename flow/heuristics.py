from __future__ import annotations

import sqlite3
from statistics import median
from typing import Any

from flow import db as _db
from flow.institutional import filter_institutional
from flow.market_hours import (
    is_current_session_trade,
    parse_et_timestamp,
    to_iso_utc,
    us_equity_options_open,
)


def _tod_score(trade_dt) -> float:
    """Time-of-day score: institutional blocks cluster at the open/close."""
    if trade_dt is None:
        return 0.5
    try:
        mins = trade_dt.hour * 60 + trade_dt.minute - 9 * 60 - 30  # mins since 9:30 ET
    except Exception:
        return 0.5
    if mins < 0 or mins > 390:
        return 0.3
    if mins <= 30 or mins >= 360:
        return 1.0
    if mins <= 60 or mins >= 330:
        return 0.7
    return 0.35


def _is_block(delta_vol: int, contract: str, conn) -> bool:
    """A huge single-scan volume jump vs the contract's own history."""
    if delta_vol < 250:
        return False
    baseline = 50
    if conn is not None:
        try:
            hist = _db.contract_delta_history(conn, contract, 5)
            if hist:
                baseline = max(median(hist), 1)
        except Exception:
            pass
    return delta_vol >= max(500, 3 * baseline)


def _price(row: dict[str, Any] | sqlite3.Row) -> float | None:
    if isinstance(row, dict):
        p = row.get("price")
        if p is not None:
            return float(p)
        for k in ("last", "ask", "bid"):
            if row.get(k) is not None:
                return float(row[k])
        return None
    for k in ("last", "ask", "bid"):
        if row[k] is not None:
            return float(row[k])
    return None


def _otm_pct(call_put: str, strike: float, spot: float | None) -> float | None:
    if spot is None or spot <= 0:
        return None
    if call_put == "C":
        return round((strike - spot) / spot * 100.0, 1)
    return round((spot - strike) / spot * 100.0, 1)


def score_contracts(
    current: list[dict[str, Any]],
    previous: dict[str, sqlite3.Row] | None,
    scan_id: int,
    heuristics: dict[str, Any],
    conn: sqlite3.Connection | None = None,
    model_path=None,
) -> list[dict[str, Any]]:
    """
    Candidate unusual prints, then AI institutional filter.

    Heuristics only propose size/vol anomalies; the institutional model
    decides what reaches the tape (desk-size / hedge vs retail noise).
    """
    min_premium = float(heuristics.get("min_premium_usd", 50_000))
    min_vol_oi = float(heuristics.get("min_vol_oi_ratio", 0.5))
    min_volume = int(heuristics.get("min_volume", 500))
    min_delta = int(heuristics.get("min_delta_volume", 15))
    previous = previous or {}

    market_open = us_equity_options_open()
    candidates: list[dict[str, Any]] = []
    for row in current:
        vol = int(row.get("volume") or 0)
        oi = int(row.get("open_interest") or 0)
        price = _price(row)
        if price is None or price <= 0 or vol <= 0:
            continue

        # Prefer Cboe last_trade_time; only today's RTH session (not prior close).
        ltt = row.get("last_trade_time")
        trade_dt = parse_et_timestamp(ltt) if ltt else None
        if trade_dt is not None:
            if not is_current_session_trade(ltt):
                continue
            event_at = to_iso_utc(trade_dt)
        else:
            # No trade stamp (e.g. Yahoo): only emit while RTH is open.
            if not market_open:
                continue
            event_at = None

        reasons: list[str] = []
        vol_oi = (vol / oi) if oi > 0 else None
        delta_vol = 0
        prev = previous.get(row["contract"])
        if prev is not None:
            prev_vol = int(prev["volume"] or 0)
            delta_vol = max(0, vol - prev_vol)

        premium_delta = delta_vol * price * 100.0
        premium_day = vol * price * 100.0

        if prev is not None and delta_vol <= 0:
            continue
        # Ignore micro ticks that would re-spam unusual_vol_oi every poll
        if prev is not None and delta_vol < min_delta and premium_delta < min_premium:
            continue

        if (
            vol_oi is not None
            and vol >= min_volume
            and vol_oi >= min_vol_oi
            and (prev is None or delta_vol >= min_delta or premium_delta >= min_premium)
        ):
            reasons.append("unusual_vol_oi")
        if delta_vol > 0 and premium_delta >= min_premium:
            reasons.append("premium_delta")
        if prev is None and vol >= min_volume and premium_day >= min_premium:
            reasons.append("large_day_premium")

        if not reasons:
            continue

        spot = row.get("spot")
        try:
            spot_f = float(spot) if spot is not None else None
        except (TypeError, ValueError):
            spot_f = None

        # Microstructure vs previous snapshot
        iv_change = 0.0
        oi_change = 0
        if prev is not None:
            try:
                iv_now = row.get("implied_volatility")
                iv_prev = prev["implied_volatility"]
                if iv_now is not None and iv_prev:
                    iv_change = round(float(iv_now) - float(iv_prev), 4)
            except (TypeError, ValueError, IndexError, KeyError):
                pass
            try:
                oi_change = oi - int(prev["open_interest"] or 0)
            except (TypeError, ValueError, IndexError, KeyError):
                oi_change = 0
        prev_oi = max(int(prev["open_interest"] or 0) if prev is not None else 0, 1)
        oi_change_norm = round(oi_change / prev_oi, 4)

        # Volume vs the contract's own recent history
        rel_vol = 1.0
        if conn is not None:
            try:
                hist = _db.contract_volume_history(conn, str(row["contract"]), 10)
                hist = [v for v in hist if v > 0]
                if hist:
                    rel_vol = round(vol / max(median(hist), 1), 3)
            except Exception:
                pass

        is_block = _is_block(delta_vol, str(row["contract"]), conn)

        candidates.append(
            {
                "scan_id": scan_id,
                "underlying": row["underlying"],
                "contract": row["contract"],
                "call_put": row["call_put"],
                "strike": row["strike"],
                "expiry": row["expiry"],
                "dte": row.get("dte"),
                "price": price,
                "volume": vol,
                "open_interest": oi,
                "delta_volume": delta_vol,
                "premium_usd": premium_delta if delta_vol > 0 else premium_day,
                "vol_oi_ratio": None if vol_oi is None else round(vol_oi, 3),
                "reason": ",".join(reasons),
                "side_bias": row.get("side_bias", "unknown"),
                "spot": spot_f,
                "otm_pct": _otm_pct(row["call_put"], float(row["strike"]), spot_f),
                "event_at": event_at,
                "gamma": row.get("gamma"),
                "bid": row.get("bid"),
                "ask": row.get("ask"),
                "iv_change": iv_change,
                "oi_change": oi_change,
                "oi_change_norm": oi_change_norm,
                "rel_vol": rel_vol,
                "tod_score": _tod_score(trade_dt),
                "is_block": 1 if is_block else 0,
            }
        )

    inst_cfg = heuristics.get("institutional") or {}
    # Allow top-level or nested config
    min_score = float(inst_cfg.get("min_score", heuristics.get("inst_min_score", 0.64)))
    dust = float(inst_cfg.get("min_premium_usd", 20_000))
    alerts = filter_institutional(
        candidates,
        min_score=min_score,
        min_premium_usd=dust,
        chain=current,
        min_percentile=float(inst_cfg.get("min_percentile", 0.82)),
        cluster_percentile=float(inst_cfg.get("cluster_percentile", 0.68)),
        memory=heuristics.get("_memory"),
        conn=conn,
        model_path=model_path,
    )

    # Multi-leg structures: a desk trades packages, not single contracts.
    # Combos add a score bonus and are persisted for the UI.
    from flow.combos import combos_for_alert, detect_combos

    combos = detect_combos(alerts)
    for a in alerts:
        ac = combos_for_alert(a, combos)
        if ac:
            best = max(ac, key=lambda c: c.confidence)
            a["combo_type"] = best.combo_type
            a["inst_score"] = min(0.98, round(float(a["inst_score"]) + 0.05 * best.confidence, 3))
            reason = str(a.get("reason") or "")
            a["reason"] = f"{reason},combo:{best.combo_type}" if reason else f"combo:{best.combo_type}"
        if a.get("is_block"):
            reason = str(a.get("reason") or "")
            a["reason"] = f"{reason},block" if reason else "block"

    alerts.sort(key=lambda a: (a.get("inst_score") or 0, a.get("premium_usd") or 0), reverse=True)
    return alerts


def positioning_bias(alerts: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Sum alert premium by underlying and call/put for a rough weekly bias."""
    out: dict[str, dict[str, float]] = {}
    for a in alerts:
        u = a["underlying"]
        bucket = out.setdefault(u, {"C": 0.0, "P": 0.0})
        bucket[a["call_put"]] = bucket.get(a["call_put"], 0.0) + float(
            a.get("premium_usd") or 0
        )
    return out
