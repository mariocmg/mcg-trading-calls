# FLOW AI — Posicionamiento Institucional al estilo BladeMap (Vector · Lattice · Pulse)

Una interfaz oscura y directa inspirada en **BladeMap AI**, alimentada por un motor híbrido (Microestructura + Griegas reales de Cboe + Clasificador de Intención IA + síntesis opcional con Gemini LLM).

## Arrancar

Doble clic en `iniciar.bat` (crea el `.venv` e instala dependencias si hace falta, levanta el servidor y abre `http://127.0.0.1:8787/`). O por consola:

```bash
python -m flow              # servidor + escaneo cada 30 s con el mercado abierto
python -m flow scan         # una pasada en MU y veredicto por consola
python -m flow scan NVDA    # escaneo directo de cualquier ticker (NVDA, TSLA, SPY, QQQ…)
```

## Arquitectura estilo BladeMap AI

1. **PULSE (Filtro IA de Microestructura e Intención Institucional)**:
   - **Inferencia híbrida de agresión (`infer_side`)**: Cruza la posición del último precio en la horquilla (`bid/ask`) con el **precio teórico del modelo de Cboe (`theo`)** para rescatar compras y ventas agresivas incluso tras desplazamientos de horquilla.
   - **Clasificador de Intención IA (`classify_prints_ai`)**: Etiqueta cada print y filtra automáticamente el ruido:
     - 🐋 **BALLENA (`WHALE`)**: Apertura direccional agresiva de alta convicción (`convicción ≥ 82` y `prima ≥ $250K`).
     - 🎯 **APERTURA CALL / PUT (`DIRECTIONAL`)**: Flujo direccional limpio al ask.
     - 💰 **VENTA PUT (SUELO) / VENTA CALL (TECHO) (`INCOME`)**: Instituciones vendiendo prima para defender un soporte o fijar un techo.
     - 🛡️ **COBERTURA PUT (`HEDGE`)**: Puts fuera de dinero (`OTM ≥ 3.5%` o `|Δ| ≤ 0.28`) compradas mientras domina el flujo alcista. Su voto bajista se atenúa (`0.25×`) para no contaminar la señal direccional.
     - 🔄 **CIERRE / ROTACIÓN (`CLOSING`)**: Contratos con caída de interés abierto (`ΔOI < 0`). Ocultos por defecto en el Filtro IA.
   - **Detección de Estructuras Multi-Pata**: Detecta automáticamente `CALL SPREAD`, `PUT SPREAD` y `RISK REVERSAL` cuando dos patas del mismo vencimiento se ejecutan con volumen equivalente.

2. **LATTICE (Exposición Gamma Real de Dealers — GEX)**:
   - Calcula el **Dealer Gamma Exposure (`net_gex`)** real por strike usando la `gamma` y el `open_interest` del feed de Cboe:
     $$\text{GEX}_{\text{call}} = +\text{OI} \times \gamma \times 100 \times \text{Spot}^2 \times 0.01, \quad \text{GEX}_{\text{put}} = -\text{OI} \times \gamma \times 100 \times \text{Spot}^2 \times 0.01$$
   - Determina el **Régimen Gamma** (**Gamma Positiva · Estabilizador** vs. **Gamma Negativa · Acelerador**), el **King Node** (mayor imán gamma) y el **Gamma Flip** (nivel de cambio de régimen).

3. **VECTOR & MAPA VISUAL ("Hacia dónde se están posicionando")**:
   - **Mapa de Posicionamiento Institucional**: Barra visual que sitúa en una sola línea el **Suelo Put**, el **Spot Actual**, el **Objetivo IA (Imán)** y el **Techo Call**, mostrando con un vector luminoso hacia qué strike exacto empujan las instituciones.
   - **Modo Stand Down**: Cuando no hay convicción institucional neta (`< $500K`), recomienda mantenerse al margen en lugar de forzar operaciones.
   - **Multi-Ticker**: Selector rápido (`MU`, `NVDA`, `TSLA`, `SPY`, `QQQ`, `AMD`, `AAPL`, `META`, `AMZN`, `MSFT`, `PLTR`) más buscador para cualquier ticker.
   - **Síntesis LLM Opcional**: Si defines `GEMINI_API_KEY` en `.env`, añade un resumen ejecutivo generado por Gemini 2.5 Flash; sin API key, funciona al 100% con el motor determinista local.

