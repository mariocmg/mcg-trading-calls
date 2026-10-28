"""Persistencia mínima: JSON planos en data/. Sin SQLite, sin .pkl.

- snapshot.json: {fecha_sesion_ET, volumes: {contrato: volumen}} del último scan.
  Sirve para calcular el Δvolumen del siguiente scan.
- prints.json: {fecha_sesion_ET, prints: [...]} acumulados del día.
- state.json: último estado completo que sirve la web (veredicto, prints,
  niveles, spot, fuente, hora). La web lo lee sin esperar a un scan.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ET = ZoneInfo("America/New_York")


def session_date() -> str:
    """Fecha de la sesión en America/New_York (YYYY-MM-DD)."""
    return datetime.now(ET).date().isoformat()


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


def load_snapshot() -> tuple[str | None, dict[str, int]]:
    """(fecha, volumes) del snapshot anterior. Vacío si no existe."""
    data = _read("snapshot.json")
    volumes = data.get("volumes") or {}
    return data.get("date"), {k: int(v) for k, v in volumes.items()}


def save_snapshot(date: str, volumes: dict[str, int]) -> None:
    _write("snapshot.json", {"date": date, "volumes": volumes})


def load_day_prints() -> tuple[str | None, list[dict[str, Any]]]:
    data = _read("prints.json")
    return data.get("date"), data.get("prints") or []


def save_day_prints(date: str, prints: list[dict[str, Any]]) -> None:
    _write("prints.json", {"date": date, "prints": prints})


def load_state() -> dict[str, Any]:
    return _read("state.json")


def save_state(state: dict[str, Any]) -> None:
    _write("state.json", state)
