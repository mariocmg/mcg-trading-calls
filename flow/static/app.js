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
  sel.textContent = "";
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "ALL";
  sel.appendChild(all);
  for (const u of [...set].sort()) {
    const opt = document.createElement("option");
    opt.value = u;
    opt.textContent = u;
    sel.appendChild(opt);
  }
  sel.value = [...sel.options].some((o) => o.value === cur) ? cur : "MU";
}

function span(cls, text) {
  const el = document.createElement("span");
  if (cls) el.className = cls;
  el.textContent = text;
  return el;
}

function renderFeed() {
  const f = filters();
  const feed = $("#feed");
  const rows = [...alerts.values()]
    .filter((a) => matches(a, f))
    .sort((a, b) => b.id - a.id)
    .slice(0, 150);

  feed.textContent = "";
  $("#feed-count").textContent = `${rows.length}`;

  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = alerts.size ? "Sin coincidencias" : "Esperando flujo…";
    feed.appendChild(empty);
    return;
  }

  for (const a of rows) {
    const row = document.createElement("div");
    row.className = "row" + (freshIds.has(a.id) ? " fresh" : "");
    row.appendChild(span("time", fmtTime(a.event_at || a.created_at)));
    row.appendChild(span("sym", String(a.underlying ?? "")));
    row.appendChild(span("", Number(a.strike).toFixed(0)));
    row.appendChild(span(`cp ${a.call_put}`, String(a.call_put ?? "")));
    row.appendChild(span("otm", otmLabel(a)));
    const ia =
      a.inst_score != null
        ? `${a.inst_label === "HEDGE" ? "HDG" : "INST"} ${Math.round(Number(a.inst_score) * 100)}${Number(a.opening) ? " · OPEN" : ""}`
        : "—";
    row.appendChild(span(`ia ${a.inst_label === "HEDGE" ? "hedge" : "inst"}`, ia));
    const flags = document.createElement("span");
    flags.className = "flags";
    if (Number(a.is_block)) {
      const f = document.createElement("i");
      f.className = "flag";
      f.textContent = "BLOCK";
      flags.appendChild(f);
    }
    if (a.combo_type) {
      const f = document.createElement("i");
      f.className = "flag combo";
      f.textContent = String(a.combo_type).replace(/_/g, " ");
      flags.appendChild(f);
    }
    if (!flags.hasChildNodes()) flags.textContent = "—";
    row.appendChild(flags);
    row.appendChild(span("", a.dte == null ? "—" : String(a.dte)));
    row.appendChild(span("prem", money(a.premium_usd)));
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

  // Evidence checklist (5 checks → stage)
  const checksEl = $("#v-checks");
  checksEl.textContent = "";
  for (const c of v.checks || []) {
    const chip = document.createElement("span");
    chip.className = "check" + (c.ok ? " on" : "");
    chip.textContent = `${c.ok ? "✓" : "✗"} ${c.label}`;
    chip.title = c.detail || "";
    checksEl.appendChild(chip);
  }
  $("#v-conf-note").textContent = v.confidence_note || "";

  const invBits = [];
  if (inv.reason) invBits.push(inv.reason);
  if (inv.method) invBits.push(`método: ${inv.method}`);
  if (inv.time) invBits.push(`horizonte: ${inv.time}${inv.time_reason ? ` (${inv.time_reason})` : ""}`);
  $("#v-inv-detail").textContent = invBits.join(" · ");

  const status = $("#vec-status");
  status.textContent = v.status || "DEGRADED";
  status.className = "status-chip" + (v.status === "LIVE" ? " ok" : " warn");

  const ts = v.updated_at ? new Date(v.updated_at) : null;
  $("#vec-updated").textContent =
    ts && !Number.isNaN(ts.getTime())
      ? `LAST UPDATED ${ts.toLocaleString()}`
      : "LAST UPDATED —";
  await loadBook(und);
  await loadLevels(und).catch(() => {});
  await loadCampaigns(und).catch(() => {});
}

