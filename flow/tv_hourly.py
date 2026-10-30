"""Tendencia horaria vía sidecar Node + TradingView (pata SMA del Holy Grail).

El escáner es Python y la librería de TradingView es Node, así que la
tendencia se pide a un sidecar (`tv/tv-hourly.mjs`) que imprime un JSON.
Diseño deliberado para no romper nunca un scan:

- Caché por ticker: 15 min, y refresco cuando se completa una hora nueva
  en horario regular (la SMA5 solo cambia al cerrarse una hora).
- Si el sidecar falla (sin Node, sin red, ticker desconocido), se devuelve
  el último valor cacheado marcado como obsoleto, o None si no hay nada.
- Nunca lanza: cualquier excepción se convierte en un dict con error.
- No se llama desde `_enrich_legacy_state`: esa función corre en cada
  lectura de /api/state y un subprocess por lectura sería un disparate.
  La tendencia solo se refresca dentro de run_scan.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CACHE_TTL_S = 900
_SIDECAR = Path(__file__).resolve().parent.parent / "tv" / "tv-hourly.mjs"
_NODE_CMD = os.environ.get("TV_NODE_CMD", "node")
_CACHE: dict[str, dict[str, Any]] = {}


def _latest_completed_hour_start(now: datetime | None = None) -> datetime:
    """Inicio ET de la última hora regular completada (barra etiquetada)."""
    dt = now or datetime.now(ET)
    floored = dt.replace(minute=0, second=0, microsecond=0)
    return floored - timedelta(hours=1)


def _market_hours(now: datetime | None = None) -> bool:
    dt = now or datetime.now(ET)
    return dt.weekday() < 5 and dtime(9, 30) <= dt.time() <= dtime(16, 0)


def _run_sidecar(ticker: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        proc = subprocess.run(
            [_NODE_CMD, str(_SIDECAR), ticker],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        return None, "Node no encontrado (instala Node >= 20 o revisa TV_NODE_CMD)"
    except subprocess.TimeoutExpired:
        return None, "El sidecar TradingView tardó más de 30 s"
    except Exception as exc:  # noqa: BLE001 - nunca debe romper un scan
        return None, f"Error lanzando el sidecar: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return None, detail[-1] if detail else "El sidecar falló sin mensaje"
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return None, "El sidecar devolvió JSON inválido"
    if not isinstance(data, dict) or data.get("sma5") is None:
        return None, "El sidecar devolvió datos incompletos"
    return data, None


def snapshot(ticker: str, spot: float | None = None) -> dict[str, Any]:
    """Tendencia horaria del ticker. Nunca lanza; error -> campo error."""
    sym = (ticker or "").strip().upper()
    now_ts = time.time()
    cached = _CACHE.get(sym)
    data = (cached or {}).get("data")
    stale = False
    error: str | None = None
    now_et = datetime.now(ET)
    need_refresh = data is None
    if data is not None:
        age = now_ts - float(cached.get("_fetched_ts") or 0)
        if age > CACHE_TTL_S:
            need_refresh = True
        elif _market_hours(now_et) and age > 60:
            # Si ya se completó una hora nueva, la SMA5 puede haber cambiado.
            # El margen de 60 s evita llamar en cada scan mientras TV aún
            # no ha publicado la barra recién cerrada.
            last_bar = str(data.get("last_bar_et") or "")
            latest = _latest_completed_hour_start(now_et).strftime("%Y-%m-%d %H:%M")
            if last_bar[:16] != latest:
                need_refresh = True
    if need_refresh:
        fresh, err = _run_sidecar(sym)
        if fresh is not None:
            data = fresh
            _CACHE[sym] = {"data": data, "_fetched_ts": now_ts}
        elif data is not None:
            stale = True
            error = err
        else:
            return {
                "sma5": None, "last_close": None, "last_bar_et": None,
                "position": None, "distance_pct": None, "source": "tradingview",
                "stale": False, "error": err,
            }
    sma5 = float(data["sma5"])
    ref = float(spot) if spot else float(data["last_close"])
    if ref > sma5:
        position = "above"
    elif ref < sma5:
        position = "below"
    else:
        position = "flat"
    return {
        "sma5": sma5,
        "last_close": float(data["last_close"]),
        "last_bar_et": str(data.get("last_bar_et") or ""),
        "position": position,
        "distance_pct": round((ref / sma5 - 1) * 100, 2),
        "source": "tradingview",
        "stale": stale,
        "error": error,
    }
