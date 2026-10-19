"""Walk-forward evaluation of the institutional filter.

Uses resolved outcomes (ATR-normalized, SPY-adjusted labels from outcomes.py)
to answer: did high-score alerts actually work, and would a simple ruleset
have made money? No price simulation needed — outcome_return is stored.

Also provides a temporal train/test split: train the institutional model on
the older half of labeled alerts, evaluate precision@k on the newer half.
This is the honest test the synthetic bootstrap never had.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import numpy as np


def walk_forward(
    conn: sqlite3.Connection,
    underlying: str | None = None,
    min_score: float = 0.5,
) -> dict[str, Any]:
    """Aggregate resolved outcomes by score bucket / subtype / month."""
    clauses = ["outcome_hit IS NOT NULL", "inst_score IS NOT NULL", "inst_score >= ?"]
    params: list[Any] = [min_score]
    if underlying:
        clauses.append("underlying = ?")
        params.append(underlying.upper())
    rows = conn.execute(
        f"""
        SELECT inst_score, inst_subtype, inst_label, call_put, premium_usd,
               outcome_hit, outcome_return, outcome_adj_return, outcome_oi_growth,
               is_block, combo_type, created_at
        FROM alerts
        WHERE {' AND '.join(clauses)}
        ORDER BY created_at
        """,
        params,
    ).fetchall()
    items = [dict(r) for r in rows]
    n = len(items)
    if not n:
        return {"n": 0}

    def agg(sel: list[dict]) -> dict[str, Any]:
        k = len(sel)
        if not k:
            return {"n": 0}
        hits = sum(int(i["outcome_hit"]) for i in sel)
        rets = [float(i["outcome_adj_return"] or 0) for i in sel]
        return {
            "n": k,
            "hit_rate": round(hits / k, 3),
            "avg_adj_return": round(float(np.mean(rets)), 4),
            "total_premium": round(sum(float(i["premium_usd"] or 0) for i in sel), 0),
        }

    by_score = []
    for lo, hi, name in [(0.50, 0.65, "0.50-0.65"), (0.65, 0.80, "0.65-0.80"), (0.80, 1.01, "0.80+")]:
        sel = [i for i in items if lo <= float(i["inst_score"]) < hi]
        by_score.append({"bucket": name, **agg(sel)})

    by_subtype = []
    seen: dict[str, list[dict]] = {}
    for i in items:
        seen.setdefault(str(i.get("inst_subtype") or i.get("inst_label") or "?"), []).append(i)
    for key in sorted(seen, key=lambda k: -len(seen[k])):
        by_subtype.append({"subtype": key, **agg(seen[key])})

    by_month: dict[str, list[dict]] = {}
    for i in items:
        by_month.setdefault(str(i.get("created_at") or "")[:7], []).append(i)
    monthly = [{"month": m, **agg(by_month[m])} for m in sorted(by_month)]

    blocks = agg([i for i in items if i.get("is_block")])
    combos = agg([i for i in items if i.get("combo_type")])
    oi_up = agg([i for i in items if (i.get("outcome_oi_growth") or 0) > 0.1])

    return {
        "n": n,
        "overall": agg(items),
        "by_score": by_score,
        "by_subtype": by_subtype,
        "monthly": monthly,
        "blocks": blocks,
        "with_combo": combos,
        "oi_grew": oi_up,
    }


def temporal_split_report(conn: sqlite3.Connection, min_labels: int = 60) -> dict[str, Any]:
    """Train on the older half of labeled alerts, test precision@k on the newer half."""
    rows = conn.execute(
        """
        SELECT premium_usd, price, volume, open_interest, delta_volume,
               otm_pct, dte, call_put, side_bias, side_conf, spot, cluster_n,
               iv_change, rel_vol, oi_change_norm, tod_score,
               outcome_hit, created_at
        FROM alerts
        WHERE outcome_hit IS NOT NULL
          AND inst_score IS NOT NULL
          AND premium_usd IS NOT NULL
        ORDER BY created_at
        """
    ).fetchall()
    items = [dict(r) for r in rows]
    if len(items) < min_labels:
        return {"n": len(items), "ok": False, "reason": f"need ≥{min_labels} labeled alerts"}
    if len({int(i["outcome_hit"]) for i in items}) < 2:
        return {"n": len(items), "ok": False, "reason": "single class"}

    from flow.institutional import FEATURE_NAMES, _build_pipeline, contract_features

    cut = len(items) // 2
    train, test = items[:cut], items[cut:]

    def to_xy(batch):
        X, y = [], []
        for d in batch:
            feats = contract_features(d, premium_rank=0.5, cluster_n=int(d.get("cluster_n") or 1))
            X.append([feats[k] for k in FEATURE_NAMES])
            y.append(int(d["outcome_hit"]))
        return np.asarray(X, dtype=float), np.asarray(y, dtype=int)

    Xtr, ytr = to_xy(train)
    Xte, yte = to_xy(test)
    if len(np.unique(ytr)) < 2 or len(np.unique(yte)) < 2:
        return {"n": len(items), "ok": False, "reason": "single class in split"}
    clf = _build_pipeline()
    clf.fit(Xtr, ytr)
    probs = clf.predict_proba(Xte)[:, 1]
    order = np.argsort(-probs)
    out: dict[str, Any] = {
        "ok": True,
        "n": len(items),
        "n_train": len(train),
        "n_test": len(test),
        "train_hit_rate": round(float(ytr.mean()), 3),
        "test_hit_rate": round(float(yte.mean()), 3),
        "precision_at": {},
    }
    for k in (10, 25, 50):
        kk = min(k, len(test))
        out["precision_at"][f"@{k}"] = round(float(yte[order[:kk]].mean()), 3) if kk else None
    return out


def format_report(rep: dict[str, Any]) -> str:
    if not rep.get("n"):
        return "No hay outcomes resueltos todavía."
    lines = [
        f"Outcomes: {rep['n']}  |  hit-rate global: {rep['overall']['hit_rate']:.1%}  | "
        f"retorno medio ajustado: {rep['overall']['avg_adj_return']:+.2%}",
        "",
        "Por bucket de score:",
    ]
    for b in rep["by_score"]:
        if b["n"]:
            lines.append(f"  {b['bucket']}: {b['hit_rate']:.1%}  ret {b['avg_adj_return']:+.2%}  (n={b['n']})")
    lines.append("")
    lines.append("Por subtipo:")
    for s in rep["by_subtype"][:8]:
        lines.append(f"  {s['subtype']}: {s['hit_rate']:.1%}  (n={s['n']})")
    if rep["blocks"]["n"]:
        lines.append(f"\nBlocks: {rep['blocks']['hit_rate']:.1%} (n={rep['blocks']['n']})")
    if rep["with_combo"]["n"]:
        lines.append(f"Con combo: {rep['with_combo']['hit_rate']:.1%} (n={rep['with_combo']['n']})")
    if rep["oi_grew"]["n"]:
        lines.append(f"OI creció >10%: {rep['oi_grew']['hit_rate']:.1%} (n={rep['oi_grew']['n']})")
    if rep["monthly"]:
        lines.append("\nPor mes:")
        for m in rep["monthly"][-6:]:
            lines.append(f"  {m['month']}: {m['hit_rate']:.1%} (n={m['n']})")
    return "\n".join(lines)
