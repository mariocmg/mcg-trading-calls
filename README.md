# Options Flow MVP (delayed)

Scanner de cadenas de opciones con datos **delayed** (Cboe por defecto; Yahoo fallback).  
Pensado para **posicionamiento semanal**, no para tape intradía tipo BladeMap.

Fuentes gratis verificadas: ver [DATA_SOURCES.md](DATA_SOURCES.md).

## Qué hace

1. Poll de cadenas (calls/puts) de un watchlist
2. Guarda snapshots en SQLite
3. Compara con el scan anterior → `Δvolume` × precio × 100 = premium estimado
4. Marca actividad “institucional / unusual” por heurísticas (premium, vol/OI)
5. Resume sesgo call vs put por underlying

**Limitaciones:** sin side real de tape, sin sweeps. El bias buy/sell es débil (last vs bid/ask).
Yahoo a veces devuelve `openInterest=0` y bid/ask vacíos; el MVP se apoya sobre todo en **volumen × precio (premium)** y en **Δvolumen entre scans**.

## Setup

```bash
cd calls
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## UI web

```bash
python -m flow.web
```

Abre http://127.0.0.1:8787 — fondo negro minimal, lean en vivo y tape de alertas.
El servidor escanea solo en bucle (`live_poll_interval_seconds` en config, default ~90s) y la UI se actualiza por SSE.

## CLI

```bash
# Primer scan (baseline; las alertas de Δpremium aparecen desde el 2º)
python -m flow scan

# Ver alertas guardadas
python -m flow alerts

# Resumen call/put últimos 7 días
python -m flow summary

# Poll cada 15 min (config.yaml → poll_interval_seconds)
python -m flow watch
```

Edita `config.yaml` para watchlist y umbrales (`min_premium_usd`, `min_vol_oi_ratio`, etc.).

## Flujo recomendado (semana)

- Lunes–viernes: `python -m flow watch` en horario de mercado (o un `scan` 2–4×/día)
- Domingo/lunes: `python -m flow summary --days 7` para ver lean call/put por ticker

DB: `data/flow.db`
