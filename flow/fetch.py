"""Fetch de la cadena de opciones (MU por defecto, o cualquier ticker). Sin API keys.

Fuente primaria: endpoint público delayed de Cboe
(https://cdn.cboe.com/api/global/delayed_quotes/options/{TICKER}.json).
Si Cboe falla o viene vacío, fallback a Yahoo Finance (yfinance).

Devuelve por fila: contrato (símbolo OCC), vencimiento, call/put, strike,
bid, ask, last, precio (mid o last), volumen del día, open interest, DTE, spot
y —cuando Cboe los incluye— griegas reales (delta, gamma, vega, theta), IV,
precio teórico (theo), tamaño de horquilla (bid_size, ask_size) y cambio del
subyacente.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date
from typing import Any
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

TICKER = "MU"
DEFAULT_TICKERS = [
    "MU",
    "NVDA",
    "TSLA",
    "SPY",
    "QQQ",
    "AMD",
    "AAPL",
    "META",
    "AMZN",
    "MSFT",
    "PLTR",
]

# Ventana de vencimientos que nos interesa: hasta 45 días, strikes a ±12% del spot.
MAX_DTE = 45
MAX_MONEYNESS = 0.12

CBOE_URL = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{TICKER}.json"

# Símbolo OCC: ROOT + AAMMDD + C/P + strike en milésimas (8 dígitos).
OCC_RE = re.compile(
    r"^(?P<root>[A-Z]+)(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<cp>[CP])(?P<strike>\d{8})$"
)

ET = ZoneInfo("America/New_York")


def clean_ticker(ticker: str | None) -> str:
    """Normaliza un ticker (solo letras/puntos/guiones, máx. 8 chars). Fallback a MU."""
    if not ticker:
        return TICKER
    cleaned = re.sub(r"[^A-Z0-9._-]", "", ticker.strip().upper())
    return cleaned[:8] or TICKER


def cboe_url_for(ticker: str) -> str:
    """URL de Cboe para el ticker dado (índices como SPX/VIX llevan guion bajo si aplica)."""
    sym = clean_ticker(ticker)
    if sym == TICKER:
        return CBOE_URL
    return f"https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"


def _f(v: Any) -> float | None:
    """float(v) o None (también para NaN/inf y basura)."""
    try:
        if v is None:
            return None
        x = float(v)
        if x != x or x in (float("inf"), float("-inf")):
            return None
        return x
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int:
    f = _f(v)
    return int(f) if f is not None else 0


def _mid(bid: float | None, ask: float | None, last: float | None) -> float | None:
    """Precio de referencia: mid si hay bid/ask, si no last."""
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return last or ask or bid


def _today_et() -> date:
    from datetime import datetime

    return datetime.now(ET).date()


def parse_occ(symbol: str) -> dict[str, Any] | None:
    """Descompone un símbolo OCC en vencimiento, call/put, strike y DTE."""
    m = OCC_RE.match(symbol.replace(" ", ""))
    if not m:
        return None
    expiry = f"20{m['yy']}-{m['mm']}-{m['dd']}"
    try:
        dte = (date.fromisoformat(expiry) - _today_et()).days
    except ValueError:
        return None
    return {
        "expiry": expiry,
        "call_put": m["cp"],
        "strike": int(m["strike"]) / 1000.0,
        "dte": dte,
    }


def _get_json(url: str, timeout: float = 15.0, attempts: int = 3) -> Any:
    """GET JSON con reintentos. Lanza la última excepción si todo falla."""
    last: BaseException | None = None
    for i in range(attempts):
        try:
            req = Request(url, headers={"User-Agent": "FLOW/1.0 (+uso local)"})
            with urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except Exception as exc:  # noqa: BLE001 - reintentado, luego propagado
            last = exc
            if i < attempts - 1:
                time.sleep(1.5 * (2**i))
    assert last is not None
    raise last


def fetch_cboe(ticker: str = TICKER) -> list[dict[str, Any]]:
    """Cadena delayed de Cboe para el ticker. Lista vacía si no hay datos."""
    sym = clean_ticker(ticker)
    try:
        payload = _get_json(cboe_url_for(sym))
    except Exception:
        return []
    data = payload.get("data") or {}
    spot = _f(data.get("current_price"))
    spot_change_pct = _f(data.get("price_change_percent"))
    iv30 = _f(data.get("iv30"))
    iv30_change = _f(data.get("iv30_change"))
    options = data.get("options") or []
    if not options:
        return []

    rows: list[dict[str, Any]] = []
    for raw in options:
        parsed = parse_occ(str(raw.get("option") or ""))
        if not parsed:
            continue
        if parsed["dte"] < 0 or parsed["dte"] > MAX_DTE:
            continue
        strike = parsed["strike"]
        if spot and spot > 0 and abs(strike - spot) / spot > MAX_MONEYNESS:
            continue
        bid = _f(raw.get("bid"))
        ask = _f(raw.get("ask"))
        last = _f(raw.get("last_trade_price"))
        rows.append(
            {
                "contract": str(raw.get("option") or ""),
                "ticker": sym,
                "expiry": parsed["expiry"],
                "call_put": parsed["call_put"],
                "strike": strike,
                "dte": parsed["dte"],
                "bid": bid,
                "ask": ask,
                "bid_size": _i(raw.get("bid_size")),
                "ask_size": _i(raw.get("ask_size")),
                "last": last,
                "theo": _f(raw.get("theo")),
                "price": _mid(bid, ask, last),
                "volume": _i(raw.get("volume")),
                "open_interest": _i(raw.get("open_interest")),
                "delta": _f(raw.get("delta")),
                "gamma": _f(raw.get("gamma")),
                "vega": _f(raw.get("vega")),
                "theta": _f(raw.get("theta")),
                "iv": _f(raw.get("iv")),
                "last_trade_time": raw.get("last_trade_time"),
                "spot": spot,
                "spot_change_pct": spot_change_pct,
                "iv30": iv30,
                "iv30_change": iv30_change,
                "source": "cboe",
            }
        )
    return rows


def fetch_yahoo(ticker: str = TICKER) -> list[dict[str, Any]]:
    """Fallback: cadena delayed de Yahoo Finance, misma forma de fila.

    Yahoo no da griegas ni theo por contrato, así que esos campos van a None.
    Si Yahoo también falla, lista vacía.
    """
    sym = clean_ticker(ticker)
    try:
        import yfinance as yf
    except ImportError:
        return []
    try:
        tk = yf.Ticker(sym)
        spot = None
        try:
            fast = tk.fast_info
            spot = float(getattr(fast, "last_price", None) or 0) or None
        except Exception:
            spot = None
        expiries = list(tk.options or [])
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for expiry in expiries:
        try:
            dte = (date.fromisoformat(expiry) - _today_et()).days
        except ValueError:
            continue
        if dte < 0 or dte > MAX_DTE:
            continue
        try:
            chain = tk.option_chain(expiry)
        except Exception:
            continue
        for call_put, frame in (("C", chain.calls), ("P", chain.puts)):
            if frame is None or frame.empty:
                continue
            for _, r in frame.iterrows():
                strike = float(r["strike"])
                if spot and spot > 0 and abs(strike - spot) / spot > MAX_MONEYNESS:
                    continue
                bid = _f(r.get("bid"))
                ask = _f(r.get("ask"))
                last = _f(r.get("lastPrice"))
                rows.append(
                    {
                        "contract": str(r.get("contractSymbol") or ""),
                        "ticker": sym,
                        "expiry": expiry,
                        "call_put": call_put,
                        "strike": strike,
                        "dte": dte,
                        "bid": bid,
                        "ask": ask,
                        "bid_size": 0,
                        "ask_size": 0,
                        "last": last,
                        "theo": None,
                        "price": _mid(bid, ask, last),
                        "volume": _i(r.get("volume")),
                        "open_interest": _i(r.get("openInterest")),
                        "delta": None,
                        "gamma": None,
                        "vega": None,
                        "theta": None,
                        "iv": _f(r.get("impliedVolatility")),
                        "last_trade_time": None,
                        "spot": spot,
                        "spot_change_pct": None,
                        "iv30": None,
                        "iv30_change": None,
                        "source": "yahoo",
                    }
                )
    return rows


def fetch_chain(ticker: str = TICKER) -> tuple[list[dict[str, Any]], str]:
    """Cadena del ticker: Cboe primero, Yahoo si Cboe viene vacío. Devuelve (filas, fuente)."""
    sym = clean_ticker(ticker)
    rows = fetch_cboe(sym)
    if rows:
        return rows, "cboe"
    rows = fetch_yahoo(sym)
    return rows, "yahoo" if rows else "none"
