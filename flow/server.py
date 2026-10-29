"""Servidor web: interfaz estilo BladeMap AI + bucle de escaneo.

- GET  /                  -> la página (flow/static/index.html)
- GET  /static/*          -> CSS
- GET  /api/state         -> último estado del ticker (?ticker=MU)
- POST /api/scan          -> un scan ahora mismo (?ticker=MU) y devuelve el estado nuevo
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from flow import engine, opra, store
from flow.fetch import DEFAULT_TICKERS, TICKER, clean_ticker, fetch_chain

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

ET = ZoneInfo("America/New_York")
SCAN_INTERVAL_S = 30
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="FLOW — Institutional Positioning AI")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_ACTIVE_TICKER = TICKER


def market_open(now: datetime | None = None) -> bool:
    """True en horario regular USA: lun–vie 09:30–16:00 ET."""
    dt = now or datetime.now(ET)
    if dt.weekday() >= 5:
        return False
    return dtime(9, 30) <= dt.time() <= dtime(16, 0)


def _enrich_legacy_state(state: dict[str, Any]) -> dict[str, Any]:
    """Asegura que un state.json guardado antes tenga positioning y campos IA."""
    if not state or "verdict" not in state:
        return state
    spot = state.get("spot")
    prints = [engine.normalize_print(p, spot=spot) for p in (state.get("prints") or [])]
    engine.classify_prints_ai(prints, spot=spot, spot_change_pct=state.get("spot_change_pct"))
    state["prints"] = prints
    v = engine.verdict(prints, streak=int((state.get("verdict") or {}).get("streak") or 1))
    levels_ = state.get("levels")
    v["invalidation"] = engine.invalidation(v["direction"], levels_, spot)
    state["verdict"] = v
    state["book"] = engine.book_stats(prints)
    if "positioning" not in state or not state["positioning"]:
        state["positioning"] = engine.positioning_map(prints, levels_, v, spot)
    state.setdefault("available_tickers", DEFAULT_TICKERS)
    return state


def run_scan(ticker: str | None = None) -> dict[str, Any]:
    """Una pasada: cadena -> prints + Filtro IA -> acumular día -> mapa de posicionamiento."""
    global _ACTIVE_TICKER
    sym = clean_ticker(ticker or _ACTIVE_TICKER)
    _ACTIVE_TICKER = sym

    today = store.session_date()
    rows, source = fetch_chain(sym)
    snap_date, prev_volumes, prev_oi = store.load_snapshot(sym)
    if snap_date != today:
        prev_volumes = {}
    day_date, day_prints = store.load_day_prints(sym)
    if day_date != today:
        day_prints = []

    spot = rows[0]["spot"] if rows else None
    spot_change_pct = rows[0].get("spot_change_pct") if rows else None
    iv30 = rows[0].get("iv30") if rows else None

    rows_by_contract = {r["contract"]: r for r in rows} if rows else {}
    for p in day_prints:
        r = rows_by_contract.get(p.get("contract", ""))
        if r:
            for k in ("delta", "gamma", "iv", "theo"):
                if p.get(k) is None and r.get(k) is not None:
                    p[k] = r[k]
            if p.get("side") == "unknown":
                inferred = engine.infer_side(r.get("bid"), r.get("ask"), r.get("last"), r.get("theo"))
                if inferred != "unknown":
                    p["side"] = inferred
        engine.normalize_print(p, spot=spot)

    # Fuente de prints: OPRA (cinta real) primero; cadena Cboe solo si
    # OPRA falla o viene vacía. Los niveles/GEX siempre salen de la
    # cadena (la cinta no trae el OI completo por strike).
    opra_trades, opra_meta = opra.fetch_opra_trades(sym)
    prints_source: str | None = None
    prints_delay: int | None = None
    if opra_trades:
        if spot is None:
            spot = opra_trades[0].get("underlying_price")
        # En modo OPRA solo cuentan los trades OPRA del día (con
        # unique_id): los prints viejos de cadena del mismo día se
        # dejan fuera para no contar dos veces el mismo flujo.
        kept = [p for p in day_prints if p.get("unique_id")]
        new_opra = opra.prints_for_day(opra_trades, today, spot=spot)
        engine._mark_sweeps(new_opra, spot)  # clusters además del flag OPRA
        day_prints = opra.accumulate_opra_prints(kept, new_opra)
        engine.classify_prints_ai(day_prints, spot=spot, spot_change_pct=spot_change_pct)
        prints_source = "OPRA"
        prints_delay = int((opra_meta or {}).get("delay_minutes") or 60)
    else:
        new_prints = engine.detect_prints(rows, prev_volumes, prev_oi, spot=spot) if rows else []
        day_prints = engine.merge_day_prints(day_prints, new_prints, store.now_iso_et())
        engine.classify_prints_ai(day_prints, spot=spot, spot_change_pct=spot_change_pct)
        prints_source = source if rows else None

    # Streak: scans consecutivos con la misma dirección (mismo día).
    prev_state = store.load_state(sym)
    prev_verdict = prev_state.get("verdict") or {}
    verdict_ = engine.verdict(day_prints)
    if prev_state.get("date") == today and prev_verdict.get("direction") == verdict_["direction"]:
        streak = int(prev_verdict.get("streak") or 1) + 1
    else:
        streak = 1
    verdict_ = engine.verdict(day_prints, streak=streak)

    levels_ = engine.levels(rows)
    sweeps_today = sum(1 for p in day_prints if p.get("sweep"))
    verdict_["invalidation"] = engine.invalidation(verdict_["direction"], levels_, spot)
    pos_map = engine.positioning_map(day_prints, levels_, verdict_, spot)
    llm_note = engine.maybe_llm_synthesis(sym, spot, verdict_, pos_map, levels_, day_prints)
    if llm_note:
        pos_map["llm_synthesis"] = llm_note

    state: dict[str, Any] = {
        "date": today,
        "ticker": sym,
        "available_tickers": DEFAULT_TICKERS,
        "spot": spot,
        "spot_change_pct": spot_change_pct,
        "iv30": iv30,
        "verdict": verdict_,
        "positioning": pos_map,
        "prints": day_prints,
        "levels": levels_,
        "book": engine.book_stats(day_prints),
        "targets": engine.targets(day_prints, levels_),
        "campaigns": engine.campaigns(day_prints),
        "sweeps_today": sweeps_today,
        "source": prints_source or source,
        "prints_source": prints_source,
        "prints_delay_minutes": prints_delay,
        "market_open": market_open(),
        "llm_enabled": bool(os.environ.get("GEMINI_API_KEY")),
        "updated_at": datetime.now(ET).isoformat(),
        "error": None
        if (rows or opra_trades)
        else f"Sin datos para {sym}: OPRA y Cboe/Yahoo no respondieron.",
    }
    store.save_state(state, sym)
    store.save_day_prints(today, day_prints, sym)
    if rows:
        store.save_snapshot(
            today,
            {r["contract"]: r["volume"] for r in rows},
            {r["contract"]: r["open_interest"] for r in rows},
            sym,
        )
    return state


def _loop() -> None:
    """Bucle de fondo: escanea cada 30 s solo con el mercado abierto."""
    while True:
        try:
            if market_open():
                run_scan(_ACTIVE_TICKER)
        except Exception:
            pass
        time.sleep(SCAN_INTERVAL_S)


@app.on_event("startup")
def _startup() -> None:
    if not store.load_state(_ACTIVE_TICKER):
        try:
            run_scan(_ACTIVE_TICKER)
        except Exception:
            pass
    threading.Thread(target=_loop, daemon=True).start()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico")
def favicon() -> Response:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="8" fill="#0b0f19"/>'
        '<path d="M7 22L14 14L19 18L25 9" stroke="#3b82f6" stroke-width="3" fill="none" stroke-linecap="round"/>'
        "</svg>"
    )
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/api/state")
def api_state(ticker: str | None = Query(default=None)) -> JSONResponse:
    global _ACTIVE_TICKER
    sym = clean_ticker(ticker or _ACTIVE_TICKER)
    _ACTIVE_TICKER = sym
    state = store.load_state(sym)
    if not state:
        # Si el usuario cambia a un ticker nuevo sin estado en disco, escaneamos al vuelo
        try:
            state = run_scan(sym)
        except Exception as exc:
            state = {"ticker": sym, "error": f"Error escaneando {sym}: {exc}"}
    else:
        state = _enrich_legacy_state(state)
    return JSONResponse(state)


@app.post("/api/scan")
def api_scan(ticker: str | None = Query(default=None)) -> JSONResponse:
    sym = clean_ticker(ticker or _ACTIVE_TICKER)
    return JSONResponse(run_scan(sym))


def serve() -> None:
    """Levanta el servidor en 127.0.0.1:8787 (lo que abre iniciar.bat)."""
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="warning")
