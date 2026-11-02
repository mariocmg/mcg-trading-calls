# FLOW · Pulse — Posicionamiento institucional, simple

La página muestra solo tres cosas: **veredicto** (del flujo institucional
de la sesión), **tres niveles** de la cadena Cboe y la **tabla de prints
institucionales**. Nada de objetivos, gates, vectores ni párrafos: si el
filtro no lo pasa, no vota y no se muestra.

## Arrancar

Doble clic en `iniciar.bat` (crea el `.venv` e instala dependencias si hace falta, levanta el servidor y abre `http://127.0.0.1:8787/`). O por consola:

```bash
python -m flow              # servidor + escaneo cada 30 s con el mercado abierto
python -m flow scan         # una pasada en MU y veredicto por consola
python -m flow scan NVDA    # escaneo directo de cualquier ticker (NVDA, TSLA, SPY, QQQ…)
```

## Filtro Pulse: qué prints son institucionales

Cada print puntúa 0–10 (`engine.pulse_score`) y solo cuentan los de **7 o
más** (el ajuste de operador de BladeMap: filtrar 7+). Componentes, todos
observables en OPRA/Cboe:

| Componente | Puntos |
|---|---|
| Prima ≥ $1M / ≥ $500k / ≥ $250k | 3 / 2 / 1 |
| Volumen del print ≥ OI / ≥ 0,5×OI | 2 / 1 |
| Barrido (sweep) | 1 |
| Cluster 90s: ≥3 trades del mismo contrato en 90 s (solo OPRA, que trae timestamp por trade) | 2 |
| Apertura (BTO/STO en OPRA, o ΔOI > 0 en cadena) | 1 |
| Banda OTM 2–30% | 1 |

Los **cierres** (BTC/STC en OPRA, o ΔOI < 0 en cadena) y los prints **sin
lado** puntúan 0 siempre: no son posicionamiento nuevo. El veredicto
(ALCISTA/BAJISTA/NEUTRAL, umbral $500K de neto) sale **solo** de los prints
que pasan el filtro; la tabla muestra solo esos. El pie de página dice
cuántos prints hubo, cuántos pasaron y cuántos se descartaron, para que
un filtro mal calibrado se vea a primera vista.

**Lo que NO se calcula (y no se finge)**: el "volumen relativo 5× sobre
la norma" del grader de BladeMap exige una media de volumen por contrato
que el snapshot no trae. Tampoco se replica su math de servidor (no está
en el bundle público): este score es una aproximación documentada, no su
fórmula.

**El clasificador viejo (`classify_prints_ai`)** sigue corriendo y
etiquetando (WHALE/DIRECTIONAL/INCOME/HEDGE/CLOSING, sellos BM, lectura
apertura/cierre), pero **ya no decide nada**: lo que decide qué es
institucional es el score Pulse. Se puede borrar en una limpieza futura.

## Niveles (cadena Cboe)

Suelo put (put wall), techo call (call wall) y gamma flip, del OI y la
gamma reales de Cboe, con el régimen gamma (positiva = amortigua/rango,
negativa = acelera/tendencial). Niveles, GEX y spot siempre salen de la
cadena Cboe, nunca de la cinta.

## Lo que ya no muestra la página (2026-11-02)

- **Objetivo/imán**: era el strike con más prima del lado del veredicto,
  sin comprobar que un objetivo bajista estuviera por debajo del spot.
  Así salía "BAJISTA presionando hacia $1190 (+39,7%)": una venta de call
  1190 es un techo, no un destino. Eliminado, no arreglado: un strike con
  mucho flujo no es un objetivo de precio.
- **Gates, Vector, setup ×N, tendencia 1h, campañas, estadísticas del
  libro y mapa Lattice**: texto y tarjetas que no decidían nada. El
  código (`vector_update`, `gates`, `setup_update`, `positioning_map`,
  `campaigns`, `book_stats`) sigue en `engine.py` y sus tests en verde,
  pero el servidor ya no lo calcula ni la página lo pide.
- **Etiqueta "IA"**: el filtro es una suma de reglas documentadas, no un
  modelo. Se llama Pulse, como lo que hace.

## Fuente de prints: OPRA (CaymanBot) + respaldo Cboe

- **Primaria: cinta OPRA** — `GET https://app.caymanbot.com/api/v1/trades?limit=200&symbol=MU`, sin sesión ni API key. Lado real de cada trade, flags whale/sweep/unusual, multileg (`strategy_*`) y `unique_id` para deduplicar. Una página por scan (200 trades recientes) acumulada por `unique_id`.
- **Retraso**: 60 min en anónimo (16 min con cuenta gratis de CaymanBot + sesión; esa cuenta la crea el usuario si la quiere — el código no usa ninguna credencial). **No es cinta en vivo**: el retraso lo aplica el servidor.
- **Respaldo**: si OPRA falla o viene vacía, los prints salen de la cadena delayed de Cboe (lado inferido) como antes. Niveles, GEX y spot siempre salen de la cadena Cboe.
- **Fragilidad**: el endpoint es interno y no documentado (CaymanBot dice no tener API pública); puede cambiar, limitar o romperse sin aviso. Por eso el respaldo Cboe siempre está activo.
- **Filtro**: solo entran trades con prima ≥ $50K; la cinta del día se limita a los 500 de mayor prima.

## Sidecar Node + TradingView (sin uso desde 2026-11-02)

El código sigue en `tv/` (`tv-hourly.mjs`: velas 1h de sesión regular,
SMA5 de las últimas 5 horas completadas) y `flow/tv_hourly.py`, pero el
bucle de scan ya no lo llama: la página simple no tiene gate SMA y no
tiene sentido lanzar Node en cada scan para un dato que no se muestra.
Si vuelve a hacer falta un gate de tendencia, se reengancha llamando a
`tv_hourly.snapshot` dentro de `run_scan`. Requisito si se reactiva:
Node ≥ 20 en el PATH (`iniciar.bat` lo comprueba e instala
`tv/node_modules` si falta).
