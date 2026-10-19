# Options Flow MVP (delayed)

Scanner de cadenas de opciones con datos **delayed** (Cboe por defecto; Yahoo fallback).  
Pensado para **posicionamiento semanal**, no para tape intradía tipo BladeMap.

Fuentes gratis verificadas: ver [DATA_SOURCES.md](DATA_SOURCES.md).

## Qué hace

1. Poll de cadenas (calls/puts) de un watchlist
2. Guarda snapshots en SQLite
3. Compara con el scan anterior → `Δvolume` × precio × 100 = premium estimado
4. Marca actividad “institucional / unusual” con un modelo GradientBoosting (22 features:
   tamaño, vol/OI, urgencia, ΔIV, volumen relativo al histórico del contrato, hora del día…)
   entrenado con outcomes reales 1–5d cuando hay ≥80 (si no, bootstrap sintético — la UI/CLI lo avisa)
5. **Combos**: detecta risk reversals, spreads, collars y strangles entre prints del mismo vencimiento
6. **Blocks**: saltos de volumen ≥3× el histórico del contrato en un solo scan
7. **Niveles**: muros de OI (call = resistencia, put = soporte), gamma flip, max pain por vencimiento,
   y buckets de DTE (dónde se posicionan en el tiempo)
8. Resume sesgo call vs put por underlying

**Limitaciones:** sin side real de tape, sin sweeps. El bias buy/sell es débil (last vs bid/ask).
Yahoo a veces devuelve `openInterest=0` y bid/ask vacíos; el MVP se apoya sobre todo en **volumen × precio (premium)** y en **Δvolumen entre scans**.

## Setup

```bash
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

# Hit-rate real del filtro contra outcomes 1-5d resueltos (incluye split temporal)
python -m flow evaluate

# Walk-forward por bucket de score / subtipo / mes + test de blocks y combos
python -m flow backtest

# Muros de OI, gamma flip, max pain y posicionamiento por DTE
python -m flow levels

# Campañas de acumulación activas
python -m flow campaigns

# Borrar snapshots antiguos (pregunta antes; --yes para no preguntar)
python -m flow prune --keep-days 14
```

Edita `config.yaml` para watchlist y umbrales (`min_premium_usd`, `min_vol_oi_ratio`, etc.).

## Tests

```bash
python -m pytest
```

## Notas sobre el "score"

El score institucional combina un clasificador calibrado (22 features) con percentiles
de prima, clusters de strikes, memoria de aciertos, bonus por combos multi-leg y flags
de block. Hasta acumular suficientes outcomes reales (1–5 días), el clasificador se
entrena con datos sintéticos: el score es **heurístico**, no conocimiento aprendido del
mercado. `python -m flow evaluate` muestra precision@k, hit-rate por bucket y un split
temporal (entrena en la mitad antigua, testea en la nueva) con etiquetas reales.

Las etiquetas de outcome están diseñadas para medir edge, no suerte: el movimiento se
**ajusta por beta** (se resta el retorno del SPY en la misma ventana), se exige superar
un **suelo de ruido de 0,5× ATR(14)**, el **horizonte depende del DTE**
(1d para 0–7 DTE … 5d para >45) y se guarda si el **OI siguió creciendo** (acumulación
real vs lotto que decae). `python -m flow backtest` agrega el walk-forward por score,
subtipo, mes, blocks y combos.

La **confianza del Vector Board** (etapa = nº de checks de 5) se **calibra empíricamente**
con el hit-rate de setups direccionales pasados cuando hay ≥20; si no, usa la heurística
y lo dice. El **régimen gamma** (long/short según GEX neta) condiciona la tesis: en
long-gamma la tendencia pesa la mitad (los muros sujetan, reversión), en short-gamma
pesa ×1,3 (las rupturas aceleran).

El **side bias es continuo con incertidumbre**: cada print lleva `side_conf` (0–1) según
lo estrecho de la horquilla y lo pegado que esté al bid/ask; el modelo lo usa como
feature con signo (`side_aggr`) en vez de un binario.

Los prints del mismo contrato se enlazan en **campañas de acumulación** (tabla
`campaigns`, comando `python -m flow campaigns`, `/api/campaigns`): prima total,
nº de prints y convicción (BUILDING → REPEATING → ACCUMULATING). Cada campaña
también se **juzga como un todo** (¿ganó el desk?): `outcome_hit` con el mismo
etiquetado ATR/beta que las alertas; el `backtest` y el CLI muestran su hit-rate.

**Niveles que buscan las instituciones** (`flow/targets.py`, `/api/targets`,
comando `python -m flow targets`, panel en la UI): del flujo institucional, qué
strikes concentran la prima — objetivos call (alcistas), objetivos put
(bajistas) y niveles de cobertura (hedges) — agrupados en bandas del ~2%. Es
flujo de hoy, no OI acumulado: responde a dónde apuntan, no dónde están las
coberturas.

La **memoria de outcomes decae exponencialmente** (half-life 30 días,
`heuristics.memory.half_life_days` en config.yaml): los regímenes viejos dejan
de pesar en el score.

El **modelo de régimen** (BULLISH/BEARISH/HEDGING/MIXED) también aprende de
etiquetas reales: cuando hay ≥60 días con outcomes resueltos, entrena con la
dirección que funcionó por día; si no, usa el bootstrap sintético y lo indica
(`model_info()` / UI).

La **puerta de calidad** descarta scans con cadena vacía/stale, salto de spot >8%
(glitch) o horquillas invertidas: se guardan marcados (`quality_ok=0`) pero no emiten
alertas para no envenenar la memoria de outcomes.

## Flujo recomendado (semana)

- Lunes–viernes: `python -m flow watch` en horario de mercado (o un `scan` 2–4×/día)
- Domingo/lunes: `python -m flow summary --days 7` para ver lean call/put por ticker

DB: `data/flow.db`
