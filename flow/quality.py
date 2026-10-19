"""Data-quality gate for chain snapshots.

The Cboe delayed feed (and Yahoo) occasionally serves stale chains, empty
greeks or glitch prints. A bad scan must never emit alerts — it would poison
the outcome memory. This gate runs before heuristics; failing scans are still
stored (marked) but produce no alerts.
"""

from __future__ import annotations

from typing import Any


def _safe(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        x = float(v)
        if x != x or x in (float("inf"), float("-inf")):
            return default
        return x
    except (TypeError, ValueError):
        return default


def assess_chain_quality(
    rows: list[dict[str, Any]],
    prev_spot: float | None = None,
) -> tuple[bool, str]:
    """(ok, note). ok=False means: store the scan, emit no alerts."""
    if not rows:
        return False, "cadena vacía"
    n = len(rows)

    # 1. Spot sanity: median spot vs previous scan (glitch prints)
    spots = sorted(_safe(r.get("spot")) for r in rows if _safe(r.get("spot")) > 0)
    if not spots:
        return False, "sin spot en la cadena"
    med_spot = spots[len(spots) // 2]
    if prev_spot and prev_spot > 0:
        jump = abs(med_spot / prev_spot - 1.0)
        if jump > 0.08:
            return False, f"salto de spot {jump:.1%} vs scan anterior (glitch?)"

    # 2. Stale quotes: how many rows have no usable price?
    no_price = sum(1 for r in rows if _safe(r.get("price")) <= 0 and _safe(r.get("last")) <= 0)
    if no_price / n > 0.5:
        return False, f"{no_price}/{n} contratos sin precio"

    # 3. Volume anomalies: negative or absurd single prints
    bad_vol = sum(1 for r in rows if _safe(r.get("volume")) < 0)
    if bad_vol:
        return False, f"{bad_vol} volúmenes negativos"

    # 4. Bid/ask sanity on rows that have them
    bad_quotes = sum(
        1 for r in rows
        if r.get("bid") is not None and r.get("ask") is not None
        and _safe(r.get("ask")) > 0 and _safe(r.get("ask")) < _safe(r.get("bid"))
    )
    if bad_quotes / max(n, 1) > 0.2:
        return False, f"{bad_quotes}/{n} horquillas invertidas"

    # 5. Gamma coverage (informational only — doesn't fail the scan)
    with_gamma = sum(1 for r in rows if r.get("gamma") is not None)
    note = f"ok ({n} contratos, gamma {with_gamma}/{n})"
    return True, note
