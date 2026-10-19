"""Offline evaluation of the institutional filter against real outcome labels.

Uses alerts with outcome_hit resolved (1 = the 1-5d follow-through agreed with
the thesis, 0 = it didn't). Reports precision@k and hit-rate by score bucket
and by subtype, so you can see whether the score actually predicts anything.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def evaluate(conn: sqlite3.Connection, min_score: float = 0.0) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT id, underlying, call_put, inst_score, inst_subtype, inst_label,
               premium_usd, outcome_hit
        FROM alerts
        WHERE outcome_hit IS NOT NULL
          AND inst_score IS NOT NULL
          AND inst_score >= ?
        ORDER BY inst_score DESC
        """,
        (min_score,),
    ).fetchall()
    items = [dict(r) for r in rows]
    n = len(items)
    if not n:
        return {"n": 0}

    hits = [int(i["outcome_hit"]) for i in items]

    def precision_at(k: int) -> float | None:
        k = min(k, n)
        if k <= 0:
            return None
        return sum(hits[:k]) / k

    buckets = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 1.01)]
    by_bucket = []
    for lo, hi in buckets:
        sel = [i for i in items if lo <= float(i["inst_score"]) < hi]
        if sel:
            hr = sum(int(i["outcome_hit"]) for i in sel) / len(sel)
            by_bucket.append(
                {"bucket": f"{lo:.2f}-{hi:.2f}", "n": len(sel), "hit_rate": round(hr, 3)}
            )

    by_subtype: dict[str, dict[str, float]] = {}
    for i in items:
        key = str(i.get("inst_subtype") or i.get("inst_label") or "?")
        b = by_subtype.setdefault(key, {"n": 0, "hits": 0})
        b["n"] += 1
        b["hits"] += int(i["outcome_hit"])
    subtype_rows = [
        {"subtype": k, "n": int(v["n"]), "hit_rate": round(v["hits"] / v["n"], 3)}
        for k, v in sorted(by_subtype.items(), key=lambda kv: -kv[1]["n"])
    ]

    return {
        "n": n,
        "overall_hit_rate": round(sum(hits) / n, 3),
        "precision_at": {f"@{k}": precision_at(k) for k in (10, 25, 50, 100)},
        "by_score_bucket": by_bucket,
        "by_subtype": subtype_rows,
    }


def format_report(rep: dict[str, Any]) -> str:
    if not rep.get("n"):
        return "No hay alertas con outcome resuelto todavía."
    lines = [
        f"Alertas evaluadas: {rep['n']}  |  hit-rate global: {rep['overall_hit_rate']:.1%}",
        "",
        "Precision@k (ordenadas por score):",
    ]
    for k, v in rep["precision_at"].items():
        lines.append(f"  {k:>5}: {'—' if v is None else f'{v:.1%}'}")
    lines.append("")
    lines.append("Hit-rate por bucket de score:")
    for b in rep["by_score_bucket"]:
        lines.append(f"  {b['bucket']}: {b['hit_rate']:.1%}  (n={b['n']})")
    lines.append("")
    lines.append("Hit-rate por subtipo:")
    for s in rep["by_subtype"]:
        lines.append(f"  {s['subtype']}: {s['hit_rate']:.1%}  (n={s['n']})")
    return "\n".join(lines)
