"""Fuente OPRA: cinta de trades de CaymanBot (endpoint interno, no documentado).

GET https://app.caymanbot.com/api/v1/trades?limit=200&symbol=MU

Verificado anónimo el 2026-10-29 (sin sesión ni API key):
- Respuesta: success, delayed=true, data_delay_minutes=60 anónimo
  (16 min con cuenta gratis + sesión; esa cuenta la crea Mario si la
  quiere — aquí NO se usa ninguna credencial ni sesión).
- trades[] con lado REAL (trade_direction + is_at_ask/is_at_bid),
  griegas, IV, OI, volumen, prima, flags (whale/sweep/unusual/multileg),
  agrupación multileg (strategy_*) y unique_id para deduplicar.
- Ventana 24h; la página se recorta a 200 trades (limit mayor no da
  más). Se pide UNA página por scan (los más recientes) y se acumula
  por unique_id: en MU hay ~35 trades/min en hora punta, así que 200
  cubren de sobra un intervalo de 30 s.
- NO es cinta en vivo: el retraso lo aplica el servidor (60 min).
- Endpoint no documentado (CaymanBot dice no tener API pública):
  puede cambiar, limitar o romperse sin aviso. Por eso el servidor
  usa la cadena Cboe como respaldo si OPRA falla o viene vacía.
- Uso personal, frecuencia moderada: un GET cada 30 s con mercado
  abierto, el mismo ritmo que la propia app (que hace polling REST,
  no websocket).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from flow import engine

OPRA_URL = "https://app.caymanbot.com/api/v1/trades"
# UA de navegador: el endpoint sirve a la app web; un UA de librería
# genérico tiene más papeletas de acabar bloqueado por el WAF.
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

ET = ZoneInfo("America/New_York")

# Prima mínima para que un trade OPRA entre en la cinta: la misma que
# la regla de inusualidad de la cadena (evita que la cinta se llene
# de prints pequeños de retail).
MIN_OPRA_PREMIUM_USD = 50_000
# La cinta OPRA acumula eventos individuales (no contratos), así que
# el tope del día es mayor que el de la cadena.
MAX_OPRA_DAY_PRINTS = 500
# Convicción mínima de un trade que la propia OPRA marca como whale:
# se respeta el flag de la fuente en vez de recalcularlo.
WHALE_CONVICTION_FLOOR = 85

_DIRECTION_TO_SIDE = {
    "BTO": "buy",  # buy to open
    "BTC": "buy",  # buy to close
    "STC": "sell",  # sell to close
    "STO": "sell",  # sell to open
}


def _get_json(url: str, timeout: float = 15.0, attempts: int = 2) -> Any:
    """GET JSON con reintento. Lanza la última excepción si todo falla."""
    last: BaseException | None = None
    for i in range(attempts):
        try:
            req = Request(url, headers={"User-Agent": _USER_AGENT})
            with urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except Exception as exc:  # noqa: BLE001 - reintentado, luego propagado
            last = exc
            if i < attempts - 1:
                time.sleep(1.0)
    assert last is not None
    raise last


def fetch_opra_trades(
    symbol: str, limit: int = 200
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Trades OPRA más recientes del ticker + metadatos de retraso.

    Devuelve ([], None) si el endpoint falla o no trae trades: el
    llamador decide el respaldo a Cboe. Nunca lanza.
    """
    try:
        payload = _get_json(f"{OPRA_URL}?limit={limit}&symbol={symbol}")
    except Exception:
        return [], None
    if not isinstance(payload, dict) or not payload.get("success"):
        return [], None
    trades = payload.get("trades") or []
    meta = {
        "delay_minutes": payload.get("data_delay_minutes", 60),
        "free_delay_minutes": payload.get("free_delay_minutes", 16),
        "authenticated": bool((payload.get("subscription") or {}).get("authenticated")),
        "window": payload.get("data_window"),
    }
    return trades, meta


def _trade_side(t: dict[str, Any]) -> str:
    """Lado REAL del trade (no inferido): dirección OPRA, con respaldo
    a la posición en horquilla que la propia OPRA declara."""
    side = _DIRECTION_TO_SIDE.get(str(t.get("trade_direction") or "").upper())
    if side:
        return side
    if t.get("is_at_ask") or t.get("is_ask_side"):
        return "buy"
    if t.get("is_at_bid") or t.get("is_bid_side"):
        return "sell"
    return "unknown"


def _et_date(epoch_s: float | None) -> str | None:
    if not epoch_s:
        return None
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).astimezone(ET).date().isoformat()


def _et_iso(epoch_s: float | None) -> str | None:
    if not epoch_s:
        return None
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).astimezone(ET).isoformat()


def _occ_without_prefix(contract_symbol: str) -> str:
    """'O:MU270115C01100000' -> 'MU270115C01100000' (formato del motor)."""
    return contract_symbol[2:] if contract_symbol.startswith("O:") else contract_symbol


