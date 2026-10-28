"""Servidor web: una página oscura con 3 bloques + bucle de escaneo.

- GET  /           -> la página (flow/static/index.html)
- GET  /api/state  -> último estado (veredicto, prints, niveles)
- POST /api/scan   -> un scan ahora mismo y devuelve el estado nuevo

Al arrancar, un hilo escanea cada 120 s mientras el mercado USA está abierto
(lun–vie 09:30–16:00 ET; sin calendario de festivos: si es festivo, Cboe no
trae volumen nuevo y el estado simplemente no cambia). Con el mercado cerrado
no escanea: la página muestra el último estado del día.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from flow import engine, store
from flow.fetch import TICKER, fetch_chain

ET = ZoneInfo("America/New_York")
SCAN_INTERVAL_S = 120
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="FLOW — MU")


def market_open(now: datetime | None = None) -> bool:
    """True en horario regular USA: lun–vie 09:30–16:00 ET."""
    dt = now or datetime.now(ET)
    if dt.weekday() >= 5:
        return False
    return dtime(9, 30) <= dt.time() <= dtime(16, 0)


def run_scan() -> dict[str, Any]:
    """Una pasada: cadena -> prints inusuales -> veredicto -> niveles -> JSON."""
    today = store.session_date()
    rows, source = fetch_chain()
    snap_date, prev_volumes = store.load_snapshot()
    if snap_date != today:
        # Día nuevo: sin snapshot válido, el Δvolumen es el volumen del día.
        prev_volumes = {}
    day_date, day_prints = store.load_day_prints()
    if day_date != today:
        day_prints = []

    new_prints = engine.detect_prints(rows, prev_volumes) if rows else []
    # Acumula sin duplicar el mismo contrato dos veces en un mismo scan.
    seen = {p["contract"] for p in day_prints}
    day_prints.extend(p for p in new_prints if p["contract"] not in seen)
    day_prints.sort(key=lambda p: p["premium"], reverse=True)
    day_prints = day_prints[:200]

    verdict = engine.verdict(day_prints)
    spot = rows[0]["spot"] if rows else None
    state: dict[str, Any] = {
        "ticker": TICKER,
        "spot": spot,
        "verdict": verdict,
        "prints": day_prints[: engine.MAX_PRINTS],
        "levels": engine.levels(rows),
        "source": source,
        "market_open": market_open(),
        "updated_at": datetime.now(ET).isoformat(),
        "error": None if rows else "Sin datos: Cboe y Yahoo no respondieron.",
    }
    store.save_state(state)
    store.save_day_prints(today, day_prints)
    if rows:
        store.save_snapshot(today, {r["contract"]: r["volume"] for r in rows})
    return state


def _loop() -> None:
    """Bucle de fondo: escanea cada 2 min solo con el mercado abierto."""
    while True:
        try:
            if market_open():
                run_scan()
        except Exception:
            # Un scan fallido no tumba el servidor: el siguiente lo reintenta.
            pass
        time.sleep(SCAN_INTERVAL_S)


@app.on_event("startup")
def _startup() -> None:
    if not store.load_state():
        # Primer arranque sin estado: un scan inmediato para no servir vacío.
        try:
            run_scan()
        except Exception:
            pass
    threading.Thread(target=_loop, daemon=True).start()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
def api_state() -> JSONResponse:
    state = store.load_state()
    if not state:
        state = {"error": "Sin estado todavía: esperando el primer scan."}
    return JSONResponse(state)


@app.post("/api/scan")
def api_scan() -> JSONResponse:
    return JSONResponse(run_scan())


def serve() -> None:
    """Levanta el servidor en 127.0.0.1:8787 (lo que abre iniciar.bat)."""
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="warning")
