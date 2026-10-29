"""Persistencia mínima: JSON planos en data/. Sin SQLite, sin .pkl.

- snapshot.json (o snapshot_<TICKER>.json): {fecha_sesion_ET, volumes, oi}
  del último scan. Sirve para el Δvolumen y el ΔOI del siguiente scan.
- prints.json (o prints_<TICKER>.json): {fecha_sesion_ET, prints: [...]}
  acumulados del día.
- state.json (o state_<TICKER>.json): último estado completo que sirve la web.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from flow.fetch import TICKER, clean_ticker

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ET = ZoneInfo("America/New_York")


def _file_for(base: str, ticker: str = TICKER) -> str:
    sym = clean_ticker(ticker)
    if sym == TICKER:
        return f"{base}.json"
    return f"{base}_{sym}.json"


def session_date() -> str:
    """Fecha de la sesión en America/New_York (YYYY-MM-DD)."""
    return datetime.now(ET).date().isoformat()


def now_iso_et() -> str:
    """Ahora en America/New_York, ISO (para first_seen / last_seen)."""
    return datetime.now(ET).isoformat()


def _read(name: str) -> dict[str, Any]:
    path = DATA_DIR / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(name: str, payload: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DATA_DIR / (name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    tmp.replace(DATA_DIR / name)


def load_snapshot(ticker: str = TICKER) -> tuple[str | None, dict[str, int], dict[str, int]]:
    """(fecha, volumes, oi) del snapshot anterior. Vacío si no existe."""
    data = _read(_file_for("snapshot", ticker))
    volumes = data.get("volumes") or {}
    oi = data.get("oi") or {}
    return (
        data.get("date"),
        {k: int(v) for k, v in volumes.items()},
        {k: int(v) for k, v in oi.items()},
    )


def save_snapshot(
    date: str,
    volumes: dict[str, int],
    oi: dict[str, int],
    ticker: str = TICKER,
) -> None:
    _write(_file_for("snapshot", ticker), {"date": date, "volumes": volumes, "oi": oi})


def load_day_prints(ticker: str = TICKER) -> tuple[str | None, list[dict[str, Any]]]:
    data = _read(_file_for("prints", ticker))
    return data.get("date"), data.get("prints") or []


def save_day_prints(date: str, prints: list[dict[str, Any]], ticker: str = TICKER) -> None:
    _write(_file_for("prints", ticker), {"date": date, "prints": prints})


def load_state(ticker: str = TICKER) -> dict[str, Any]:
    return _read(_file_for("state", ticker))


def save_state(state: dict[str, Any], ticker: str = TICKER) -> None:
    _write(_file_for("state", ticker), state)