4. **Ciclo Vector, sellos BM, OI sintético, gates y setup (aplicación BladeMap, 2026-10-30)**:
   - **Ciclo Vector (`vector_update`)**: por ticker y día, etapa por scans consecutivos en la misma dirección (1 = Giro/Flip, 2 = Construyendo/Follow, 3+ = Confirmado/Confirm) con frescura por minutos desde el giro (≤15 Reciente, ≤45 Activo, ≤90 Envejeciendo, resto Entrada tardía) y conteo de caídas de etapa. No cruza días. Es distinto de la etapa del veredicto (EARLY/BUILDING/CONFIRMED por confianza): ambas se muestran con nombres distintos.
   - **Sellos BladeMap (`bm_badge`, NO confundir con `ai_grade`)**: Whale ≥ $1M; Golden ≥ $900K y 2–30% OTM; Silver ≥ $300K con etiqueta de convicción (barrido o volumen > OI). La pata "5× sobre la norma" no se calcula: el snapshot no trae media de volumen por contrato y no se finge.
   - **Lectura Apertura/Cierre (`open_close`)**: dirección OPRA real (BTO/STO = apertura, BTC/STC = cierre) o ΔOI en cadena.
   - **OI sintético (`synthetic_oi`)**: OI oficial + flujo firmado del día por strike (compra suma, venta resta). Proxy, no OI oficial.
   - **Gates y setup (`gates`, `setup_update`)**: frescura (retraso declarado), dirección, respaldo (≥2 prints votando o racha ≥2), explicado y SMA horaria (pata Holy Grail: el spot debe estar del lado correcto de la SMA5 horaria de TradingView; si el sidecar no está disponible el gate queda en ? y bloquea la confirmación, porque nada sin verificar confirma). Setup confirmado = gates en verde + ciclo en Confirmado; la misma clave dirección:etapa se pliega en un contador ×N (estilo Sentinel) en vez de abrir otra tarjeta.
   - **NO implementado (y por qué)**: un Vector supertrend 5m de precio (el backtest `strategy-search/backtest-vector-tv.mjs` sobre MU 5m extendido, 21-sep → 30-oct-2026, muestra que mult 3/4 no gira nunca, mult 2 gira 5 veces en gaps de sesión y mult ≤1.5 es ruido con ~50% de supervivencia a 60 min: sin filo demostrable); chat Barry (la síntesis Gemini opcional ya cubre ese papel); nada de Academy, Discord, Whop o facturación (no son el escáner).

## Fuente de prints: OPRA (CaymanBot) + respaldo Cboe

- **Primaria: cinta OPRA** — `GET https://app.caymanbot.com/api/v1/trades?limit=200&symbol=MU`, sin sesión ni API key. Lado real de cada trade, flags whale/sweep/unusual, multileg (`strategy_*`) y `unique_id` para deduplicar. Una página por scan (200 trades recientes) acumulada por `unique_id`.
- **Retraso**: 60 min en anónimo (16 min con cuenta gratis de CaymanBot + sesión; esa cuenta la crea el usuario si la quiere — el código no usa ninguna credencial). **No es cinta en vivo**: el retraso lo aplica el servidor.
- **Respaldo**: si OPRA falla o viene vacía, los prints salen de la cadena delayed de Cboe (lado inferido) como antes. Niveles, GEX y spot siempre salen de la cadena Cboe.
- **Fragilidad**: el endpoint es interno y no documentado (CaymanBot dice no tener API pública); puede cambiar, limitar o romperse sin aviso. Por eso el respaldo Cboe siempre está activo.
- **Filtro**: solo entran trades con prima ≥ $50K; la cinta del día se limita a los 500 de mayor prima.

## Tendencia horaria: sidecar Node + TradingView (pata SMA del Holy Grail)

El escáner es Python y la librería de TradingView (`@mathieuc/tradingview`) es Node, así que la SMA5 horaria vive en un sidecar: `tv/tv-hourly.mjs` (velas 1h de sesión regular, últimas 5 horas completadas; la barra en formación se excluye) invocado por `flow/tv_hourly.py` como subprocess. La tendencia (`trend` en el estado) trae SMA5, último cierre, barra, posición del spot en vivo frente a la media y distancia %; el gate SMA exige el lado correcto según la dirección (alcista: por encima; bajista: por debajo).

- **Requisito**: Node ≥ 20 en el PATH. `iniciar.bat` avisa si falta Node e instala `tv/node_modules` con npm si no existe. Sin Node, el escáner funciona entero salvo este gate, que queda en ? y bloquea la confirmación del setup.
- **Caché**: por ticker, 15 min, más refresco cuando se completa una hora nueva en horario regular. El sidecar no se llama en lecturas de `/api/state`, solo dentro de `run_scan`.
- **Fallo sin bloquear**: si el sidecar falla, se reutiliza el último valor marcado como obsoleto; si no hay ninguno, `trend` trae el error y el gate queda en ?. Un fallo de tendencia nunca marca el scan como error.
- **Variables**: `TV_NODE_CMD` (comando node, por defecto `node`); `TV_USE_RAW_TRANSPORT=1` solo para el sandbox de desarrollo, donde el proxy rompe el handshake websocket normal (en Windows no hace falta).
- **Mercados**: mapa en el script para los 11 tickers por defecto (SPY en AMEX, resto NASDAQ); un ticker desconocido prueba NASDAQ, NYSE y AMEX en ese orden.
