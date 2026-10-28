"""Servidor web: la página con el diseño clásico + bucle de escaneo.

- GET  /           -> la página (flow/static/index.html)
- GET  /static/*   -> CSS del diseño clásico
- GET  /api/state  -> último estado (veredicto, prints, niveles, book,
  targets, campañas)
- POST /api/scan   -> un scan ahora mismo y devuelve el estado nuevo

Al arrancar, un hilo escanea cada 30 s mientras el mercado USA está abierto
(lun–vie 09:30–16:00 ET; sin calendario de festivos: si es festivo, Cboe no
trae volumen nuevo y el estado simplemente no cambia). Con el mercado
cerrado no escanea: la página muestra el último estado del día.

El refresco es rápido pero el dato es delayed (~15 min de la bolsa): esto
no es cinta en vivo. Un feed tick a tick real sería de pago y no está.
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
from fastapi.staticfiles import StaticFiles

from flow import engine, store
from flow.fetch import TICKER, fetch_chain

ET = ZoneInfo("America/New_York")
SCAN_INTERVAL_S = 30
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="FLOW — MU")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def market_open(now: datetime | None = None) -> bool:
    """True en horario regular USA: lun–vie 09:30–16:00 ET."""
    dt = now or datetime.now(ET)
    if dt.weekday() >= 5:
        return False
    return dtime(9, 30) <= dt.time() <= dtime(16, 0)


def run_scan() -> dict[str, Any]:
    """Una pasada: cadena -> prints -> acumular día -> estado completo."""
    today = store.session_date()
    rows, source = fetch_chain()
    snap_date, prev_volumes, prev_oi = store.load_snapshot()
    if snap_date != today:
        # Día nuevo: sin snapshot de volumen válido, el Δvolumen es el
        # volumen del día. El OI previo SE conserva (ver store.py): el ΔOI
        # del primer scan mide el cambio de posición entre sesiones.
        prev_volumes = {}
    day_date, day_prints = store.load_day_prints()
    if day_date != today:
        day_prints = []
    # Prints de la versión simple (sin convicción/ΔOI/flags): normalizarlos
    # para que la tape y el veredicto no tengan huecos.
    day_prints = [engine.normalize_print(p) for p in day_prints]

    new_prints = engine.detect_prints(rows, prev_volumes, prev_oi) if rows else []
    day_prints = engine.merge_day_prints(day_prints, new_prints, store.now_iso_et())

    # Streak: scans consecutivos con la misma dirección (mismo día).
    prev_state = store.load_state()
    prev_verdict = prev_state.get("verdict") or {}
    verdict = engine.verdict(day_prints)  # streak provisional, se corrige abajo
    if prev_state.get("date") == today and prev_verdict.get("direction") == verdict["direction"]:
        streak = int(prev_verdict.get("streak") or 1) + 1
    else:
        streak = 1
    verdict = engine.verdict(day_prints, streak=streak)

    spot = rows[0]["spot"] if rows else None
    levels_ = engine.levels(rows)
    sweeps_today = sum(1 for p in day_prints if p.get("sweep"))
    verdict["invalidation"] = engine.invalidation(verdict["direction"], levels_, spot)
    state: dict[str, Any] = {
        "date": today,
        "ticker": TICKER,
        "spot": spot,
        "verdict": verdict,
        "prints": day_prints,
        "levels": levels_,
        "book": engine.book_stats(day_prints),
        "targets": engine.targets(day_prints, levels_),
        "campaigns": engine.campaigns(day_prints),
        "sweeps_today": sweeps_today,
        "source": source,
        "market_open": market_open(),
        "updated_at": datetime.now(ET).isoformat(),
        "error": None if rows else "Sin datos: Cboe y Yahoo no respondieron.",
    }
    store.save_state(state)
    store.save_day_prints(today, day_prints)
    if rows:
        store.save_snapshot(
            today,
            {r["contract"]: r["volume"] for r in rows},
            {r["contract"]: r["open_interest"] for r in rows},
        )
    return state


def _loop() -> None:
    """Bucle de fondo: escanea cada 30 s solo con el mercado abierto."""
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
