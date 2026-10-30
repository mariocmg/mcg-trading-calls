// Sidecar: velas horarias de TradingView para la pata SMA del setup Holy Grail.
// Uso: node tv-hourly.mjs <TICKER>   -> JSON en stdout, errores en stderr + exit 1.
// No se usa en el PC de Mario con transporte especial: el transporte manual
// (tv_transport.mjs) solo se activa con TV_USE_RAW_TRANSPORT=1, que solo hace
// falta en el sandbox de desarrollo, donde el proxy rompe el handshake
// websocket normal de la librería. En Windows el transporte por defecto vale.
import { getCandles } from '@mathieuc/tradingview';

// Mapa de mercado TradingView para los tickers por defecto del escáner.
// Un ticker desconocido prueba NASDAQ y luego NYSE.
const EXCHANGES = {
  MU: 'NASDAQ', NVDA: 'NASDAQ', TSLA: 'NASDAQ', QQQ: 'NASDAQ',
  AMD: 'NASDAQ', AAPL: 'NASDAQ', META: 'NASDAQ', AMZN: 'NASDAQ',
  MSFT: 'NASDAQ', SPY: 'AMEX', PLTR: 'NASDAQ',
};
const PERIOD = 5; // SMA de 5 horas, como el Holy Grail de BladeMap

function etParts(date) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York', year: 'numeric', month: '2-digit',
    day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(date);
  const get = (t) => Number(parts.find((p) => p.type === t).value);
  return { y: get('year'), mo: get('month'), d: get('day'), h: get('hour'), mi: get('minute') };
}

// Inicio (ET) de la última hora completada: a las 10:20 ET es la barra de
// las 09:00; tras el cierre, la de las 15:00 del día. Las barras de TV van
// etiquetadas por su inicio, así que filtrar por inicio <= ese instante
// deja fuera la barra en formación.
function latestCompletedHourStartMs(now = new Date()) {
  const p = etParts(now);
  // Buscar el instante UTC cuya representación ET es la hora buscada
  // (la hora anterior a la hora ET actual), en pasos de 15 min.
  const target = { y: p.y, mo: p.mo, d: p.d, h: p.h - 1 };
  let guess = Date.UTC(target.y, target.mo - 1, target.d, target.h);
  for (let i = 0; i < 96; i += 1) {
    const q = etParts(new Date(guess));
    if (q.y === target.y && q.mo === target.mo && q.d === target.d && q.h === target.h) return guess;
    const qDate = Date.UTC(q.y, q.mo - 1, q.d, q.h);
    const tDate = Date.UTC(target.y, target.mo - 1, target.d, target.h);
    guess += (tDate > qDate ? 900000 : -900000);
  }
  return guess;
}

function fmtEt(ms) {
  const p = etParts(new Date(ms));
  const pad = (n) => String(n).padStart(2, '0');
  return `${p.y}-${pad(p.mo)}-${pad(p.d)} ${pad(p.h)}:${pad(p.mi)} ET`;
}

async function fetchBars(symbol) {
  let transport;
  if (process.env.TV_USE_RAW_TRANSPORT === '1') {
    const { createRawTransport } = await import('./tv_transport.mjs');
    transport = createRawTransport(process.env.HTTPS_PROXY || '');
  }
  const raw = await getCandles({
    symbol, timeframe: '60', session: 'regular',
    from: new Date(Date.now() - 14 * 86400 * 1000),
    clientOptions: transport ? { transport } : undefined,
    timeoutMs: 60000,
  });
  return [...raw].sort((a, b) => a.time - b.time).filter((b) => b.close > 0);
}

async function main() {
  const ticker = (process.argv[2] || '').toUpperCase();
  if (!ticker) {
    console.error('Uso: node tv-hourly.mjs <TICKER>');
    process.exit(1);
  }
  const candidates = [];
  const mapped = EXCHANGES[ticker];
  if (mapped) candidates.push(mapped);
  for (const ex of ['NASDAQ', 'NYSE', 'AMEX']) if (!candidates.includes(ex)) candidates.push(ex);
  let bars = [];
  let used = null;
  for (const ex of candidates) {
    try {
      const got = await fetchBars(`${ex}:${ticker}`);
      if (got.length >= PERIOD) { bars = got; used = ex; break; }
    } catch { /* probar siguiente mercado */ }
  }
  if (!used) {
    console.error(`Sin velas horarias para ${ticker} en ${candidates.join('/')}`);
    process.exit(1);
  }
  const cutoff = latestCompletedHourStartMs();
  const completed = bars.filter((b) => b.time * 1000 <= cutoff);
  const tail = completed.slice(-PERIOD);
  if (tail.length < PERIOD) {
    console.error(`Solo ${tail.length} horas completadas para ${ticker}, hacen falta ${PERIOD}`);
    process.exit(1);
  }
  const closes = tail.map((b) => b.close);
  const sma5 = closes.reduce((a, x) => a + x, 0) / closes.length;
  const last = tail[tail.length - 1];
  console.log(JSON.stringify({
    ticker, exchange: used, sma5: Number(sma5.toFixed(2)),
    last_close: last.close, last_bar_et: fmtEt(last.time * 1000),
    n_bars: completed.length, source: 'tradingview',
  }));
}

main().catch((e) => { console.error(`tv-hourly: ${e?.message || e}`); process.exit(1); });