async function loadCampaigns(und) {
  const data = await api(`/api/campaigns?underlying=${encodeURIComponent(und)}&limit=6`);
  const list = $("#camp-list");
  list.textContent = "";
  const camps = data.campaigns || [];
  $("#camp-count").textContent = `${camps.length}`;
  if (!camps.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "Sin campañas activas";
    list.appendChild(empty);
    return;
  }
  for (const c of camps) {
    const row = document.createElement("div");
    row.className = "camp";
    const left = document.createElement("span");
    const oc = c.outcome_hit == null ? "" : c.outcome_hit ? " ✓" : " ✗";
    left.textContent =
      `${c.call_put} ${Number(c.strike).toFixed(0)} · ${c.n_prints} prints · ${money(c.total_premium)}${oc}`;
    const right = document.createElement("span");
    right.className = `conv ${c.conviction || "BUILDING"}`;
    right.textContent = c.conviction || "BUILDING";
    row.appendChild(left);
    row.appendChild(right);
    list.appendChild(row);
  }
}

async function loadLevels(und) {
  const lv = await api(`/api/levels?underlying=${encodeURIComponent(und)}`);
  if (!lv.ok) {
    for (const id of ["#lv-call-wall", "#lv-put-wall", "#lv-flip", "#lv-maxpain"]) $(id).textContent = "—";
    return;
  }
  $("#lv-call-wall").textContent = lv.call_wall ? Number(lv.call_wall.strike).toFixed(0) : "—";
  $("#lv-put-wall").textContent = lv.put_wall ? Number(lv.put_wall.strike).toFixed(0) : "—";
  $("#lv-flip").textContent = lv.gamma_flip ? Number(lv.gamma_flip.strike).toFixed(0) : "—";
  const regime = lv.gamma_regime || {};
  $("#lv-regime").textContent =
    regime.regime === "LONG_GAMMA" ? "Long γ" : regime.regime === "SHORT_GAMMA" ? "Short γ" : "—";
  $("#lv-regime-read").textContent = regime.read_es || "—";
  const mp = lv.max_pain_nearest;
  $("#lv-maxpain").textContent = mp ? Number(mp.strike).toFixed(0) : "—";
  $("#lv-maxpain-exp").textContent = mp ? String(mp.expiry) : "—";
  try {
    const cb = await api(`/api/combos?underlying=${encodeURIComponent(und)}&days=7`);
    $("#lv-combos").textContent = String(cb.n || 0);
    $("#lv-combos-detail").textContent =
      (cb.combos || []).slice(0, 2).map((c) => c.label_es).join(" · ") || "—";
  } catch {
    $("#lv-combos").textContent = "—";
  }
  try {
    await loadTargets(und);
  } catch {
    /* targets panel stays empty */
  }
}

async function loadTargets(und) {
  const t = await api(`/api/targets?underlying=${encodeURIComponent(und)}&days=7`);
  const fmtLvl = (lv) => {
    if (!lv) return "—";
    const lo = Number(lv.strike_lo).toFixed(0);
    const hi = Number(lv.strike_hi).toFixed(0);
    return lo === hi ? lo : `${lo}–${hi}`;
  };
  const fmtDetail = (lv) =>
    lv ? `${money(lv.premium_usd)} · ${lv.n_prints} prints` : "—";
  const groups = [
    ["bull_targets", "#tg-bull", "#tg-bull-detail"],
    ["bear_targets", "#tg-bear", "#tg-bear-detail"],
    ["hedge_levels", "#tg-hedge", "#tg-hedge-detail"],
  ];
  for (const [key, main, detail] of groups) {
    const lvls = (t && t[key]) || [];
    $(main).textContent = lvls.slice(0, 2).map(fmtLvl).join(" · ") || "—";
    $(detail).textContent = lvls.length ? fmtDetail(lvls[0]) : "—";
  }
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

  // DTE buckets: where they position in time
  const buckets = b.dte_buckets || {};
  const names = ["0-7", "8-21", "22-45"];
  const ids = [["#book-dte1", "#book-dte1s"], ["#book-dte2", "#book-dte2s"], ["#book-dte3", "#book-dte3s"]];
  names.forEach((name, i) => {
    const bk = buckets[name];
    const [vId, sId] = ids[i];
    if (!bk || (bk.call_premium || 0) + (bk.put_premium || 0) <= 0) {
      $(vId).textContent = "—";
      $(sId).textContent = "—";
      return;
    }
    $(vId).textContent = money(bk.call_premium + bk.put_premium);
    $(sId).textContent =
      `C ${bk.call_share != null ? Math.round(bk.call_share * 100) + "%" : "—"}`;
  });
}

let refreshing = false;

async function refreshAll() {
  if (refreshing) return; // skip overlapping refreshes (interval + SSE can collide)
  refreshing = true;
  try {
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
  } finally {
    refreshing = false;
  }
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
