from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from flow import db
from flow.config import load_config
from flow.market_hours import market_status, us_equity_options_open
from flow.ml_positioning import analyze_alerts
from flow.scan_runner import run_scan
from flow.vector import compute_vector

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop, _watcher_started
    _loop = asyncio.get_running_loop()
    if not _watcher_started:
        _watcher_started = True
        threading.Thread(target=_watcher_loop, daemon=True, name="flow-live").start()
    yield


app = FastAPI(title="FLOW", description="Live delayed options positioning", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_scan_lock = threading.Lock()
_scan_state: dict[str, Any] = {
    "running": False,
    "last": None,
    "error": None,
    "live": True,
    "next_scan_at": None,
    "generation": 0,
}
_subscribers: list[asyncio.Queue] = []
_sub_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_watcher_started = False


def _conn():
    cfg = load_config()
    return db.connect(cfg["database"]), cfg


def _row(r) -> dict[str, Any]:
    return dict(r)


def _publish(event: dict[str, Any]) -> None:
    payload = dict(event)
    with _sub_lock:
        queues = list(_subscribers)
    if _loop is None:
        return
    for q in queues:
        try:
            _loop.call_soon_threadsafe(_safe_put, q, payload)
        except RuntimeError:
            pass  # loop closed; dropping event


def _safe_put(q: asyncio.Queue, payload: dict[str, Any]) -> None:
    """put_nowait that drops the event (or the slow subscriber) instead of
    raising QueueFull inside the event loop."""
    try:
        q.put_nowait(payload)
    except asyncio.QueueFull:
        # Slow consumer: drop this event rather than crashing the publisher.
        # The 15s SSE ping keeps the client alive; it will re-sync on next scan.
        pass


def _do_scan() -> dict[str, Any]:
    with _scan_lock:
        if _scan_state["running"]:
            return {"ok": False, "error": "already running"}
        _scan_state["running"] = True
        _scan_state["error"] = None
        _publish({"type": "scan_start"})
        try:
            result = run_scan()
            _scan_state["last"] = result
            _scan_state["generation"] += 1
            _publish(
                {
                    "type": "scan_done",
                    "result": {
                        "ok": result.get("ok"),
                        "scan_id": result.get("scan_id"),
                        "contracts": result.get("contracts"),
                        "alerts": result.get("alerts"),
                        "error": result.get("error"),
                    },
                    "generation": _scan_state["generation"],
                }
            )
            return result
        except Exception as exc:  # noqa: BLE001
            _scan_state["error"] = str(exc)
            _scan_state["last"] = None
            _publish({"type": "scan_error", "error": str(exc)})
            return {"ok": False, "error": str(exc)}
        finally:
            _scan_state["running"] = False


def _watcher_loop() -> None:
    while True:
        cfg = load_config()
        interval = int(cfg.get("live_poll_interval_seconds") or cfg.get("poll_interval_seconds") or 120)
        interval = max(30, interval)
        allow_closed = bool(cfg.get("scan_when_closed", False))
        if _scan_state["live"]:
            if not allow_closed and not us_equity_options_open():
                _scan_state["next_scan_at"] = None
                _publish({"type": "market_closed", **market_status()})
                time.sleep(60)
                continue
            _scan_state["next_scan_at"] = time.time()
            _do_scan()
            _scan_state["next_scan_at"] = time.time() + interval
            _publish({"type": "idle", "next_scan_at": _scan_state["next_scan_at"], "interval": interval})
            time.sleep(interval)
        else:
            _scan_state["next_scan_at"] = None
            time.sleep(2)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
def status():
    conn, cfg = _conn()
    try:
        info = db.latest_scan_info(conn)
        interval = int(
            cfg.get("live_poll_interval_seconds") or cfg.get("poll_interval_seconds") or 120
        )
        return {
            "watchlist": cfg["watchlist"],
            "scans": db.scan_count(conn),
            "alerts_total": db.alert_count(conn),
            "alerts_7d": db.alert_count(conn, days=7),
            "latest_scan": dict(info) if info else None,
            "scan_running": _scan_state["running"],
            "live": _scan_state["live"],
            "next_scan_at": _scan_state["next_scan_at"],
            "interval": max(30, interval),
            "generation": _scan_state["generation"],
            "last_scan_result": _scan_state["last"],
            "last_scan_error": _scan_state["error"],
            "heuristics": cfg["heuristics"],
            "market": market_status(),
            "scan_when_closed": bool(cfg.get("scan_when_closed", False)),
        }
    finally:
        conn.close()


@app.get("/api/summary")
def summary(days: int = Query(7, ge=1, le=90)):
    conn, cfg = _conn()
    try:
        rows = [_row(r) for r in db.weekly_summary(conn, days=days, min_score=_inst_min_score(cfg))]
        by_u: dict[str, dict[str, float]] = {}
        for r in rows:
            u = r["underlying"]
            by_u.setdefault(u, {"C": 0.0, "P": 0.0, "alerts_C": 0, "alerts_P": 0})
            by_u[u][r["call_put"]] = float(r["total_premium"])
            by_u[u][f"alerts_{r['call_put']}"] = int(r["alert_count"])

        bias = []
        for u, b in sorted(by_u.items()):
            c = b.get("C", 0.0)
            p = b.get("P", 0.0)
            net = c - p
            bias.append(
                {
                    "underlying": u,
                    "call_premium": c,
                    "put_premium": p,
                    "net": net,
                    "lean": "CALLS" if net > 0 else ("PUTS" if net < 0 else "FLAT"),
                    "alerts_C": int(b.get("alerts_C", 0)),
                    "alerts_P": int(b.get("alerts_P", 0)),
                }
            )
        return {"days": days, "rows": rows, "bias": bias}
    finally:
        conn.close()


def _inst_min_score(cfg: dict[str, Any]) -> float:
    """Institutional score threshold — single source of truth: config.yaml."""
    inst = (cfg.get("heuristics") or {}).get("institutional") or {}
    try:
        return float(inst.get("min_score", db.DEFAULT_MIN_SCORE))
    except (TypeError, ValueError):
        return db.DEFAULT_MIN_SCORE


@app.get("/api/alerts")
def alerts(
    limit: int = Query(80, ge=1, le=500),
    underlying: Optional[str] = None,
    call_put: Optional[str] = None,
    days: Optional[int] = Query(None, ge=1, le=90),
    after_id: Optional[int] = Query(None, ge=0),
):
    conn, cfg = _conn()
    try:
        # One shared predicate for both paths (db.recent_alerts), so the
        # incremental feed and the full list can never disagree.
        rows = db.recent_alerts(
            conn,
            limit=limit,
            underlying=underlying,
            call_put=call_put,
            days=days,
            after_id=after_id,
            min_score=_inst_min_score(cfg),
        )
        return {
            "underlyings": db.list_underlyings(conn),
            "alerts": [_row(r) for r in rows],
            "max_id": max((r["id"] for r in rows), default=after_id or 0),
        }
    finally:
        conn.close()


@app.get("/api/analysis")
def analysis(
    underlying: str = Query("MU"),
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(300, ge=20, le=1000),
):
    """ML institutional positioning from recent unusual/options alerts."""
    conn, cfg = _conn()
    try:
        rows = db.recent_alerts(
            conn,
            limit=limit,
            underlying=underlying.upper() if underlying else None,
            days=days,
            min_score=_inst_min_score(cfg),
        )
        result = analyze_alerts([_row(r) for r in rows], underlying=underlying or "MU")
        return result.as_dict()
    finally:
        conn.close()


@app.get("/api/positioning")
def positioning(underlying: str = Query("MU")):
    conn, _ = _conn()
    try:
        row = db.load_positioning(conn, underlying)
        if not row:
            return {"underlying": underlying.upper(), "ok": False}
        return {
            "ok": True,
            "underlying": row["underlying"],
            "updated_at": row["updated_at"],
            "call_premium": row["call_premium"],
            "put_premium": row["put_premium"],
            "net_premium": row["net_premium"],
            "call_avg_strike": row.get("call_avg_strike"),
            "put_avg_strike": row.get("put_avg_strike"),
            "avg_strike": row.get("avg_strike"),
            "gex": {
                "ok": bool(row["gex_ok"]),
                "strike": row["gex_strike"],
                "gex": row["gex_value"],
                "side": row["gex_side"],
            },
        }
    finally:
        conn.close()


@app.get("/api/vector")
def vector(
    underlying: str = Query("MU"),
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(400, ge=20, le=1500),
):
    """BladeMap-style Vector Board row for the selected symbol."""
    conn, cfg = _conn()
    try:
        rows = db.recent_alerts(
            conn,
            limit=limit,
            underlying=underlying.upper() if underlying else None,
            days=days,
            min_score=_inst_min_score(cfg),
        )
        return compute_vector([_row(r) for r in rows], underlying=underlying or "MU")
    finally:
        conn.close()


@app.post("/api/scan")
def trigger_scan():
    if _scan_state["running"]:
        raise HTTPException(status_code=409, detail="Scan already running")

    threading.Thread(target=_do_scan, daemon=True).start()
    return {"ok": True, "message": "Scan started"}


@app.post("/api/live")
def set_live(enabled: bool = Query(True)):
    _scan_state["live"] = bool(enabled)
    _publish({"type": "live", "live": _scan_state["live"]})
    return {"live": _scan_state["live"]}


@app.get("/api/live/stream")
async def live_stream():
    queue: asyncio.Queue = asyncio.Queue(maxsize=32)
    with _sub_lock:
        _subscribers.append(queue)

    async def gen():
        try:
            yield f"data: {json.dumps({'type': 'hello', 'live': _scan_state['live'], 'generation': _scan_state['generation']})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'ping', 'running': _scan_state['running'], 'next_scan_at': _scan_state['next_scan_at']})}\n\n"
        finally:
            with _sub_lock:
                if queue in _subscribers:
                    _subscribers.remove(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def main():
    import uvicorn

    uvicorn.run("flow.web:app", host="127.0.0.1", port=8787, reload=False)


if __name__ == "__main__":
    main()
