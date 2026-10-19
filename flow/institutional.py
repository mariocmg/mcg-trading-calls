"""
Per-contract institutional filter (heuristic score + structure rules).

Delayed chains cannot prove 'smart money', but we can reject retail noise:
tiny premium, lottery OTM, micro Δvol, and keep size + structure that
looks like desk / hedge / directional accumulation.

NOTE on the ML component: until enough real 1-5d outcome labels accumulate,
the classifier is trained on synthetic data (see _train_synthetic) — its
"probability" is a heuristic score encoding the author's assumptions, not
learned market knowledge. Check model_info() and run `python -m flow evaluate`
for real hit rates.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from flow.positioning import decompose_score
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

FEATURE_NAMES = [
    "log_premium",
    "log_day_premium",
    "vol_oi",
    "log_volume",
    "log_oi",
    "delta_share",
    "abs_otm",
    "dte_norm",
    "near_term",
    "is_put",
    "buyish",
    "lottery",          # cheap deep-OTM
    "hedge_shape",      # short-dated mild-OTM put
    "size_vs_oi",       # delta_vol / max(oi,1)
    "premium_rank",     # 1 = largest in batch
    "opening_risk",     # day vol or delta vol > open interest
    "sweet_spot_dte",   # 14 to 60 DTE institutional catalyst window
    "cluster_flag",     # multi-strike desk execution
]

_MODEL: CalibratedClassifierCV | None = None


def _safe(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        x = float(v)
        if np.isnan(x) or np.isinf(x):
            return default
        return x
    except (TypeError, ValueError):
        return default


def _log1p(x: float) -> float:
    return float(np.log1p(max(0.0, x)))


def contract_features(a: dict[str, Any], premium_rank: float = 0.5, cluster_n: int = 1) -> dict[str, float]:
    prem = max(_safe(a.get("premium_usd")), 0.0)
    price = max(_safe(a.get("price")), 0.0)
    vol = max(_safe(a.get("volume")), 0.0)
    oi = max(_safe(a.get("open_interest")), 0.0)
    dvol = max(_safe(a.get("delta_volume")), 0.0)
    otm = _safe(a.get("otm_pct"))
    dte = max(_safe(a.get("dte"), 7.0), 0.0)
    day_prem = vol * price * 100.0
    vol_oi = (vol / oi) if oi > 0 else 0.0
    near = 1.0 if dte <= 7 else 0.0
    is_put = 1.0 if a.get("call_put") == "P" else 0.0
    buyish = 1.0 if str(a.get("side_bias") or "") in ("buyish", "buy") else 0.0
    
    # Lottery: deep OTM + cheap premium per contract (typical retail)
    lottery = 1.0 if (abs(otm) >= 12 and price < 1.25 and prem < 150_000) else 0.0
    # Hedge: protective put shape (near-term, mild OTM)
    hedge = (
        1.0
        if (is_put > 0 and near > 0 and 0 <= otm <= 6 and dte <= 10)
        else 0.0
    )
    # Opening risk: new positioning being created vs churn/closing
    opening = 1.0 if (oi > 0 and (vol > oi or dvol > oi * 0.5)) else 0.0
    # Institutional sweet spot: 14 to 60 DTE for catalytic moves
    sweet_spot = 1.0 if (14 <= dte <= 60) else 0.0
    cluster_flag = 1.0 if cluster_n >= 2 else 0.0

    return {
        "log_premium": _log1p(prem),
        "log_day_premium": _log1p(day_prem),
        "vol_oi": min(vol_oi, 20.0),
        "log_volume": _log1p(vol),
        "log_oi": _log1p(oi),
        "delta_share": min(dvol / max(vol, 1.0), 1.0),
        "abs_otm": min(abs(otm), 40.0),
        "dte_norm": min(dte, 60.0) / 60.0,
        "near_term": near,
        "is_put": is_put,
        "buyish": buyish,
        "lottery": lottery,
        "hedge_shape": hedge,
        "size_vs_oi": min(dvol / max(oi, 1.0), 5.0),
        "premium_rank": float(premium_rank),
        "opening_risk": opening,
        "sweet_spot_dte": sweet_spot,
        "cluster_flag": cluster_flag,
    }


def _vec(feats: dict[str, float]) -> np.ndarray:
    return np.array([[feats[k] for k in FEATURE_NAMES]], dtype=float)


def _synthetic(rng: np.random.Generator, n: int = 3000) -> tuple[np.ndarray, np.ndarray]:
    """
    Simulate calibrated institutional footprints vs retail noise.
    1 = institutional footprint (accumulation, distribution, or structured hedge).
    0 = retail/speculative noise (cheap out-of-the-money lottos, isolated micro-ticks).
    """
    X, y = [], []
    for _ in range(n):
        label = int(rng.integers(0, 2))
        if label == 1:
            # Institutional: large dollar size, reasonable moneyness, opening bias, sweet-spot DTE
            prem = float(np.exp(rng.normal(11.8, 1.1)))   # ~$130k+ median
            day = prem * rng.uniform(1.2, 5.0)
            vol_oi = abs(rng.normal(1.8, 1.1))
            vol = float(np.exp(rng.normal(6.9, 0.9)))
            oi = max(vol / max(vol_oi, 0.2), 50.0)
            dshare = abs(rng.normal(0.12, 0.08))
            otm = abs(rng.normal(4.2, 3.2))              # ATM to mild OTM
            dte_val = rng.choice([rng.uniform(14, 60), rng.uniform(5, 14)], p=[0.75, 0.25])
            dte_n = min(dte_val / 60.0, 1.0)
            near = 1.0 if dte_val <= 7 else 0.0
            is_put = 1.0 if rng.random() < 0.45 else 0.0
            buyish = 1.0 if rng.random() < 0.65 else 0.0
            lottery = 1.0 if rng.random() < 0.02 else 0.0
            hedge = 1.0 if (is_put and near and otm <= 6) else 0.0
            size_oi = abs(rng.normal(0.25, 0.18))
            rank = abs(rng.normal(0.72, 0.18))
            opening = 1.0 if rng.random() < 0.70 else 0.0
            sweet_spot = 1.0 if (14 <= dte_val <= 60) else 0.0
            cluster = 1.0 if rng.random() < 0.40 else 0.0
        else:
            # Retail noise: smaller premium, higher lottery chance, extreme OTM, 0-3 DTE
            prem = float(np.exp(rng.normal(9.9, 0.95)))   # ~$20k median
            day = prem * rng.uniform(1.0, 2.5)
            vol_oi = abs(rng.normal(0.65, 0.55))
            vol = float(np.exp(rng.normal(5.5, 0.85)))
            oi = max(vol / max(vol_oi, 0.08), 25.0)
            dshare = abs(rng.normal(0.04, 0.04))
            otm = abs(rng.normal(11.5, 6.5))              # frequently deep OTM
            dte_val = rng.choice([rng.uniform(0, 7), rng.uniform(7, 30)], p=[0.70, 0.30])
            dte_n = min(dte_val / 60.0, 1.0)
            near = 1.0 if dte_val <= 7 else 0.0
            is_put = 1.0 if rng.random() < 0.50 else 0.0
            buyish = 1.0 if rng.random() < 0.45 else 0.0
            lottery = 1.0 if (otm >= 12 and rng.random() < 0.65) else 0.0
            hedge = 0.0
            size_oi = abs(rng.normal(0.04, 0.05))
            rank = abs(rng.normal(0.32, 0.20))
            opening = 1.0 if rng.random() < 0.25 else 0.0
            sweet_spot = 1.0 if (14 <= dte_val <= 60) else 0.0
            cluster = 1.0 if rng.random() < 0.10 else 0.0

        row = [
            _log1p(prem),
            _log1p(day),
            min(vol_oi, 20.0),
            _log1p(vol),
            _log1p(oi),
            min(dshare, 1.0),
            min(otm, 40.0),
            min(dte_n, 1.0),
            near,
            is_put,
            buyish,
            lottery,
            hedge,
            min(size_oi, 5.0),
            min(max(rank, 0.0), 1.0),
            opening,
            sweet_spot,
            cluster,
        ]
        X.append(row)
        y.append(label)
    return np.asarray(X, dtype=float), np.asarray(y, dtype=int)


_MODEL_INFO: dict[str, Any] = {"synthetic": True, "source": "none", "n_train": 0}

#: Minimum labeled alerts (with outcome_hit) needed before training on real
#: follow-through instead of the synthetic fallback.
MIN_REAL_LABELS = 80


def default_model_path():
    from pathlib import Path
    import os

    env = os.environ.get("FLOW_MODEL_PATH")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data" / "institutional_model.pkl"


def _build_pipeline() -> Pipeline:
    base = Pipeline(
        [
            ("scaler", StandardScaler()),
            (
                "clf",
                GradientBoostingClassifier(
                    random_state=42,
                    max_depth=3,
                    n_estimators=60,
                    learning_rate=0.07,
                    subsample=0.85,
                ),
            ),
        ]
    )
    try:
        return CalibratedClassifierCV(estimator=base, method="sigmoid", cv=3)
    except TypeError:  # very old scikit-learn
        return CalibratedClassifierCV(base_estimator=base, method="sigmoid", cv=3)


def _train_from_outcomes(conn) -> tuple | None:
    """Train on REAL 1-5d follow-through labels (outcome_hit) when available.

    Returns None when there aren't enough labeled alerts yet, so the caller
    can use the synthetic bootstrap instead.
    """
    rows = conn.execute(
        """
        SELECT premium_usd, price, volume, open_interest, delta_volume,
               otm_pct, dte, call_put, side_bias, spot, cluster_n, outcome_hit
        FROM alerts
        WHERE outcome_hit IS NOT NULL
          AND inst_score IS NOT NULL
          AND premium_usd IS NOT NULL
        ORDER BY id DESC
        LIMIT 5000
        """
    ).fetchall()
    if len(rows) < MIN_REAL_LABELS:
        return None
    X, y = [], []
    for r in rows:
        d = dict(r)
        feats = contract_features(d, premium_rank=0.5, cluster_n=int(d.get("cluster_n") or 1))
        X.append([feats[k] for k in FEATURE_NAMES])
        y.append(int(d["outcome_hit"]))
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=int)
    if len(np.unique(y)) < 2:
        return None  # single class -> can't train a classifier
    cal = _build_pipeline()
    cal.fit(X, y)
    return cal, {"synthetic": False, "source": "outcomes", "n_train": len(y)}


def _train_synthetic() -> tuple:
    """Bootstrap fallback: gaussian blobs hand-written in _synthetic().

    Honest framing: this teaches the model the author's own assumptions, so
    its 'probabilities' are a heuristic score, NOT learned market knowledge.
    Only used until enough real outcome labels exist.
    """
    rng = np.random.default_rng(42)
    X, y = _synthetic(rng, n=2400)
    cal = _build_pipeline()
    cal.fit(X, y)
    return cal, {"synthetic": True, "source": "synthetic", "n_train": len(y)}


def _count_labels(conn) -> int:
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM alerts WHERE outcome_hit IS NOT NULL AND inst_score IS NOT NULL"
        ).fetchone()
        return int(row["n"]) if row else 0
    except Exception:
        return 0


def get_model(conn=None, model_path=None, force_retrain: bool = False) -> CalibratedClassifierCV:
    """Calibrated Gradient Boosting Classifier for P(institutional).

    Load order: (1) persisted joblib model, (2) train on real outcome labels
    if enough exist, (3) synthetic bootstrap. The fitted model is cached and
    persisted to disk so restarts don't retrain from scratch. If the cached
    model is synthetic and enough real labels have accumulated since, it
    upgrades automatically. Use model_info() to see which training data was
    used.
    """
    from pathlib import Path

    global _MODEL, _MODEL_INFO
    path = Path(model_path) if model_path else default_model_path()
    upgrade = (
        _MODEL is not None
        and _MODEL_INFO.get("synthetic")
        and conn is not None
        and _count_labels(conn) >= MIN_REAL_LABELS
    )
    if _MODEL is None or force_retrain or upgrade:
        if path.exists() and not force_retrain and not upgrade:
            import joblib

            _MODEL = joblib.load(path)
            _MODEL_INFO = {
                "synthetic": bool(getattr(_MODEL, "_flow_synthetic", True)),
                "source": "joblib",
                "n_train": -1,
            }
        else:
            trained = _train_from_outcomes(conn) if conn is not None else None
            if trained is not None:
                _MODEL, _MODEL_INFO = trained
            else:
                _MODEL, _MODEL_INFO = _train_synthetic()
            try:
                import joblib

                path.parent.mkdir(parents=True, exist_ok=True)
                _MODEL._flow_synthetic = _MODEL_INFO["synthetic"]
                joblib.dump(_MODEL, path)
            except Exception:
                pass  # persistence is best-effort; the in-memory model still works
    return _MODEL


def model_info() -> dict[str, Any]:
    """Describe the training data behind the current model (honesty helper)."""
    return dict(_MODEL_INFO)


def day_premium(a: dict[str, Any]) -> float:
    return max(_safe(a.get("volume")), 0.0) * max(_safe(a.get("price")), 0.0) * 100.0


def premium_percentile(values: list[float], x: float) -> float:
    if not values:
        return 0.5
    below = sum(1 for v in values if v <= x)
    return below / len(values)


def chain_premiums(rows: list[dict[str, Any]]) -> dict[str, list[float]]:
    """Day premium of every contract with volume, by underlying."""
    out: dict[str, list[float]] = {}
    for r in rows:
        p = day_premium(r)
        if p <= 0:
            continue
        out.setdefault(str(r.get("underlying") or "").upper(), []).append(p)
    return out


def bucket_key(a: dict[str, Any]) -> str:
    dte = _safe(a.get("dte"), 99)
    otm = _safe(a.get("otm_pct"))
    dte_b = "near" if dte <= 7 else ("mid" if dte <= 21 else "long")
    if otm < 0:
        otm_b = "itm"
    elif otm <= 6:
        otm_b = "mild"
    elif otm <= 12:
        otm_b = "mid"
    else:
        otm_b = "far"
    label = "HEDGE" if _is_hedge(a) else "INSTITUTIONAL"
    cp = a.get("call_put") or "?"
    return f"{label}|{cp}|{dte_b}|{otm_b}"


def _is_hedge(a: dict[str, Any]) -> bool:
    otm = _safe(a.get("otm_pct"))
    dte = _safe(a.get("dte"), 99)
    return a.get("call_put") == "P" and dte <= 12 and 0 <= otm <= 6.5


def _classify_intent(a: dict[str, Any], cluster_n: int, opening: bool) -> tuple[str, str]:
    """
    Classify how institutions are positioning:
    Returns (inst_label, inst_subtype)
    inst_label is 'HEDGE' or 'INSTITUTIONAL' (for DB & UI backward compatibility).
    inst_subtype provides specific institutional intent.
    """
    is_put = a.get("call_put") == "P"
    dte = _safe(a.get("dte"), 99)
    otm = _safe(a.get("otm_pct"))
    
    if is_put and dte <= 12 and 0 <= otm <= 6.5:
        return "HEDGE", "PROTECTIVE_PUT"
    
    if cluster_n >= 2:
        return "INSTITUTIONAL", "CALL_SWEEP" if not is_put else "PUT_SWEEP"
    
    if not is_put:
        if 14 <= dte <= 65 and otm <= 10.0:
            return "INSTITUTIONAL", "CALL_ACCUMULATION"
        return "INSTITUTIONAL", "CALL_SPECULATION"
    else:
        if dte >= 14 and otm <= 12.0:
            return "INSTITUTIONAL", "PUT_DISTRIBUTION"
        return "INSTITUTIONAL", "PUT_SPECULATION"


def _cluster_sizes(cands: list[dict[str, Any]]) -> list[int]:
    """Same side + expiry, strikes within 2.5% of spot, count as one desk print."""
    n = len(cands)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        a, b = find(i), find(j)
        if a != b:
            parent[b] = a

    for i in range(n):
        for j in range(i + 1, n):
            a, b = cands[i], cands[j]
            if str(a.get("underlying")) != str(b.get("underlying")):
                continue
            if a.get("call_put") != b.get("call_put"):
                continue
            if a.get("expiry") != b.get("expiry"):
                continue
            spot = _safe(a.get("spot")) or _safe(b.get("spot")) or _safe(a.get("strike"))
            gap = abs(_safe(a.get("strike")) - _safe(b.get("strike")))
            if spot > 0 and gap / spot <= 0.025:
                union(i, j)
    counts: dict[int, int] = {}
    roots = [find(i) for i in range(n)]
    for r in roots:
        counts[r] = counts.get(r, 0) + 1
    return [counts[r] for r in roots]


def _hard_gates(a: dict[str, Any], dust_floor: float) -> tuple[bool, str]:
    """Reject lottery and dust. Size vs the day is a percentile gate, not a fixed $."""
    prem = _safe(a.get("premium_usd"))
    price = _safe(a.get("price"))
    otm = _safe(a.get("otm_pct"))
    dvol = _safe(a.get("delta_volume"))
    day = day_premium(a)
    if prem < dust_floor and day < dust_floor:
        return False, "dust"
    if abs(otm) >= 18 and price < 0.75:
        return False, "lottery"
    if dvol > 0 and dvol < 8 and prem < dust_floor:
        return False, "micro_delta"
    return True, "ok"


def _label(a: dict[str, Any]) -> str:
    return "HEDGE" if _is_hedge(a) else "INSTITUTIONAL"


def _history_adjust(memory: dict[str, tuple[int, int]] | None, key: str) -> float:
    """Shift score from realized hit rate. Neutral until the bucket has evidence."""
    if not memory or key not in memory:
        return 0.0
    hits, n = memory[key]
    if n < 4:
        return 0.0
    rate = (hits + 2.0) / (n + 4.0)
    return (rate - 0.5) * 0.16


def filter_institutional(
    candidates: list[dict[str, Any]],
    *,
    min_score: float = 0.50,
    min_premium_usd: float = 20_000,
    chain: list[dict[str, Any]] | None = None,
    min_percentile: float = 0.82,
    cluster_percentile: float = 0.68,
    memory: dict[str, tuple[int, int]] | None = None,
    conn=None,
    model_path=None,
) -> list[dict[str, Any]]:
    """
    Heuristic institutional filter: a calibrated ML score blended with today's
    premium percentile, strike-cluster sweep bonus, structural breakdown and
    historical realized accuracy.

    Honest note: until enough real outcome labels exist, the ML component is
    trained on synthetic data (see _train_synthetic) — treat its probability
    as a heuristic score, not learned market knowledge. Use model_info() /
    the `evaluate` CLI command to check real hit rates.
    """
    if not candidates:
        return []

    clf = get_model(conn=conn, model_path=model_path)
    premiums = chain_premiums(chain or candidates)
    sizes = _cluster_sizes(candidates)
    ordered_idx = sorted(
        range(len(candidates)),
        key=lambda i: _safe(candidates[i].get("premium_usd")),
        reverse=True,
    )
    rank_of = {i: 1.0 - (pos / max(len(candidates) - 1, 1)) for pos, i in enumerate(ordered_idx)}
    kept: list[dict[str, Any]] = []

    for i, a in enumerate(candidates):
        ok, gate = _hard_gates(a, min_premium_usd)
        if not ok:
            continue
        und = str(a.get("underlying") or "").upper()
        pct = premium_percentile(premiums.get(und, []), day_premium(a))
        csize = sizes[i]
        need_pct = cluster_percentile if csize >= 2 else min_percentile
        if pct < need_pct:
            continue

        feats = contract_features(a, premium_rank=rank_of[i], cluster_n=csize)
        feat_vec = _vec(feats)
        ml_prob = float(clf.predict_proba(feat_vec)[0, 1])

        broken = decompose_score(a, cluster_n=csize)
        hist = _history_adjust(memory, bucket_key(a))
        opening_bonus = 0.06 if broken["opening"] else 0.0
        cluster_bonus = min(0.08, (csize - 1) * 0.04)

        # Multi-factor AI synthesis:
        # 45% Calibrated ML probability
        # 30% Microstructure decomposed score
        # 15% Today's premium percentile relative to whole chain
        # + Opening bonus & Cluster bonus + Historical memory
        blended = (
            0.45 * ml_prob
            + 0.30 * float(broken["score"])
            + 0.15 * float(pct)
            + opening_bonus
            + cluster_bonus
            + hist
        )
        score = max(0.05, min(0.98, blended))

        if score < min_score:
            continue

        inst_label, inst_subtype = _classify_intent(a, csize, bool(broken["opening"]))

        out = dict(a)
        out["inst_score"] = round(score, 3)
        out["inst_ml_prob"] = round(ml_prob, 3)
        out["inst_label"] = inst_label
        out["inst_subtype"] = inst_subtype
        out["cluster_n"] = csize
        out["inst_pct"] = round(pct, 3)
        out["opening"] = 1 if broken["opening"] else 0
        out["score_detail"] = broken
        
        bits = [
            f"score:{score:.2f}",
            f"ml:{ml_prob:.2f}",
            f"sub:{inst_subtype}",
            f"pct:{pct:.2f}",
        ]
        for name, val in broken["parts"].items():
            bits.append(f"{name}:{'na' if val is None else f'{val:.2f}'}")
        if broken["opening"]:
            bits.append("opening")
        if hist:
            bits.append(f"hist:{hist:+.2f}")
        if feats["hedge_shape"] > 0:
            bits.append("hedge_shape")
        if gate != "ok":
            bits.append(f"gate:{gate}")
        out["inst_reason"] = ",".join(bits)
        reason = str(out.get("reason") or "")
        out["reason"] = f"{reason},institutional" if reason else "institutional"
        kept.append(out)

    kept.sort(key=lambda a: (_safe(a.get("inst_score")), _safe(a.get("premium_usd"))), reverse=True)
    return kept
