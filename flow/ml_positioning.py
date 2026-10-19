"""
Institutional positioning from options *structure*, not raw call/put dollars.

Puts > calls with short DTE and mild OTM is usually hedging, not a bearish bet.
Directional lean needs asymmetry in tenor, moneyness and premium concentration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

# Model features only (no raw dollar dominance)
FEATURE_NAMES = [
    "premium_skew",          # (C-P)/(C+P) in [-1,1]
    "count_skew",            # (nC-nP)/(nC+nP)
    "near_term_share",       # DTE <= 7
    "put_near_share",        # fraction of put premium in DTE<=7
    "call_near_share",
    "put_otm_intensity",     # put premium-weighted avg max(otm,0)
    "call_otm_intensity",
    "put_long_otm_share",    # put prem with DTE>=14 and OTM>=5
    "call_long_otm_share",   # call prem with DTE>=14 and OTM>=5
    "itm_put_share",         # puts with otm < 0 (ITM) — often rolls/assignment noise
    "avg_dte",
    "premium_concentration",
    "large_print_put_share", # top prints that are puts
    "vol_spike_score",       # mean delta_vol / max(vol,1) proxy
]

# 0=BEARISH, 1=HEDGING, 2=MIXED, 3=BULLISH
LABELS = {0: "BEARISH", 1: "HEDGING", 2: "MIXED", 3: "BULLISH"}


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


def _prem(a: dict[str, Any]) -> float:
    return max(0.0, _safe(a.get("premium_usd")))


def extract_features(alerts: list[dict[str, Any]]) -> dict[str, float]:
    empty = {k: 0.0 for k in FEATURE_NAMES}
    if not alerts:
        empty.update(
            {
                "_call_prem": 0.0,
                "_put_prem": 0.0,
                "_n": 0.0,
                "_call_n": 0.0,
                "_put_n": 0.0,
                "_structure_note": "no_data",
            }
        )
        return empty

    call = [a for a in alerts if a.get("call_put") == "C"]
    put = [a for a in alerts if a.get("call_put") == "P"]
    call_prem = sum(_prem(a) for a in call)
    put_prem = sum(_prem(a) for a in put)
    total = call_prem + put_prem
    n = len(alerts)

    def wavg(rows: list[dict[str, Any]], key: str) -> float:
        w = sum(_prem(a) for a in rows)
        if w <= 0:
            return 0.0
        return sum(_prem(a) * _safe(a.get(key)) for a in rows) / w

    def share(rows: list[dict[str, Any]], pred) -> float:
        w = sum(_prem(a) for a in rows)
        if w <= 0:
            return 0.0
        return sum(_prem(a) for a in rows if pred(a)) / w

    near = sum(1 for a in alerts if _safe(a.get("dte"), 99) <= 7) / n
    put_near = share(put, lambda a: _safe(a.get("dte"), 99) <= 7)
    call_near = share(call, lambda a: _safe(a.get("dte"), 99) <= 7)

    def otm_intensity(rows: list[dict[str, Any]]) -> float:
        w = sum(_prem(a) for a in rows)
        if w <= 0:
            return 0.0
        return sum(_prem(a) * max(_safe(a.get("otm_pct")), 0.0) for a in rows) / w

    put_long_otm = share(
        put, lambda a: _safe(a.get("dte"), 0) >= 14 and _safe(a.get("otm_pct")) >= 5
    )
    call_long_otm = share(
        call, lambda a: _safe(a.get("dte"), 0) >= 14 and _safe(a.get("otm_pct")) >= 5
    )
    itm_put = share(put, lambda a: _safe(a.get("otm_pct"), 0) < 0)

    premiums = sorted((_prem(a) for a in alerts), reverse=True)
    top_n = max(1, n // 5)
    concentration = sum(premiums[:top_n]) / total if total > 0 else 0.0

    top_alerts = sorted(alerts, key=_prem, reverse=True)[:top_n]
    top_w = sum(_prem(a) for a in top_alerts) or 1.0
    large_put = sum(_prem(a) for a in top_alerts if a.get("call_put") == "P") / top_w

    spikes = []
    for a in alerts:
        vol = max(_safe(a.get("volume")), 1.0)
        spikes.append(_safe(a.get("delta_volume")) / vol)
    vol_spike = float(np.mean(spikes)) if spikes else 0.0

    premium_skew = ((call_prem - put_prem) / total) if total > 0 else 0.0
    count_skew = ((len(call) - len(put)) / n) if n else 0.0
    avg_dte = float(
        np.average(
            [_safe(a.get("dte")) for a in alerts],
            weights=[_prem(a) or 1.0 for a in alerts],
        )
    )

    return {
        "premium_skew": premium_skew,
        "count_skew": count_skew,
        "near_term_share": near,
        "put_near_share": put_near,
        "call_near_share": call_near,
        "put_otm_intensity": otm_intensity(put),
        "call_otm_intensity": otm_intensity(call),
        "put_long_otm_share": put_long_otm,
        "call_long_otm_share": call_long_otm,
        "itm_put_share": itm_put,
        "avg_dte": avg_dte,
        "premium_concentration": concentration,
        "large_print_put_share": large_put,
        "vol_spike_score": vol_spike,
        "_call_prem": call_prem,
        "_put_prem": put_prem,
        "_n": float(n),
        "_call_n": float(len(call)),
        "_put_n": float(len(put)),
        "_avg_call_otm": otm_intensity(call),
        "_avg_put_otm": otm_intensity(put),
    }


def _feature_vector(feats: dict[str, float]) -> np.ndarray:
    return np.array([[feats[k] for k in FEATURE_NAMES]], dtype=float)


def _synthetic_training_set(rng: np.random.Generator, n: int = 1600) -> tuple[np.ndarray, np.ndarray]:
    """
    Regimes encoded in *structure*:
    - BEARISH: longer-dated OTM puts, put skew, not just short-dated protection
    - HEDGING: short DTE puts, mild OTM, balanced/slight put premium
    - MIXED: flat skew, mixed tenors
    - BULLISH: longer-dated OTM calls, call skew
    """
    X, y = [], []
    for _ in range(n):
        label = int(rng.integers(0, 4))
        if label == 3:  # BULLISH
            row = [
                rng.normal(0.35, 0.12),   # premium_skew call+
                rng.normal(0.25, 0.1),
                rng.normal(0.35, 0.12),   # not mostly 0DTE
                rng.normal(0.35, 0.12),   # put_near
                rng.normal(0.35, 0.12),
                rng.normal(2.0, 1.5),     # put otm intensity low
                rng.normal(8.0, 3.0),     # call otm intensity
                rng.normal(0.1, 0.08),    # put long otm
                rng.normal(0.45, 0.15),   # call long otm
                rng.normal(0.1, 0.08),
                rng.normal(18.0, 6.0),
                rng.normal(0.4, 0.1),
                rng.normal(0.3, 0.1),
                rng.normal(0.25, 0.1),
            ]
        elif label == 0:  # BEARISH directional
            row = [
                rng.normal(-0.4, 0.12),
                rng.normal(-0.25, 0.1),
                rng.normal(0.35, 0.12),
                rng.normal(0.35, 0.12),
                rng.normal(0.3, 0.1),
                rng.normal(9.0, 3.0),
                rng.normal(2.0, 1.5),
                rng.normal(0.45, 0.15),
                rng.normal(0.1, 0.08),
                rng.normal(0.15, 0.1),
                rng.normal(20.0, 6.0),
                rng.normal(0.45, 0.1),
                rng.normal(0.65, 0.12),
                rng.normal(0.3, 0.1),
            ]
        elif label == 1:  # HEDGING
            row = [
                rng.normal(-0.08, 0.08),  # slight put, not extreme
                rng.normal(-0.05, 0.08),
                rng.normal(0.75, 0.1),    # very near-term
                rng.normal(0.8, 0.1),     # puts are near-term
                rng.normal(0.7, 0.12),
                rng.normal(3.0, 1.5),     # mild put OTM
                rng.normal(3.0, 1.5),
                rng.normal(0.05, 0.05),   # almost no long OTM puts
                rng.normal(0.08, 0.06),
                rng.normal(0.2, 0.1),
                rng.normal(4.0, 2.0),     # short avg DTE
                rng.normal(0.35, 0.1),
                rng.normal(0.45, 0.12),
                rng.normal(0.2, 0.1),
            ]
        else:  # MIXED
            row = [
                rng.normal(0.0, 0.08),
                rng.normal(0.0, 0.08),
                rng.normal(0.45, 0.12),
                rng.normal(0.45, 0.12),
                rng.normal(0.45, 0.12),
                rng.normal(4.0, 2.0),
                rng.normal(4.0, 2.0),
                rng.normal(0.15, 0.08),
                rng.normal(0.15, 0.08),
                rng.normal(0.15, 0.08),
                rng.normal(12.0, 5.0),
                rng.normal(0.35, 0.1),
                rng.normal(0.4, 0.1),
                rng.normal(0.2, 0.1),
            ]
        X.append(row)
        y.append(label)
    return np.asarray(X, dtype=float), np.asarray(y, dtype=int)


_MODEL: Pipeline | None = None
_MODEL_INFO: dict[str, Any] = {"synthetic": True, "source": "none", "n_train": 0}

#: Minimum daily batches with resolved outcomes before the regime model
#: trains on real labels instead of the synthetic structure prior.
MIN_REAL_BATCHES = 60


def _count_batches(conn) -> int:
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM (
                SELECT underlying, date(created_at) AS d
                FROM alerts
                WHERE outcome_adj_return IS NOT NULL
                GROUP BY underlying, date(created_at)
                HAVING COUNT(*) >= 5
            )
            """
        ).fetchone()
        return int(row["n"]) if row else 0
    except Exception:
        return 0


