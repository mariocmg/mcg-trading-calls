const $ = (s) => document.querySelector(s);

let maxAlertId = 0;
let live = true;
const alerts = new Map();
const freshIds = new Set();

function money(n) {
  const v = Number(n) || 0;
  if (Math.abs(v) >= 1e6) return `$${(v / 1e6).toFixed(2)}M`;
  if (Math.abs(v) >= 1e3) return `$${(v / 1e3).toFixed(0)}K`;
  return `$${Math.round(v)}`;
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    timeZone: "America/New_York",
  });
}

function otmLabel(a) {
  if (a.otm_pct == null || Number.isNaN(Number(a.otm_pct))) return "—";
  const v = Number(a.otm_pct);
  return `${v > 0 ? "+" : ""}${v.toFixed(1)}%`;
}

function setStatus(text, mode) {
  $("#status").textContent = text;
  $("#live-dot").className = "dot " + (mode || (live ? "on" : "off"));
}

async function api(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

function filters() {
  return {
    und: ($("#f-und").value || "").toUpperCase(),
    side: ($("#f-side").value || "").toUpperCase(),
    maxDte: $("#f-dte").value === "" ? null : Number($("#f-dte").value),
    minOtm: $("#f-otm").value === "" ? null : Number($("#f-otm").value),
    minPrem: Number($("#f-prem").value) || 0,
  };
}

function matches(a, f) {
  if (f.und && a.underlying !== f.und) return false;
  if (f.side && a.call_put !== f.side) return false;
  if (f.maxDte != null && (a.dte == null || a.dte > f.maxDte)) return false;
  if (f.minOtm != null && (a.otm_pct == null || a.otm_pct < f.minOtm)) return false;
  if ((Number(a.premium_usd) || 0) < f.minPrem) return false;
  return true;
}

function fillUnderlyings(list) {
  const sel = $("#f-und");
  const cur = sel.value || "MU";
  const set = new Set(["MU", ...list]);
  sel.innerHTML =
    `<option value="">ALL</option>` +
    [...set].sort().map((u) => `<option value="${u}">${u}</option>`).join("");
  sel.value = [...sel.options].some((o) => o.value === cur) ? cur : "MU";
}

function renderFeed() {
  const f = filters();
  const feed = $("#feed");
  const rows = [...alerts.values()]
    .filter((a) => matches(a, f))
    .sort((a, b) => b.id - a.id)
    .slice(0, 150);

  feed.innerHTML = "";
  $("#feed-count").textContent = `${rows.length}`;

  if (!rows.length) {
    feed.innerHTML = `<div class="empty">${
      alerts.size ? "Sin coincidencias" : "Esperando flujo…"
    }</div>`;
    return;
  }

  for (const a of rows) {
    const row = document.createElement("div");
    row.className = "row" + (freshIds.has(a.id) ? " fresh" : "");
    row.innerHTML = `
      <span class="time">${fmtTime(a.event_at || a.created_at)}</span>
      <span class="sym">${a.underlying}</span>
      <span>${Number(a.strike).toFixed(0)}</span>
      <span class="cp ${a.call_put}">${a.call_put}</span>
      <span class="otm">${otmLabel(a)}</span>
      <span class="ia ${a.inst_label === "HEDGE" ? "hedge" : "inst"}">${
        a.inst_score != null
          ? `${a.inst_label === "HEDGE" ? "HDG" : "INST"} ${Math.round(Number(a.inst_score) * 100)}${Number(a.opening) ? " · OPEN" : ""}`
          : "—"
      }</span>
      <span>${a.dte ?? "—"}</span>
      <span class="prem">${money(a.premium_usd)}</span>
    `;
    feed.appendChild(row);
  }
}

function ingest(list, fresh) {
  for (const a of list) {
    if (!alerts.has(a.id)) {
      alerts.set(a.id, a);
      if (fresh) freshIds.add(a.id);
    } else {
      alerts.set(a.id, { ...alerts.get(a.id), ...a });
    }
    maxAlertId = Math.max(maxAlertId, a.id);
  }
  if (fresh) setTimeout(() => freshIds.clear(), 1200);
  fillUnderlyings([...new Set([...alerts.values()].map((a) => a.underlying))]);
  renderFeed();
}

async function loadAlerts(initial) {
  const data = await api(`/api/alerts?limit=200&days=7`);
  if (initial) {
    alerts.clear();
    freshIds.clear();
    maxAlertId = 0;
  }
  fillUnderlyings(data.underlyings || []);
  ingest(data.alerts, false);
}

async function loadNewAlerts() {
  if (!maxAlertId) {
    await loadAlerts(true);
    return;
  }
  const data = await api(`/api/alerts?after_id=${maxAlertId}&limit=100`);
  ingest(data.alerts, true);
}

async function loadVector() {
  const und = ($("#f-und").value || "MU").toUpperCase() || "MU";
  const v = await api(`/api/vector?underlying=${encodeURIComponent(und)}&days=7`);
  const dir = v.direction || "NEUTRAL";

  const signal = $(".signal");
  signal.className = "signal " + dir;

  $("#v-sym").textContent = v.symbol || und;

  const dirEl = $("#v-dir");
  if (dir === "BULLISH") dirEl.textContent = "▲ Alcista";
  else if (dir === "BEARISH") dirEl.textContent = "▼ Bajista";
  else dirEl.textContent = "● Sin señal";
  dirEl.className = "dir " + dir;

  const n = Number(v.stage_n) || 1;
  [...$("#v-stage-track").querySelectorAll("i")].forEach((el, i) => {
    el.classList.toggle("on", dir !== "NEUTRAL" && i < n);
  });
  $("#v-stage-label").textContent =
    dir === "NEUTRAL" ? "—" : `${v.stage || "EARLY"} · ${v.stage_fraction || "1/3"}`;

  $("#v-conf-num").textContent =
    dir === "NEUTRAL" && (v.components?.no_flow || Number(v.confidence) === 0)
      ? "—"
      : (v.confidence ?? "—").toString();
  $("#v-conf-label").textContent = dir === "NEUTRAL" ? "—" : v.confidence_label || "—";
  $("#v-conf-bar").style.width =
    dir === "NEUTRAL"
      ? "0%"
      : `${Math.max(0, Math.min(100, Number(v.confidence) || 0))}%`;

  const st = v.state || {};
  $("#v-state-title").textContent = st.title || "—";
  $("#v-state-detail").textContent = st.detail || "";
  const badge = $("#v-state-badge");
  badge.textContent = st.badge ? st.badge : "";
  badge.className = "pill " + (st.badge_kind || "");

  $("#v-price").textContent =
    v.price == null ? "—" : Number(v.price).toLocaleString(undefined, { maximumFractionDigits: 2 });

  const inv = v.invalidation || {};
  $("#v-inv-price").textContent =
    inv.price == null ? "—" : Number(inv.price).toLocaleString(undefined, { maximumFractionDigits: 2 });
  const arrow = $("#v-inv-arrow");
  arrow.textContent = inv.arrow === "up" ? "↑" : inv.arrow === "down" ? "↓" : "";
  arrow.className = "inv-arrow " + (inv.arrow || "");

  const status = $("#vec-status");
  status.textContent = v.status || "DEGRADED";
  status.className = "status-chip" + (v.status === "LIVE" ? " ok" : " warn");

  const ts = v.updated_at ? new Date(v.updated_at) : null;
  $("#vec-updated").textContent =
    ts && !Number.isNaN(ts.getTime())
      ? `LAST UPDATED ${ts.toLocaleString()}`
      : "LAST UPDATED —";
  await loadBook(und);
}

async function loadBook(und) {
  const b = await api(`/api/positioning?underlying=${encodeURIComponent(und)}`);
  if (!b.ok) {
    $("#book-net").textContent = "—";
    $("#book-split").textContent = "calls — · puts —";
    $("#book-avg").textContent = "—";
    $("#book-avg-split").textContent = "calls — · puts —";
    $("#book-gex").textContent = "sin gamma";
    return;
  }
  const net = Number(b.net_premium) || 0;
  const netEl = $("#book-net");
  netEl.textContent = `${net >= 0 ? "+" : "−"}${money(Math.abs(net))}`;
  netEl.className = net >= 0 ? "up" : "down";
  $("#book-split").textContent = `calls ${money(b.call_premium)} · puts ${money(b.put_premium)}`;
  const fmtK = (n) => (n == null || Number.isNaN(Number(n)) ? "—" : Number(n).toFixed(0));
  $("#book-avg").textContent = fmtK(b.avg_strike);
  $("#book-avg-split").textContent = `calls ${fmtK(b.call_avg_strike)} · puts ${fmtK(b.put_avg_strike)}`;
  const g = b.gex || {};
  $("#book-gex").textContent = g.ok
    ? `${g.side} ${Number(g.strike).toFixed(0)} · ${money(g.gex)}`
    : "sin gamma (cadena sin greek)";
}

async function refreshAll() {
  const s = await api("/api/status");
  live = !!s.live;
  $("#btn-live").classList.toggle("on", live);
  const mkt = s.market || {};
  if (s.scan_running) setStatus("escaneando…", "scan");
  else if (!mkt.open && live) setStatus("mercado cerrado · sin auto-scan", "off");
  else if (live) setStatus(`en vivo · ${s.interval}s`, "on");
  else setStatus("pausado", "off");
  await loadNewAlerts();
  await loadVector();
}

function connectLive() {
  const es = new EventSource("/api/live/stream");
  es.onmessage = async (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (msg.type === "scan_start") setStatus("escaneando…", "scan");
    if (msg.type === "market_closed") setStatus("mercado cerrado · sin auto-scan", "off");
    if (msg.type === "scan_done" || msg.type === "idle" || msg.type === "scan_error") {
      await refreshAll();
    }
    if (msg.type === "live") {
      live = !!msg.live;
      $("#btn-live").classList.toggle("on", live);
    }
    if (msg.type === "ping" && msg.running) setStatus("escaneando…", "scan");
  };
  es.onerror = () => setStatus("reconectando…", "off");
}

$("#btn-live").addEventListener("click", async () => {
  live = !live;
  await api(`/api/live?enabled=${live}`, { method: "POST" });
  $("#btn-live").classList.toggle("on", live);
  setStatus(live ? "en vivo" : "pausado", live ? "on" : "off");
});

$("#btn-scan").addEventListener("click", async () => {
  try {
    await api("/api/scan", { method: "POST" });
    setStatus("escaneando…", "scan");
  } catch (e) {
    setStatus(String(e.message || e), "off");
  }
});

$("#f-und").addEventListener("change", () => {
  renderFeed();
  loadVector().catch(() => {});
});

for (const id of ["#f-side", "#f-dte", "#f-otm", "#f-prem"]) {
  $(id).addEventListener("change", renderFeed);
}

$("#btn-howto").addEventListener("click", () => $("#howto-modal").showModal());

refreshAll()
  .then(connectLive)
  .catch((e) => setStatus(String(e.message || e), "off"));

setInterval(() => refreshAll().catch(() => {}), 20000);
