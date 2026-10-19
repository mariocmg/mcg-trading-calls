from __future__ import annotations

from typing import Any

from flow import db
from flow.config import load_config
from flow.fetcher import fetch_watchlist
from flow.heuristics import positioning_bias, score_contracts
from flow.outcomes import load_memory, resolve_outcomes, spots_from_rows
from flow.positioning import summarize


def run_scan(config_path: str | None = None) -> dict[str, Any]:
    """Fetch chains, persist snapshot + alerts. Shared by CLI and web UI."""
    from pathlib import Path

    cfg = load_config(Path(config_path) if config_path else None)
    h = cfg["heuristics"]
    conn = db.connect(cfg["database"])
    model_path = Path(cfg["database"]).parent / "institutional_model.pkl"

    rows = fetch_watchlist(
        cfg["watchlist"],
        max_expiries=int(cfg["max_expiries"]),
        min_dte=int(h.get("min_dte", 0)),
        max_dte=int(h.get("max_dte", 45)),
        max_moneyness=float(h.get("max_moneyness", 0.12)),
        provider=str(cfg.get("data_provider", "cboe")),
    )
    if not rows:
        conn.close()
        return {"ok": False, "error": "No chain data returned", "contracts": 0, "alerts": 0}

    source = str(rows[0].get("source") or "")
    fallback = str(rows[0].get("fallback_reason") or "")
    scan_id = db.start_scan(conn)
    try:
        n = db.insert_snapshots(conn, scan_id, rows)
        prev_id = db.previous_scan_id(conn, scan_id)
        prev_map = db.load_snapshot_map(conn, prev_id) if prev_id else None

        # Data-quality gate: a stale/glitchy chain is stored (marked) but
        # must never emit alerts — they'd poison the outcome memory.
        from flow.quality import assess_chain_quality

        prev_spot = None
        if prev_map:
            try:
                spots_prev = [float(r["spot"]) for r in prev_map.values() if float(r["spot"] or 0) > 0]
                if spots_prev:
                    spots_prev.sort()
                    prev_spot = spots_prev[len(spots_prev) // 2]
            except (TypeError, ValueError, KeyError):
                pass
        quality_ok, quality_note = assess_chain_quality(rows, prev_spot=prev_spot)
        db.mark_scan_quality(conn, scan_id, quality_ok, quality_note)

        # If volumes jump vs previous snap (Cboe stale → Yahoo), baseline once.
        baseline_only = _provider_reset(rows, prev_map)
        if baseline_only:
            alerts: list[dict[str, Any]] = []
        elif not quality_ok:
            alerts = []
        else:
            half_life = float((h.get("memory") or {}).get("half_life_days", 30))
            spots = spots_from_rows(rows)
            resolve_outcomes(conn, spots, half_life_days=half_life)
            # Campaign outcomes: did the desk's accumulation work?
            from flow import campaigns as _campaigns

            _campaigns.resolve_outcomes(conn, spots)
            h = dict(h)
            h["_memory"] = load_memory(conn)
            alerts = score_contracts(rows, prev_map, scan_id, h, conn=conn, model_path=model_path)
            # Link prints into accumulation campaigns before persisting
            from flow.campaigns import close_stale, link_alerts

            link_alerts(conn, alerts)
            close_stale(conn)
            db.insert_alerts(conn, alerts)
        underlyings = sorted({str(r.get("underlying") or "").upper() for r in rows if r.get("underlying")})
        positionings = {}
        for und in underlyings:
            p_book = summarize(rows, und)
            db.save_positioning(conn, p_book)
            positionings[und] = p_book
            # Strike levels (GEX / walls / max pain / DTE buckets) per scan
            try:
                from flow.levels import compute_positioning_levels

                lv = compute_positioning_levels(rows, und)
                db.save_strike_levels(conn, scan_id, und, lv["levels"])
            except Exception:
                pass  # levels are informational; never fail the scan
    except Exception:
        # Atomic scan: a mid-scan crash must not leave a half-written scan behind.
        _delete_scan(conn, scan_id)
        conn.close()
        raise
    conn.close()

    primary_und = str(rows[0].get("underlying") or "MU").upper()
    book = positionings.get(primary_und, {})

    return {
        "ok": True,
        "scan_id": scan_id,
        "previous_scan_id": prev_id,
        "contracts": n,
        "alerts": len(alerts),
        "bias": positioning_bias(alerts),
        "top_alerts": alerts[:20],
        "source": source,
        "fallback_reason": fallback or None,
        "baseline_only": baseline_only,
        "quality_ok": quality_ok,
        "quality_note": quality_note,
        "positioning": book,
        "all_positioning": positionings,
    }


def _delete_scan(conn, scan_id: int) -> None:
    """Remove every row written for scan_id (crash cleanup)."""
    try:
        conn.execute("DELETE FROM alerts WHERE scan_id = ?", (scan_id,))
        conn.execute("DELETE FROM snapshots WHERE scan_id = ?", (scan_id,))
        conn.execute("DELETE FROM scans WHERE id = ?", (scan_id,))
        conn.commit()
    except Exception:
        pass


def _provider_reset(
    current: list[dict[str, Any]],
    previous: dict[str, Any] | None,
) -> bool:
    """True when chain volumes look incomparable to the prior snapshot."""
    if not previous:
        return False
    n = 0
    jumps = 0
    for r in current:
        prev = previous.get(r["contract"])
        if prev is None:
            continue
        cv = int(r.get("volume") or 0)
        pv = int(prev["volume"] or 0)
        n += 1
        thr = max(80, int(0.3 * max(cv, pv, 1)))
        if abs(cv - pv) > thr:
            jumps += 1
    if n < 25:
        return False
    return (jumps / n) >= 0.35
