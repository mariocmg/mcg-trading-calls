# Fuentes de datos (gratis / casi gratis) — options flow

Objetivo: alimentación usable para FLOW / MU sin pagar OPRA enterprise.

## Veredicto corto

| Prioridad | Fuente | Coste | Latencia | Calidad vs Yahoo | Flow institucional real |
|-----------|--------|-------|----------|------------------|-------------------------|
| **1 (integrar ya)** | **Cboe delayed JSON** | $0, sin API key | ~15 min | **Mucho mejor** (OI, bid/ask, greeks) | No (cadena, no tape) |
| **2 (mejor RT gratis\*)** | **Tradier Brokerage** | Cuenta brokerage ($0 min; plan puede tener fee) | **RT** con cuenta | Alta + streaming trades | Proxy fuerte (trades RT) |
| **3 (dev)** | **Alpaca Basic `indicative`** | $0 | Quotes “indicative”; trades ~15 min | Media (no OPRA real) | No |
| 4 | Yahoo / yfinance | $0 | ~15 min | Baja (OI/bid a menudo 0) | No |
| — | Polygon/Massive free | $0 limitado | Delayed, options tick **no free** | — | No |
| — | Alpha Vantage free | $0 (25/día) | Options RT solo premium | — | No |
| — | Unusual Whales / BladeMap API | de pago | RT | Alta | Producto flow |

\*“Gratis” = sin vendor de market-data; puede requerir abrir cuenta / plan broker.

**No existe OPRA tape institucional etiquetado gratis.** Lo gratis es cadena delayed o trades RT vía broker.

---

## 1. Cboe Delayed Quotes (recomendado ahora)

- URL verificada MU: `https://cdn.cboe.com/api/global/delayed_quotes/options/MU.json`
- Sin auth. Público (superficie no “soportada” formalmente; puede cambiar).
- Campos útiles: `volume`, `open_interest`, `bid`, `ask`, `last_trade_price`, greeks, `current_price` del underlying.
- Test local (2026-09-24): **8337** contratos con OI>0; top vol con bid/ask reales (Yahoo a menudo OI=0).

Ideal para posicionamiento **semanal** + heurísticas vol/OI/premium.

## 2. Tradier Brokerage API

- Docs: https://docs.tradier.com/docs/market-data  
- Sandbox: delayed 15 min.  
- Cuenta live: **options + equities RT** + WebSocket trades (`wss://ws.tradier.com`).
- Mejor siguiente paso si quieres acercarte a “flow” (prints), aún sin label institucional.

## 3. Alpaca Basic — feed `indicative`

- Docs: https://docs.alpaca.markets/docs/historical-option-data  
- Gratis en plan Basic. Quotes no son OPRA reales; trades delayed ~15 min.
- Útil para desarrollar contra su schema; no para lean institucional serio.
- OPRA de pago (Algo Trader Plus ~$99/mo según pricing público).

## 4. Yahoo Finance (`yfinance`)

- Ya usado en el MVP. Fácil, inestable en OI/quotes.
- Mantener como fallback.

## 5. Lo que NO es free para flow real

- Polygon/Massive options tick  
- Unusual Whales / Tradytics APIs  
- Cboe LiveVol All Access (trial, luego de pago + OPRA entitlements)

---

## Sitios, foros y Reddit (revisado 2026-09-26)

No apareció un tape OPRA gratis y completo. Lo que sí existe, y se puede usar, es esto.

### Sirve gratis (con límites claros)

| Sitio | Qué es | Gratis de verdad | No es |
|-------|--------|------------------|-------|
| [Barchart unusual activity](https://www.barchart.com/options/unusual-activity/stocks) | Contratos con volumen alto vs open interest | Página visible, ~15 min | El flow de trades grandes está en Premier. El screener avanzado también |
| [OptionStrat Flow](https://optionstrat.com/flow) | Trades inusuales (dicen que consolidan prints OPRA) | ~15 min y **~10%** del flow | El tape entero y el tiempo real son de pago |
| [pve.trade](https://pve.trade/) | Sweeps/blocks | Preview de **10 filas** delayed; bot de Discord a 15 min | El tiempo real se retira el 14 sep 2026. El feed completo es Pro |
| [SquawkFlow](https://squawkflow.com/options-flow) | Scanner sin registro | Cadena delayed, refresh ~60 s | No es time & sales: premium = volumen × mid |
| [EvidInvest](https://evidinvest.com/options) | Diff de cadenas Cboe cada 15 min | Público, sin inventar lado comprador | Snapshot, no print |
| [Webull Pulse](https://www.webull.com/help/faq/10830-What-is-Unusual-Option-Activity) | Volumen de calls/puts vs media 30 días | Con cuenta | Ranking por símbolo, no cada call |
| Cboe JSON / Yahoo | Cadenas | $0 | Ya usados en FLOW. Volumen agregado, no tape |

### Reddit (lo que dice la gente, no publicidad)

- [r/options — herramientas de flow](https://www.reddit.com/r/options/comments/1t0fhj9/whats_your_preferred_options_flow_tool/): Webull “meh”; **pve.trade** con 15 min gratis; Unusual Whales / Cheddar / FlowAlgo de pago. Consenso: el flow gratis es delayed o filtrado.
- [r/options — Flowdesk](https://www.reddit.com/r/options/comments/1tea7ch/built_a_free_options_flow_scanner_that_explains/): [flowdesk.vercel.app](https://flowdesk.vercel.app) se anuncia sin registro; un usuario vio **15 min de delay** y un upgrade.
- [r/options — Stocknear](https://www.reddit.com/r/options/comments/1q5st84/built_a_realtime_options_flow_tool_would_love/): se vende como OPRA en vivo; en el hilo lo tratan como anuncio. Su propia guía dice que la cuenta gratis ve **6 trades y 15 min** en dark pool, y el options flow en vivo es Pro.

### Pared de pago (trial, no fuente)

Cheddar Flow, Unusual Whales, FlowAlgo, Tradytics, BladeMap, Barchart Options Flow, Stocknear Pro. El “free” es prueba o un recorte mínimo.

## Plan de integración en esta app

1. **Hecho / siguiente:** provider `cboe` como default (cadenas delayed con OI).
2. **Después:** Tradier token en `.env` + stream de trades MU para Δpremium real.
3. **Opcional:** Alpaca indicative como tercer provider.

Config: `config.yaml` → `data_provider: cboe | yahoo | tradier`
