from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"


def load_config(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or DEFAULT_CONFIG
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    db = Path(cfg.get("database", "data/flow.db"))
    if not db.is_absolute():
        db = ROOT / db
    cfg["database"] = db
    cfg.setdefault("watchlist", ["SPY", "QQQ"])
    cfg.setdefault("data_provider", "cboe")
    cfg.setdefault("max_expiries", 4)
    cfg.setdefault("poll_interval_seconds", 900)
    cfg.setdefault(
        "heuristics",
        {
            "min_premium_usd": 50_000,
            "min_vol_oi_ratio": 0.5,
            "min_volume": 500,
            "min_dte": 0,
            "max_dte": 45,
        },
    )
    return cfg