def trade_to_print(
    t: dict[str, Any], spot: float | None = None, today_et: str | None = None
) -> dict[str, Any]:
    """Un trade OPRA -> print con la forma que espera el motor."""
    contract = _occ_without_prefix(str(t.get("contract_symbol") or ""))
    cp = "C" if str(t.get("option_type") or "").lower().startswith("call") else "P"
    strike = float(t.get("strike_price") or 0)
    premium = round(float(t.get("premium_value") or 0))
    size = int(t.get("size") or 0)
    oi = int(t.get("open_interest") or 0)
    side = _trade_side(t)
    sweep = bool(t.get("is_sweep"))
    whale = bool(t.get("is_whale_activity"))
    direction = str(t.get("trade_direction") or "").upper()
    # Lectura Apertura/Cierre REAL (no inferida): BTO/STO abren,
    # BTC/STC cierran. Sin dirección, desconocido.
    open_close = (
        "OPEN" if direction in ("BTO", "STO")
        else "CLOSE" if direction in ("BTC", "STC")
        else "UNKNOWN"
    )
    ratio = (size / oi) if oi > 0 else 1.0
    conv = engine.conviction(premium, ratio, None, side, sweep)
    if whale:
        # La propia OPRA lo marca whale: se respeta su flag.
        conv = max(conv, WHALE_CONVICTION_FLOOR)
    expiry = str(t.get("expiry_date") or "")
    dte: int | None = None
    if expiry and today_et:
        try:
            from datetime import date

            dte = (
                date.fromisoformat(expiry) - date.fromisoformat(today_et)
            ).days
        except ValueError:
            dte = None
    trade_ts = t.get("timestamp")
    seen = _et_iso(trade_ts)
    return {
        "contract": contract,
        "ticker": str(t.get("underlying_symbol") or ""),
        "expiry": expiry,
        "call_put": cp,
        "strike": strike,
        "dte": dte,
        "otm_pct": engine.otm_pct(cp, strike, spot),
        "side": side,  # lado REAL OPRA, no inferido
        "trade_direction": direction or None,
        "open_close": open_close,
        "delta_vol": size,
        "volume": int(t.get("volume") or 0),
        "open_interest": oi,
        "d_oi": None,  # la cinta no da ΔOI (no hay snapshot previo)
        "is_new_position": False,
        "premium": premium,
        "sweep": sweep,
        "delta": t.get("delta"),
        "gamma": t.get("gamma"),
        "vega": t.get("vega"),
        "theta": t.get("theta"),
        "iv": t.get("implied_volatility"),
        "theo": None,
        "conviction": conv,
        "opra_whale": whale,
        "opra_unusual": bool(t.get("is_unusual_volume")),
        "opra_multileg": bool(t.get("is_multileg")),
        "strategy_id": t.get("strategy_id") or None,
        "strategy_type": t.get("strategy_type") or None,
        "strategy_leg_count": t.get("strategy_leg_count") or 0,
        "strategy_net_premium": t.get("strategy_net_premium") or 0,
        "strategy_sentiment": t.get("strategy_sentiment") or None,
        "sentiment": t.get("classification_sentiment") or None,
        "is_hedge": bool(t.get("is_hedge")),
        "unique_id": t.get("unique_id"),
        "trade_ts": trade_ts,
        "first_seen": seen,
        "last_seen": seen,
        "times_seen": 1,
        "source": "OPRA",
    }


def prints_for_day(
    trades: list[dict[str, Any]], session_date: str, spot: float | None = None
) -> list[dict[str, Any]]:
    """Trades del día de sesión (fecha ET) con prima suficiente,
    convertidos a prints y ordenados por prima descendente."""
    prints = []
    for t in trades:
        if _et_date(t.get("timestamp")) != session_date:
            continue
        if float(t.get("premium_value") or 0) < MIN_OPRA_PREMIUM_USD:
            continue
        prints.append(trade_to_print(t, spot=spot, today_et=session_date))
    prints.sort(key=lambda p: p["premium"], reverse=True)
    return prints


def accumulate_opra_prints(
    kept: list[dict[str, Any]], new: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Acumula trades OPRA del día deduplicando por unique_id.

    A diferencia de la cadena (que agrega por contrato), cada trade
    OPRA es un evento propio: el mismo contrato puede tener muchos
    trades distintos en un día y todos cuentan.
    """
    seen = {p.get("unique_id") for p in kept if p.get("unique_id")}
    for p in new:
        uid = p.get("unique_id")
        if uid and uid in seen:
            continue
        if uid:
            seen.add(uid)
        kept.append(p)
    kept.sort(key=lambda p: p["premium"], reverse=True)
    return kept[:MAX_OPRA_DAY_PRINTS]