def _train_from_outcomes(conn) -> tuple | None:
    """Train the regime classifier on REAL daily outcomes.

    Each (underlying, day) batch with ≥5 resolved alerts becomes one training
    row: structure features → the direction that actually worked (ATR/beta-
    adjusted median return), or HEDGING when the tape was put-heavy near-term
    and went nowhere.
    """
    import numpy as np

    rows = conn.execute(
        """
        SELECT underlying, date(created_at) AS d, call_put, premium_usd,
               dte, otm_pct, delta_volume, volume, outcome_adj_return
        FROM alerts
        WHERE outcome_adj_return IS NOT NULL
        ORDER BY underlying, d
        """
    ).fetchall()
    batches: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        batches.setdefault((str(r["underlying"]), str(r["d"])), []).append(dict(r))
    X, y = [], []
    for (und, day), items in batches.items():
        if len(items) < 5:
            continue
        feats = extract_features(items)
        med = float(np.median([float(i["outcome_adj_return"]) for i in items]))
        call_prem = sum(float(i["premium_usd"] or 0) for i in items if i["call_put"] == "C")
        put_prem = sum(float(i["premium_usd"] or 0) for i in items if i["call_put"] == "P")
        tot = call_prem + put_prem
        put_share = put_prem / tot if tot > 0 else 0.5
        if med > 0.008:
            label = 3  # BULLISH
        elif med < -0.008:
            label = 0  # BEARISH
        elif put_share > 0.6 and feats.get("put_near_share", 0) > 0.5:
            label = 1  # HEDGING
        else:
            label = 2  # MIXED
        X.append([feats[k] for k in FEATURE_NAMES])
        y.append(label)
    if len(X) < MIN_REAL_BATCHES or len(set(y)) < 2:
        return None
    pipe = Pipeline(
        [
            ("scaler", StandardScaler()),
            (
                "clf",
                GradientBoostingClassifier(
                    random_state=7, max_depth=3, n_estimators=120, learning_rate=0.08
                ),
            ),
        ]
    )
    pipe.fit(np.asarray(X, dtype=float), np.asarray(y, dtype=int))
    return pipe, {"synthetic": False, "source": "outcomes", "n_train": len(y)}


