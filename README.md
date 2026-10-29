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

## Fuente de prints: OPRA (CaymanBot) + respaldo Cboe

- **Primaria: cinta OPRA** — `GET https://app.caymanbot.com/api/v1/trades?limit=200&symbol=MU`, sin sesión ni API key. Lado real de cada trade, flags whale/sweep/unusual, multileg (`strategy_*`) y `unique_id` para deduplicar. Una página por scan (200 trades recientes) acumulada por `unique_id`.
- **Retraso**: 60 min en anónimo (16 min con cuenta gratis de CaymanBot + sesión; esa cuenta la crea el usuario si la quiere — el código no usa ninguna credencial). **No es cinta en vivo**: el retraso lo aplica el servidor.
- **Respaldo**: si OPRA falla o viene vacía, los prints salen de la cadena delayed de Cboe (lado inferido) como antes. Niveles, GEX y spot siempre salen de la cadena Cboe.
- **Fragilidad**: el endpoint es interno y no documentado (CaymanBot dice no tener API pública); puede cambiar, limitar o romperse sin aviso. Por eso el respaldo Cboe siempre está activo.
- **Filtro**: solo entran trades con prima ≥ $50K; la cinta del día se limita a los 500 de mayor prima.
