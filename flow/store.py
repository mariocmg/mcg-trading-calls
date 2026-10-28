"""Persistencia mínima: JSON planos en data/. Sin SQLite, sin .pkl.

- snapshot.json: {fecha_sesion_ET, volumes: {contrato: volumen},
  oi: {contrato: open_interest}} del último scan. Sirve para el Δvolumen
  y el ΔOI del siguiente scan. El OI se compara incluso entre días
  distintos: el OI intradía de las cadenas delayed suele ser el del cierre
  anterior, así que el ΔOI del primer scan del día mide el cambio de
  posición de la noche (lo que se abrió/cerró entre sesiones).
- prints.json: {fecha_sesion_ET, prints: [...]} acumulados del día. Cada
  print acumula sus reapariciones en scans posteriores (times_seen,
  first_seen, last_seen, prima y Δvolumen sumados).
- state.json: último estado completo que sirve la web (veredicto, prints,
  niveles, book, targets, campañas, spot, fuente, hora). La web lo lee
  sin esperar a un scan.
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


def load_snapshot() -> tuple[str | None, dict[str, int], dict[str, int]]:
    """(fecha, volumes, oi) del snapshot anterior. Vacío si no existe.

    Compatible con snapshots viejos sin clave "oi" (devuelven {}).
    """
    data = _read("snapshot.json")
    volumes = data.get("volumes") or {}
    oi = data.get("oi") or {}
    return (
        data.get("date"),
        {k: int(v) for k, v in volumes.items()},
        {k: int(v) for k, v in oi.items()},
    )


def save_snapshot(date: str, volumes: dict[str, int], oi: dict[str, int]) -> None:
    _write("snapshot.json", {"date": date, "volumes": volumes, "oi": oi})


def load_day_prints() -> tuple[str | None, list[dict[str, Any]]]:
    data = _read("prints.json")
    return data.get("date"), data.get("prints") or []


def save_day_prints(date: str, prints: list[dict[str, Any]]) -> None:
    _write("prints.json", {"date": date, "prints": prints})


def load_state() -> dict[str, Any]:
    return _read("state.json")


def save_state(state: dict[str, Any]) -> None:
    _write("state.json", state)