def default_model_path():
    from pathlib import Path
    import os

    env = os.environ.get("FLOW_POSITIONING_MODEL_PATH")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data" / "positioning_model.pkl"


def get_model(conn=None, model_path=None, force_retrain: bool = False) -> Pipeline:
    """Regime classifier. Persisted with joblib so restarts don't retrain.

    Load order: (1) persisted joblib model, (2) train on real daily outcome
    labels if enough exist, (3) synthetic structure bootstrap. If the cached
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
        and _count_batches(conn) >= MIN_REAL_BATCHES
    )
    if _MODEL is None or force_retrain or upgrade:
        _MODEL = None  # never reuse a stale model below
        if path.exists() and not force_retrain and not upgrade:
            import joblib

            _MODEL = joblib.load(path)
            _MODEL_INFO = {
                "synthetic": bool(getattr(_MODEL, "_flow_synthetic", True)),
                "source": "joblib",
                "n_train": -1,
            }
        if _MODEL is None:
            trained = _train_from_outcomes(conn) if conn is not None else None
            if trained is not None:
                _MODEL, _MODEL_INFO = trained
            else:
                rng = np.random.default_rng(7)
                X, y = _synthetic_training_set(rng)
                pipe = Pipeline(
                    [
                        ("scaler", StandardScaler()),
                        (
                            "clf",
                            GradientBoostingClassifier(
                                random_state=7,
                                max_depth=3,
                                n_estimators=120,
                                learning_rate=0.08,
                            ),
                        ),
                    ]
                )
                pipe.fit(X, y)
                _MODEL = pipe
                _MODEL_INFO = {"synthetic": True, "source": "synthetic", "n_train": len(y)}
            try:
                import joblib

                path.parent.mkdir(parents=True, exist_ok=True)
                _MODEL._flow_synthetic = _MODEL_INFO["synthetic"]
                joblib.dump(_MODEL, path)
            except Exception:
                pass
    return _MODEL


def model_info() -> dict[str, Any]:
    return dict(_MODEL_INFO)


def structural_prior(feats: dict[str, float]) -> dict[str, float]:
    """
    Explicit market-structure prior. Prevents 'slightly more puts => BEAR'.
    Returns unnormalized weights per regime.
    """
    skew = feats["premium_skew"]
    near = feats["near_term_share"]
    put_near = feats["put_near_share"]
    put_long = feats["put_long_otm_share"]
    call_long = feats["call_long_otm_share"]
    put_otm = feats["put_otm_intensity"]
    call_otm = feats["call_otm_intensity"]
    avg_dte = feats["avg_dte"]

    # Balanced book (|skew| < 0.12 ≈ less than ~56/44 split)
    balanced = abs(skew) < 0.12

    w = {"BEARISH": 0.15, "HEDGING": 0.15, "MIXED": 0.4, "BULLISH": 0.15}

    # Classic hedge: near-term, mild OTM puts, not a strong skew
    if near >= 0.6 and put_near >= 0.55 and put_long < 0.2 and avg_dte <= 8:
        w["HEDGING"] += 1.4
        w["BEARISH"] -= 0.5
        if balanced or skew > -0.2:
            w["HEDGING"] += 0.6
            w["MIXED"] += 0.3

    # Directional bear: longer OTM puts + put skew
    if put_long >= 0.25 and skew <= -0.15 and avg_dte >= 10:
        w["BEARISH"] += 1.5
        w["HEDGING"] -= 0.4

    # Directional bull: longer OTM calls + call skew
    if call_long >= 0.25 and skew >= 0.15 and avg_dte >= 10:
        w["BULLISH"] += 1.5

    # Mild call OTM with short dated still can be bullish speculation
    if call_otm >= put_otm + 3 and skew >= 0.1 and near < 0.7:
        w["BULLISH"] += 0.6

    if balanced and near < 0.55:
        w["MIXED"] += 0.8

    # Soft floor
    for k in w:
        w[k] = max(0.05, w[k])
    return w


def _softmax(weights: dict[str, float]) -> dict[str, float]:
    keys = list(weights)
    x = np.array([weights[k] for k in keys], dtype=float)
    x = x - x.max()
    e = np.exp(x)
    p = e / e.sum()
    return {k: float(v) for k, v in zip(keys, p)}


def blend_probs(ml: dict[str, float], prior: dict[str, float], alpha: float = 0.55) -> dict[str, float]:
    """Blend GBM probabilities with structure prior (alpha = weight on ML)."""
    keys = ["BEARISH", "HEDGING", "MIXED", "BULLISH"]
    prior_p = _softmax(prior)
    out = {}
    for k in keys:
        out[k] = alpha * ml.get(k, 0.0) + (1 - alpha) * prior_p.get(k, 0.0)
    s = sum(out.values()) or 1.0
    return {k: out[k] / s for k in keys}


@dataclass
class AnalysisResult:
    underlying: str
    stance: str
    confidence: float
    probabilities: dict[str, float]
    summary: str
    drivers: list[str]
    features: dict[str, float]
    n_alerts: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "stance": self.stance,
            "confidence": self.confidence,
            "probabilities": self.probabilities,
            "summary": self.summary,
            "drivers": self.drivers,
            "features": {k: self.features[k] for k in FEATURE_NAMES},
            "metrics": {
                "call_premium": self.features.get("_call_prem", 0.0),
                "put_premium": self.features.get("_put_prem", 0.0),
                "premium_skew": self.features.get("premium_skew", 0.0),
                "alerts": self.n_alerts,
                "call_alerts": int(self.features.get("_call_n", 0)),
                "put_alerts": int(self.features.get("_put_n", 0)),
                "avg_dte": self.features.get("avg_dte", 0.0),
                "near_term_share": self.features.get("near_term_share", 0.0),
                "put_long_otm_share": self.features.get("put_long_otm_share", 0.0),
                "call_long_otm_share": self.features.get("call_long_otm_share", 0.0),
            },
            "model": (
                "GradientBoosting on flow structure (trained on SYNTHETIC data — "
                "heuristic, not learned market knowledge) + structural prior "
                "(hedge vs directional; not raw put>call)"
            ),
        }


def _drivers(feats: dict[str, float], stance: str, alerts: list[dict[str, Any]] | None = None) -> list[str]:
    skew = feats["premium_skew"]
    call_pct = (0.5 + skew / 2) * 100
    out = [
        f"Skew premium (C−P)/(C+P) = {skew:+.2f} → calls ~{call_pct:.0f}% / puts ~{100-call_pct:.0f}% (no es el veredicto)",
        f"Estructura: near-term {feats['near_term_share']*100:.0f}% · avg DTE {feats['avg_dte']:.1f}",
        f"Puts: near {feats['put_near_share']*100:.0f}% · long-OTM {feats['put_long_otm_share']*100:.0f}% · OTM intensity {feats['put_otm_intensity']:.1f}%",
        f"Calls: near {feats['call_near_share']*100:.0f}% · long-OTM {feats['call_long_otm_share']*100:.0f}% · OTM intensity {feats['call_otm_intensity']:.1f}%",
    ]
    if alerts:
        from collections import Counter
        subtypes = [a.get("inst_subtype") for a in alerts if a.get("inst_subtype")]
        if subtypes:
            counts = Counter(subtypes)
            top_sub = ", ".join(f"{k}: {v}" for k, v in counts.most_common(3))
            out.append(f"Patrones (score heurístico): {top_sub}")

    if stance == "HEDGING":
        out.append("Lectura: puts cortos/ATM-ish = protección de libro, no short institucional claro")
    elif stance == "BEARISH":
        out.append("Lectura: puts más largos/OTM con skew negativo = sesgo bajista directional")
    elif stance == "BULLISH":
        out.append("Lectura: calls más largos/OTM con skew positivo = acumulación alcista")
    else:
        out.append("Lectura: flujo mixto — sin lean direccional dominante")
    return out


def _summary(underlying: str, stance: str, conf: float, feats: dict[str, float]) -> str:
    call_p = feats.get("_call_prem", 0.0)
    put_p = feats.get("_put_prem", 0.0)
    skew = feats.get("premium_skew", 0.0)
    near = feats.get("near_term_share", 0.0) * 100
    if stance == "HEDGING":
        return (
            f"{underlying}: HEDGING (conf {conf:.0%}) — puts cortos dominan la estructura "
            f"(near-term {near:.0f}%, skew {skew:+.2f}). "
            f"No implica short agresivo. Ref: calls ${call_p:,.0f} · puts ${put_p:,.0f}."
        )
    if stance == "BULLISH":
        return (
            f"{underlying}: BULLISH structural (conf {conf:.0%}) — calls con tenor/OTM "
            f"compatibles con acumulación. Ref: calls ${call_p:,.0f} · puts ${put_p:,.0f}."
        )
    if stance == "BEARISH":
        return (
            f"{underlying}: BEARISH structural (conf {conf:.0%}) — puts largos/OTM "
            f"y skew negativo (no solo 'más premium put'). "
            f"Ref: calls ${call_p:,.0f} · puts ${put_p:,.0f}."
        )
    return (
        f"{underlying}: MIXED (conf {conf:.0%}) — skew {skew:+.2f}, near-term {near:.0f}%. "
        f"Sin lean institucional claro. Ref: calls ${call_p:,.0f} · puts ${put_p:,.0f}."
    )


def analyze_alerts(
    alerts: list[dict[str, Any]],
    underlying: str = "MU",
    conn=None,
) -> AnalysisResult:
    scoped = [
        a
        for a in alerts
        if not underlying or str(a.get("underlying", "")).upper() == underlying.upper()
    ]
    feats = extract_features(scoped)
    if not scoped:
        return AnalysisResult(
            underlying=underlying.upper(),
            stance="MIXED",
            confidence=0.0,
            probabilities={"BEARISH": 0.0, "HEDGING": 0.0, "MIXED": 1.0, "BULLISH": 0.0},
            summary=f"{underlying.upper()}: sin alertas aún para analizar.",
            drivers=["Espera al próximo scan LIVE."],
            features=feats,
            n_alerts=0,
        )

    model = get_model(conn=conn)
    raw = model.predict_proba(_feature_vector(feats))[0]
    classes = list(model.named_steps["clf"].classes_)
    ml = {LABELS[int(c)]: float(p) for c, p in zip(classes, raw)}
    for k in LABELS.values():
        ml.setdefault(k, 0.0)

    prior = structural_prior(feats)
    # When book is nearly balanced, trust structure prior more than GBM
    alpha = 0.35 if abs(feats["premium_skew"]) < 0.12 else 0.55
    probs = blend_probs(ml, prior, alpha=alpha)
    stance = max(probs, key=probs.get)
    conf = float(probs[stance])

    return AnalysisResult(
        underlying=underlying.upper(),
        stance=stance,
        confidence=conf,
        probabilities=probs,
        summary=_summary(underlying.upper(), stance, conf, feats),
        drivers=_drivers(feats, stance, scoped),
        features=feats,
        n_alerts=len(scoped),
    )
