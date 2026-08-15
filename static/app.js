"use strict";

// Map panel_key -> render function for its payload body.
const RENDERERS = {
  regime: renderRegime,
  calendar: renderCalendar,
  gamma_spx: (b, p) => renderGamma(b, p, true),
  gamma_spy: renderSpyPositioning,
  vix_structure: renderVix,
  correlation: renderCorrelation,
  cftc_positioning: renderCftcPositioning,
  volume_profile: renderProfile,
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// --------------------------------------------------------------------------
// Utilities
// --------------------------------------------------------------------------

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function relTime(epochSec) {
  if (!epochSec) return "unknown time";
  const diff = Date.now() / 1000 - epochSec;
  if (diff < 0) return "just now";
  const units = [[31536000, "y"], [2592000, "mo"], [604800, "w"],
                 [86400, "d"], [3600, "h"], [60, "m"]];
  for (const [s, label] of units) {
    if (diff >= s) return `${Math.floor(diff / s)}${label} ago`;
  }
  return `${Math.floor(diff)}s ago`;
}

function absTime(epochSec) {
  return epochSec ? new Date(epochSec * 1000).toLocaleString() : "never";
}

function usd(x) {
  if (x == null) return "—";
  const a = Math.abs(x), s = x < 0 ? "-" : "";
  if (a >= 1e12) return `${s}$${(a / 1e12).toFixed(2)}tn`;
  if (a >= 1e9) return `${s}$${(a / 1e9).toFixed(1)}bn`;
  if (a >= 1e6) return `${s}$${(a / 1e6).toFixed(0)}mm`;
  return `${s}$${a.toFixed(0)}`;
}

function px(x, dp = 2) {
  return x == null ? "—" : Number(x).toLocaleString(undefined, {
    minimumFractionDigits: dp, maximumFractionDigits: dp });
}
// Plain counts (futures contracts), not dollars — see usd() for money.
function fmtNum(n) {
  if (n == null) return "—";
  const a = Math.abs(n);
  if (a >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(n);
}
function pct(frac, dp = 2) {
  return frac == null ? "—" : `${(frac * 100).toFixed(dp)}%`;
}
function signed(frac, dp = 2) {
  if (frac == null) return "—";
  const v = frac * 100;
  return `${v > 0 ? "+" : ""}${v.toFixed(dp)}%`;
}
function cls(x) { return x == null ? "" : (x >= 0 ? "pos" : "neg"); }

function card(label, value, sub, klass) {
  return `<div class="card">` +
    `<div class="card-label">${esc(label)}</div>` +
    `<div class="card-value ${klass || ""}">${value}</div>` +
    `<div class="card-sub">${sub || ""}</div></div>`;
}

function commentaryBlock(c) {
  if (!c) return "";
  const warns = (c.warnings || []).map((w) => `<div class="warn">${esc(w)}</div>`).join("");
  const sents = (c.sentences || []).map(esc).join(" ");
  return `<div class="row commentary">${warns}` +
    `<div class="headline">${esc(c.headline || "")}</div>` +
    `<p class="prose">${sents}</p></div>`;
}

// --------------------------------------------------------------------------
// Panel framing
// --------------------------------------------------------------------------

function panelEls(key) {
  const root = document.getElementById("panel-" + key);
  return { root, badge: $("[data-badge]", root), updated: $("[data-updated]", root),
           body: $("[data-body]", root) };
}

function setMeta(key, record) {
  const { badge, updated } = panelEls(key);
  if (!badge) return;
  if (!record || record.updated_at == null) {
    badge.className = "badge empty"; badge.textContent = "no data";
    updated.textContent = "never"; updated.title = ""; return;
  }
  const status = record.status || "ok";
  const hasPayload = !!record.payload;
  let cl, label;
  if (status === "error" && hasPayload) { cl = "stale"; label = "stale"; }
  else if (status === "error") { cl = "error"; label = "error"; }
  else if (status === "empty") { cl = "empty"; label = "empty"; }
  else { cl = "ok"; label = "ok"; }
  badge.className = "badge " + cl;
  badge.textContent = label;
  badge.title = record.error || "";
  updated.textContent = relTime(record.updated_at);
  updated.title = "Last updated: " + absTime(record.updated_at);
}

function renderPanel(key, record) {
  setMeta(key, record);
  const { body } = panelEls(key);
  const fn = RENDERERS[key];
  if (!body || !fn) return;
  try {
    fn(body, record ? record.payload : null, record);
  } catch (e) {
    body.innerHTML = `<p class="empty-note">Render error: ${esc(e.message)}</p>`;
  }
}

function renderAll(state) {
  for (const key of Object.keys(RENDERERS)) renderPanel(key, state[key]);
}

// --------------------------------------------------------------------------
// Regime banner
// --------------------------------------------------------------------------

const REGIME_CLASS = {
  PIN_GRIND: "r-pin", UNSTABLE_PIN: "r-unstable", ACCELERATION: "r-accel",
  REFLEXIVE_REPAIR: "r-repair", MIXED: "r-mixed",
};

function renderRegime(body, p) {
  if (!p || !p.regime) {
    body.innerHTML = `<p class="empty-note">No data yet — press ` +
      `<strong>Refresh</strong> to build the regime read.</p>`;
    return;
  }
  const inv = p.invalidation || {};
  const votes = (p.votes || []).map((v) =>
    `<div class="vote"><span class="vote-sig">${esc(v.signal)}</span>` +
    `<span class="vote-read">${esc(v.reading)}</span></div>`).join("");

  const destab = (p.destabilisers || []).length
    ? `<div class="chips">` + p.destabilisers.map((d) =>
        `<span class="chip warn">${esc(d)}</span>`).join("") + `</div>`
    : "";

  const banner =
    `<div class="row regime-banner">` +
      `<div>` +
        `<div class="regime-name ${REGIME_CLASS[p.regime] || ""}">${esc(p.label)}</div>` +
        `<div class="regime-conf">${Math.round((p.confidence || 0) * 100)}% ` +
          `SIGNAL AGREEMENT</div>` +
      `</div>` +
      `<div class="regime-side">` +
        `<p class="prose">${esc(p.posture || "")}</p>` +
        (inv.description
          ? `<div class="regime-inval"><b>Invalidation:</b> ${esc(inv.description)}</div>`
          : "") +
        destab +
      `</div>` +
    `</div>`;

  const conf = (p.confluence || []).length ? confluenceTable(p.confluence, p.spot) : "";
  return void (body.innerHTML =
    banner + commentaryBlock(p.commentary) +
    `<div class="row">${votes}</div>` + conf);
}

function confluenceTable(rows, spot) {
  const body = rows.map((r) => {
    const lvn = r.in_lvn
      ? `<span class="lvn-tag" title="Inside a low-volume corridor — nobody agreed ` +
        `on value here, so this level is weakened, not confirmed">LVN</span>` : "";
    return `<tr class="${r.score >= 3 ? "strong" : ""}">` +
      `<td>${px(r.level, 2)}${lvn}</td>` +
      `<td>${r.score}</td>` +
      `<td class="${cls(r.distance)}">${signed(r.distance_pct)}</td>` +
      `<td class="dim">${esc((r.sources || []).join(", "))}</td></tr>`;
  }).join("");
  return `<div class="row"><div class="tbl-wrap"><table class="data">` +
    `<thead><tr><th>Level (SPX)</th><th>Score</th><th>vs spot</th>` +
    `<th>Independent sources</th></tr></thead><tbody>${body}</tbody></table></div>` +
    `<div class="footnote">Spot ${px(spot)} · three or more independent sources ` +
    `marks a trade location; fewer is a note.</div></div>`;
}

// --------------------------------------------------------------------------
// Gamma panels
// --------------------------------------------------------------------------

function gexChart(p) {
  const chart = (p.chart || []).slice().sort((a, b) => b.strike - a.strike);
  if (!chart.length) return `<p class="empty-note">No strike data in window.</p>`;

  const n = chart.length;
  const rowH = 18, padTop = 24, padBot = 12;
  const H = padTop + padBot + n * rowH;
  const W = 1000, cx = 500, half = 415, gutter = 42;
  let maxAbs = 0;
  chart.forEach((c) => {
    maxAbs = Math.max(maxAbs, Math.abs(c.call_gex), Math.abs(c.put_gex));
  });
  maxAbs = maxAbs || 1;

  const strikes = chart.map((c) => c.strike);
  const maxK = strikes[0], minK = strikes[n - 1];
  const yFirst = padTop + rowH / 2, yLast = padTop + (n - 1) * rowH + rowH / 2;
  const yOf = (price) => {
    if (price == null || maxK === minK) return null;
    const f = Math.min(1, Math.max(0, (maxK - price) / (maxK - minK)));
    return yFirst + f * (yLast - yFirst);
  };

  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" ` +
    `aria-label="Dealer gamma exposure by strike; put gamma left, call gamma right">`;
  svg += `<line x1="${cx}" y1="${padTop - 8}" x2="${cx}" y2="${H - padBot + 4}" class="gex-axis"/>`;

  const bucket = p.bucket || 25;
  const magnets = new Set((p.oi_magnets || []).map(
    (m) => Math.round(m.strike / bucket) * bucket));

  chart.forEach((c, i) => {
    const y = padTop + i * rowH + rowH / 2;
    const cw = (Math.abs(c.call_gex) / maxAbs) * half;
    const pw = (Math.abs(c.put_gex) / maxAbs) * half;
    if (cw > 0.5)
      svg += `<rect class="gex-call" x="${cx + gutter}" y="${y - rowH * 0.32}" ` +
        `width="${cw}" height="${rowH * 0.64}"><title>${px(c.strike, 0)} call gamma ` +
        `${usd(c.call_gex)}</title></rect>`;
    if (pw > 0.5)
      svg += `<rect class="gex-put" x="${cx - gutter - pw}" y="${y - rowH * 0.32}" ` +
        `width="${pw}" height="${rowH * 0.64}"><title>${px(c.strike, 0)} put gamma ` +
        `${usd(c.put_gex)}</title></rect>`;
    svg += `<text class="gex-strike" x="${cx}" y="${y + 3}" text-anchor="middle">` +
      `${px(c.strike, 0)}</text>`;
    if (magnets.has(Math.round(c.strike / bucket) * bucket))
      svg += `<circle class="gex-magnet" cx="${cx - gutter - pw - 8}" cy="${y}" r="3">` +
        `<title>High open-interest magnet</title></circle>`;
  });

  const ys = yOf(p.spot);
  if (ys != null) {
    svg += `<line class="gex-spot" x1="30" y1="${ys}" x2="${W - 20}" y2="${ys}"/>`;
    svg += `<text class="gex-spot-lbl" x="34" y="${ys - 4}">Spot ${px(p.spot)}</text>`;
  }
  const yz = yOf(p.zero_gamma);
  if (yz != null) {
    svg += `<line class="gex-zero" x1="30" y1="${yz}" x2="${W - 20}" y2="${yz}"/>`;
    svg += `<text class="gex-zero-lbl" x="${W - 24}" y="${yz - 4}" text-anchor="end">` +
      `Flip ${px(p.zero_gamma)}</text>`;
  }
  const yc = yOf(p.call_wall);
  if (yc != null)
    svg += `<text class="gex-wall call" x="${W - 24}" y="${yc + 3}" text-anchor="end">` +
      `Call wall</text>`;
  const yp = yOf(p.put_wall);
  if (yp != null)
    svg += `<text class="gex-wall put" x="20" y="${yp + 3}">Put wall</text>`;

  svg += `</svg>`;
  return svg;
}

function charmChart(proj) {
  const s = (proj && proj.series) || [];
  if (!s.length) return "";
  const W = 1000, H = 190, padL = 8, padR = 8, padT = 16, padB = 34;
  const n = s.length;
  const bw = (W - padL - padR) / n;
  const vals = s.map((r) => r.charm_per_day);
  const maxAbs = Math.max(...vals.map(Math.abs), 1);
  const zeroY = padT + (H - padT - padB) * (maxAbs / (2 * maxAbs));
  const scale = (H - padT - padB) / (2 * maxAbs);

  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" ` +
    `aria-label="Projected dealer hedging flow per session from delta decay">`;
  svg += `<line class="bar-axis" x1="${padL}" y1="${zeroY}" x2="${W - padR}" y2="${zeroY}"/>`;

  s.forEach((r, i) => {
    const x = padL + i * bw + bw * 0.18;
    const w = bw * 0.64;
    const h = Math.abs(r.charm_per_day) * scale;
    const y = r.charm_per_day >= 0 ? zeroY - h : zeroY;
    svg += `<rect class="${r.charm_per_day >= 0 ? "bar-pos" : "bar-neg"}" x="${x}" ` +
      `y="${y}" width="${w}" height="${Math.max(h, 0.5)}">` +
      `<title>${r.date}: ${usd(r.charm_per_day)} (${r.charm_per_day >= 0 ? "buy" : "sell"})` +
      ` · cumulative ${usd(r.cum_hedge_flow)} · ${r.contracts_alive.toLocaleString()} ` +
      `contracts alive</title></rect>`;
    if (r.is_opex) {
      svg += `<line class="bar-opex" x1="${x + w / 2}" y1="${padT}" ` +
        `x2="${x + w / 2}" y2="${H - padB}"/>`;
      svg += `<text class="bar-lbl amber" x="${x + w / 2}" y="${padT - 4}" ` +
        `text-anchor="middle" fill="#f2c14e">OPEX</text>`;
    }
    svg += `<text class="bar-lbl" x="${x + w / 2}" y="${H - padB + 13}" ` +
      `text-anchor="middle">${esc(r.date.slice(5))}</text>`;
    svg += `<text class="bar-lbl" x="${x + w / 2}" y="${H - padB + 25}" ` +
      `text-anchor="middle">${usd(r.charm_per_day)}</text>`;
  });
  svg += `</svg>`;
  return svg;
}

function renderGamma(body, p, primary) {
  if (!p || p.spot == null) {
    body.innerHTML = `<p class="empty-note">No data yet — press ` +
      `<strong>Refresh</strong> to fetch the CBOE chain.</p>`;
    return;
  }
  const pos = p.regime === "positive";
  const flipSub = p.zero_gamma == null ? "no flip in ±8%" : `Flip ${px(p.zero_gamma)}`;

  const cards =
    `<div class="row cards ${primary ? "cards-6" : "cards-2"}">` +
    card("Regime", pos ? "Positive" : "Negative", flipSub, pos ? "pos" : "neg") +
    card("Spot vs flip", p.cushion_pct == null ? "—" : signed(p.cushion_pct),
         `Spot ${px(p.spot)}`, cls(p.cushion_pct)) +
    card("Net GEX", usd(p.net_gex), "per 1% move", cls(p.net_gex)) +
    card("Charm flow", `${usd(p.charm_drift)}`, "next session, + = dealers buy",
         cls(p.charm_drift)) +
    (primary ? card("Vanna", usd(p.vanna_pressure), "per −1 vol pt",
                    cls(p.vanna_pressure)) : "") +
    (primary ? card("0DTE gamma", pct(p.zero_dte_gamma_share, 0),
                    "of visible gamma",
                    p.zero_dte_gamma_share > 0.35 ? "warn" : "") : "") +
    `</div>`;

  const levels = [];
  if (p.call_wall != null) levels.push(["Call wall", p.call_wall]);
  (p.oi_magnets || []).forEach((m) => levels.push(["OI magnet", m.strike]));
  if (p.zero_gamma != null) levels.push(["Zero gamma", p.zero_gamma]);
  if (p.put_wall != null) levels.push(["Put wall", p.put_wall]);
  levels.push(["Spot", p.spot]);
  levels.sort((a, b) => b[1] - a[1]);
  const levelsTbl = `<div class="tbl-wrap"><table class="data">` +
    `<thead><tr><th>Level</th><th>Price</th><th>vs spot</th></tr></thead><tbody>` +
    levels.map(([k, v]) =>
      `<tr><td>${esc(k)}</td><td>${px(v)}</td>` +
      `<td class="${cls(v - p.spot)}">${signed((v - p.spot) / p.spot)}</td></tr>`
    ).join("") + `</tbody></table></div>`;

  const buckets = (p.expiry_buckets || []).length
    ? `<div class="tbl-wrap"><table class="data">` +
      `<thead><tr><th>Expiry bucket</th><th>Net GEX</th><th>Share</th>` +
      `<th>Contracts</th></tr></thead><tbody>` +
      p.expiry_buckets.map((b) =>
        `<tr><td>${esc(b.label)}</td><td class="${cls(b.net_gex)}">${usd(b.net_gex)}</td>` +
        `<td>${pct(b.abs_share, 1)}</td>` +
        `<td class="dim">${b.n_contracts.toLocaleString()}</td></tr>`).join("") +
      `</tbody></table></div>` : "";

  const footer =
    `<div class="footnote">CBOE delayed snapshot ${esc(p.snapshot_ts || "")} · ` +
    `~15-min delayed · expirations ≤ ${p.expiry_window_days || 90}d · ` +
    `${(p.n_contracts || 0).toLocaleString()} contracts · ` +
    `assumes dealers long calls / short puts.</div>`;

  if (!primary) {
    body.innerHTML = cards + `<div class="row">${levelsTbl}</div>` + footer;
    return;
  }

  body.innerHTML =
    commentaryBlock(p.commentary) + cards +
    `<div class="row">${gexChart(p)}</div>` +
    `<div class="row cards cards-2"><div>${levelsTbl}</div><div>${buckets}</div></div>` +
    `<div class="row"><div class="card-label">Charm decay — projected hedging flow ` +
    `per session</div>${charmChart(p.charm_projection)}` +
    `<div class="footnote">${esc((p.charm_projection || {}).assumption || "")} ` +
    `Monday bars absorb the weekend, so they run larger.</div></div>` +
    footer;
}

// --------------------------------------------------------------------------
// SPY dealer positioning
// --------------------------------------------------------------------------
//
// The full SPY book, not a cross-check. Its chart deliberately differs from the
// SPX one above: SPX splits call and put gamma to either side of the axis,
// whereas this shows their *sum* per strike — one bar, green where dealers are
// net long gamma at that price and red where they are net short. The split view
// answers "what is stacked here"; the net view answers "which way does hedging
// push if price gets here", which is the question the level ladder below is
// asking. Geometry is otherwise identical to gexChart so the two panels read as
// siblings side by side.

function spyNetGexChart(p) {
  const chart = (p.chart || []).slice().sort((a, b) => b.strike - a.strike);
  if (!chart.length) return `<p class="empty-note">No strike data in window.</p>`;

  // net_gex is served by the panel; fall back for payloads cached before it
  // existed, so a stale row renders rather than blanking the chart.
  const netOf = (c) => (c.net_gex != null ? c.net_gex
    : (c.call_gex || 0) + (c.put_gex || 0));

  const n = chart.length;
  const rowH = 18, padTop = 24, padBot = 12;
  const H = padTop + padBot + n * rowH;
  const W = 1000, cx = 500, half = 415, gutter = 42;
  let maxAbs = 0;
  chart.forEach((c) => { maxAbs = Math.max(maxAbs, Math.abs(netOf(c))); });
  maxAbs = maxAbs || 1;

  const strikes = chart.map((c) => c.strike);
  const maxK = strikes[0], minK = strikes[n - 1];
  const yFirst = padTop + rowH / 2, yLast = padTop + (n - 1) * rowH + rowH / 2;
  const yOf = (price) => {
    if (price == null || maxK === minK) return null;
    const f = Math.min(1, Math.max(0, (maxK - price) / (maxK - minK)));
    return yFirst + f * (yLast - yFirst);
  };

  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" ` +
    `aria-label="Net dealer gamma exposure per strike; positive right in green, ` +
    `negative left in red">`;
  svg += `<line x1="${cx}" y1="${padTop - 8}" x2="${cx}" y2="${H - padBot + 4}" class="gex-axis"/>`;

  const bucket = p.bucket || 5;
  const magnets = new Set((p.oi_magnets || []).map(
    (m) => Math.round(m.strike / bucket) * bucket));

  chart.forEach((c, i) => {
    const y = padTop + i * rowH + rowH / 2;
    const net = netOf(c);
    const w = (Math.abs(net) / maxAbs) * half;
    const pos = net >= 0;
    const x = pos ? cx + gutter : cx - gutter - w;
    if (w > 0.5)
      svg += `<rect class="${pos ? "gex-net-pos" : "gex-net-neg"}" x="${x}" ` +
        `y="${y - rowH * 0.32}" width="${w}" height="${rowH * 0.64}">` +
        `<title>${px(c.strike, 0)} net gamma ${usd(net)} ` +
        `(calls ${usd(c.call_gex)}, puts ${usd(c.put_gex)})</title></rect>`;
    svg += `<text class="gex-strike" x="${cx}" y="${y + 3}" text-anchor="middle">` +
      `${px(c.strike, 0)}</text>`;
    if (magnets.has(Math.round(c.strike / bucket) * bucket)) {
      const mx = pos ? x + w + 8 : x - 8;
      svg += `<circle class="gex-magnet" cx="${mx}" cy="${y}" r="3">` +
        `<title>High open-interest magnet</title></circle>`;
    }
  });

  const ys = yOf(p.spot);
  if (ys != null) {
    svg += `<line class="gex-spot" x1="30" y1="${ys}" x2="${W - 20}" y2="${ys}"/>`;
    svg += `<text class="gex-spot-lbl" x="34" y="${ys - 4}">Spot ${px(p.spot)}</text>`;
  }
  const yz = yOf(p.zero_gamma);
  if (yz != null) {
    svg += `<line class="gex-zero" x1="30" y1="${yz}" x2="${W - 20}" y2="${yz}"/>`;
    svg += `<text class="gex-zero-lbl" x="${W - 24}" y="${yz - 4}" text-anchor="end">` +
      `Flip ${px(p.zero_gamma)}</text>`;
  }
  const yc = yOf(p.call_wall);
  if (yc != null)
    svg += `<text class="gex-wall call" x="${W - 24}" y="${yc + 3}" text-anchor="end">` +
      `Call wall</text>`;
  const yp = yOf(p.put_wall);
  if (yp != null)
    svg += `<text class="gex-wall put" x="20" y="${yp + 3}">Put wall</text>`;

  svg += `</svg>` +
    `<div class="footnote">One bar per strike: call gamma plus put gamma. ` +
    `<span class="lgd-pos">Green</span> = dealers net long gamma there ` +
    `(hedging damps moves through it); <span class="lgd-neg">red</span> = net ` +
    `short (hedging amplifies). The flip is where the running total crosses zero.` +
    `</div>`;
  return svg;
}

function renderSpyPositioning(body, p) {
  if (!p || p.spot == null) {
    body.innerHTML = `<p class="empty-note">No data yet — press ` +
      `<strong>Refresh</strong> to fetch the CBOE SPY chain.</p>`;
    return;
  }
  const pos = p.regime === "positive";
  const flipSub = p.zero_gamma == null ? "no flip in ±8%" : `Flip ${px(p.zero_gamma)}`;
  const dexShort = (p.dex || 0) < 0;

  const cards =
    `<div class="row cards cards-4">` +
    card("Regime", pos ? "Positive" : "Negative", flipSub, pos ? "pos" : "neg") +
    card("Spot vs flip", p.cushion_pct == null ? "—" : signed(p.cushion_pct),
         `Spot ${px(p.spot)}`, cls(p.cushion_pct)) +
    card("Net GEX", usd(p.net_gex), "per 1% move", cls(p.net_gex)) +
    card("DEX bias", usd(p.dex),
         dexShort ? "dealers net short delta" : "dealers net long delta",
         cls(p.dex)) +
    `</div>`;

  const vannaSub = p.vanna_pressure == null ? "—"
    : (p.vanna_pressure >= 0
      ? "falling IV forces dealer buying"
      : "rising IV forces dealer selling");
  const charmSub = (p.charm_drift || 0) >= 0
    ? "drift to buy into the close"
    : "drift to sell into the close";

  const cards2 =
    `<div class="row cards cards-3">` +
    card("Vanna", usd(p.vanna_pressure), vannaSub, cls(p.vanna_pressure)) +
    card("Charm drift", `${usd(p.charm_drift)}/day`, charmSub, cls(p.charm_drift)) +
    card("0DTE gamma", pct(p.zero_dte_gamma_share, 0), "of visible gamma",
         (p.zero_dte_gamma_share || 0) > 0.35 ? "warn" : "") +
    `</div>`;

  const levels = [];
  if (p.call_wall != null) levels.push(["Call wall", p.call_wall]);
  (p.oi_magnets || []).forEach((m) => levels.push(["OI magnet", m.strike]));
  if (p.zero_gamma != null) levels.push(["Zero gamma", p.zero_gamma]);
  if (p.put_wall != null) levels.push(["Put wall", p.put_wall]);
  levels.push(["Spot", p.spot]);
  levels.sort((a, b) => b[1] - a[1]);
  const levelsTbl = `<div class="tbl-wrap"><table class="data">` +
    `<thead><tr><th>Level</th><th>Price</th><th>vs spot</th></tr></thead><tbody>` +
    levels.map(([k, v]) =>
      `<tr><td>${esc(k)}</td><td>${px(v)}</td>` +
      `<td class="${cls(v - p.spot)}">${signed((v - p.spot) / p.spot)}</td></tr>`
    ).join("") + `</tbody></table></div>`;

  const footer =
    `<div class="footnote">CBOE delayed snapshot ${esc(p.snapshot_ts || "")} · ` +
    `~15-min delayed · expirations ≤ ${p.expiry_window_days || 90}d · ` +
    `${(p.n_contracts || 0).toLocaleString()} contracts · ` +
    `assumes dealers long calls / short puts.</div>`;

  body.innerHTML =
    commentaryBlock(p.commentary) + cards +
    `<div class="row">${spyNetGexChart(p)}</div>` +
    cards2 + `<div class="row">${levelsTbl}</div>` + footer;
}

// --------------------------------------------------------------------------
// CFTC trader positioning
// --------------------------------------------------------------------------

// A 0-100 horizontal gauge: state colours the fill, the number is the 3-yr
// percentile. `secondary` renders the thinner Asset-Managers variant.
function cftcGauge(label, block, secondary) {
  if (!block) return "";
  const pctl = block.pctl == null ? 0 : block.pctl;
  const w = Math.max(0, Math.min(100, pctl));
  const state = block.state || "neutral";
  const klass = secondary ? "cftc-gauge secondary" : "cftc-gauge";
  const aria = `${label}: ${pctl.toFixed(0)}th percentile over 3 years, ` +
    `net ${fmtNum(block.net)} contracts`;
  return `<div class="cftc-gauge-row">` +
    `<div class="cftc-gauge-label">${esc(label)}</div>` +
    `<div class="${klass}" role="img" aria-label="${esc(aria)}">` +
      `<div class="cftc-gauge-fill s-${esc(state)}" style="width:${w}%"></div>` +
    `</div>` +
    `<div class="cftc-gauge-num">${pctl.toFixed(0)}</div></div>`;
}

// Compact net-position-vs-price sparkline (dual independent scales) so a
// price/positioning divergence is visible at a glance.
function cftcSpark(series) {
  const pts = (series || []).filter((d) => d.lev_net != null);
  if (pts.length < 2) return "";
  const W = 1000, H = 90, padL = 4, padR = 4, padT = 8, padB = 8;
  const n = pts.length;
  const xs = (i) => padL + (i / (n - 1)) * (W - padL - padR);

  const nets = pts.map((d) => d.lev_net);
  const nmin = Math.min(...nets, 0), nmax = Math.max(...nets, 0);
  const nrange = (nmax - nmin) || 1;
  const yNet = (v) => padT + (1 - (v - nmin) / nrange) * (H - padT - padB);

  const prices = pts.map((d) => d.price).filter((v) => v != null);
  let pricePath = "";
  if (prices.length >= 2) {
    const pmin = Math.min(...prices), pmax = Math.max(...prices);
    const pr = (pmax - pmin) || 1;
    const yP = (v) => padT + (1 - (v - pmin) / pr) * (H - padT - padB);
    pricePath = pts.map((d, i) =>
      d.price == null ? null
        : `${i === 0 ? "M" : "L"}${xs(i).toFixed(1)},${yP(d.price).toFixed(1)}`
    ).filter(Boolean).join(" ");
  }
  const netPath = pts.map((d, i) =>
    `${i === 0 ? "M" : "L"}${xs(i).toFixed(1)},${yNet(d.lev_net).toFixed(1)}`
  ).join(" ");
  const yZero = yNet(0).toFixed(1);

  let svg = `<svg class="cftc-spark" viewBox="0 0 ${W} ${H}" ` +
    `preserveAspectRatio="none" role="img" ` +
    `aria-label="Leveraged Funds net position versus price over the lookback window">`;
  svg += `<line class="cftc-spark-zero" x1="0" y1="${yZero}" x2="${W}" y2="${yZero}"/>`;
  if (pricePath) svg += `<path class="cftc-spark-price" d="${pricePath}"/>`;
  svg += `<path class="cftc-spark-net" d="${netPath}"/></svg>`;
  return svg +
    `<div class="cftc-spark-legend"><span class="net">— LF net</span> · ` +
    `<span class="price">— price</span></div>`;
}

function renderCftcPositioning(body, p) {
  if (!p || !p.contracts || !p.contracts.length) {
    body.innerHTML = `<p class="empty-note">No data yet — press ` +
      `<strong>Refresh</strong> to fetch the latest Commitments of Traders report.</p>`;
    return;
  }
  const cards = p.contracts.map((ct) => {
    const lev = ct.lev || {}, am = ct.am;
    const state = lev.state || "neutral";
    const flags = [];
    if (ct.flags) {
      if (ct.flags.stale) flags.push(`<span class="cftc-flag stale">stale</span>`);
      if (ct.flags.price_missing) flags.push(`<span class="cftc-flag">no price</span>`);
      if (ct.flags.short_history) flags.push(`<span class="cftc-flag">short history</span>`);
    }
    const wowSign = lev.wow > 0 ? "+" : "";
    const netLine = `${lev.net >= 0 ? "net-long" : "net-short"} ` +
      `${fmtNum(Math.abs(lev.net || 0))} · WoW ${wowSign}${fmtNum(lev.wow)}`;
    const amLine = am
      ? `<div class="cftc-meta sub">Asset Managers (real money): net ` +
        `${am.net >= 0 ? "+" : ""}${fmtNum(am.net)} · secondary read</div>`
      : "";
    return `<div class="cftc-contract">` +
      `<div class="cftc-contract-head">` +
        `<span class="cftc-contract-label">${esc(ct.label)}</span>${flags.join("")}</div>` +
      `<div class="cftc-verdict s-${esc(state)}">${esc(lev.verdict || "")}</div>` +
      cftcGauge("Leveraged Funds", lev, false) +
      `<div class="cftc-meta">${esc(netLine)}</div>` +
      `<p class="cftc-sentence">${esc(lev.sentence || "")}</p>` +
      (am ? cftcGauge("Asset Managers", am, true) : "") +
      amLine +
      cftcSpark(ct.series) +
    `</div>`;
  }).join("");

  body.innerHTML = `<div class="cftc-grid">${cards}</div>` +
    `<div class="footnote">As of ${esc(p.as_of || "")} · ${esc(p.caveat || "")}</div>`;
}

// --------------------------------------------------------------------------
// VIX term structure
// --------------------------------------------------------------------------

function vixCurve(p) {
  const fut = (p.futures_curve || []).filter((f) => f.days <= 200);
  const idx = p.index_curve || [];
  if (!fut.length && !idx.length) return "";

  const W = 1000, H = 240, padL = 46, padR = 16, padT = 16, padB = 34;
  const all = fut.map((f) => ({ d: f.days, v: f.forward }))
    .concat(idx.map((i) => ({ d: i.days, v: i.value })))
    .concat([{ d: 0, v: p.spot_vix }]);
  const maxD = Math.max(...all.map((a) => a.d), 30);
  const minV = Math.min(...all.map((a) => a.v));
  const maxV = Math.max(...all.map((a) => a.v));
  const pad = (maxV - minV) * 0.15 || 1;
  const lo = minV - pad, hi = maxV + pad;
  const X = (d) => padL + (d / maxD) * (W - padL - padR);
  const Y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);

  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" ` +
    `aria-label="VIX term structure: futures forwards and constant-maturity indices">`;

  for (let g = 0; g <= 4; g++) {
    const v = lo + (hi - lo) * g / 4;
    svg += `<line class="curve-grid" x1="${padL}" y1="${Y(v)}" x2="${W - padR}" y2="${Y(v)}"/>`;
    svg += `<text class="curve-lbl" x="${padL - 6}" y="${Y(v) + 3}" text-anchor="end">` +
      `${v.toFixed(1)}</text>`;
  }

  if (idx.length > 1) {
    const d = idx.map((i, k) => `${k ? "L" : "M"}${X(i.days).toFixed(1)},${Y(i.value).toFixed(1)}`).join(" ");
    svg += `<path class="curve-line index" d="${d}"/>`;
    idx.forEach((i) => {
      svg += `<circle class="curve-dot" cx="${X(i.days)}" cy="${Y(i.value)}" r="3" ` +
        `stroke="#41c8ff"><title>${esc(i.label)} ${i.value.toFixed(2)} (${i.days}d)</title></circle>`;
    });
  }

  if (fut.length) {
    const d = fut.map((f, k) => `${k ? "L" : "M"}${X(f.days).toFixed(1)},${Y(f.forward).toFixed(1)}`).join(" ");
    svg += `<path class="curve-line ${esc(p.structure)}" d="${d}"/>`;
    fut.forEach((f) => {
      const stroke = p.structure === "backwardation" ? "#ff5247"
        : p.structure === "flat" ? "#f2c14e" : "#33d36b";
      svg += `<circle class="curve-dot" cx="${X(f.days)}" cy="${Y(f.forward)}" ` +
        `r="${f.is_monthly ? 4 : 2.5}" stroke="${stroke}">` +
        `<title>${f.expiry} (${f.days}d) forward ${f.forward.toFixed(2)}` +
        `${f.is_monthly ? " — monthly" : ""}</title></circle>`;
      if (f.is_monthly && f.days <= 120)
        svg += `<text class="curve-lbl" x="${X(f.days)}" y="${Y(f.forward) - 9}" ` +
          `text-anchor="middle">${f.forward.toFixed(2)}</text>`;
    });
  }

  // Spot VIX marker at day 0.
  svg += `<circle class="spark-now" cx="${X(0)}" cy="${Y(p.spot_vix)}" r="4">` +
    `<title>Spot VIX ${p.spot_vix.toFixed(2)}</title></circle>`;
  svg += `<text class="curve-lbl" x="${X(0) + 6}" y="${Y(p.spot_vix) - 8}">spot</text>`;

  [0, 30, 60, 90, 120, 150, 180].filter((d) => d <= maxD).forEach((d) => {
    svg += `<text class="curve-lbl" x="${X(d)}" y="${H - padB + 15}" ` +
      `text-anchor="middle">${d}d</text>`;
  });
  svg += `</svg>` +
    `<div class="footnote">Solid = VX forwards from put-call parity on the VIX ` +
    `option chain (large dots are monthlies). Dashed cyan = constant-maturity ` +
    `index family.</div>`;
  return svg;
}

function renderVix(body, p) {
  if (!p || p.spot_vix == null) {
    body.innerHTML = `<p class="empty-note">No data yet — press <strong>Refresh</strong>.</p>`;
    return;
  }
  const f = p.flags || {};
  const structClass = p.structure === "contango" ? "pos"
    : p.structure === "backwardation" ? "neg" : "warn";
  const cards =
    `<div class="row cards cards-4">` +
    card("Structure", esc(p.structure), `basis ${p.basis == null ? "—" : p.basis.toFixed(2)}`,
         structClass) +
    card("Spot VIX", px(p.spot_vix), `CM30 ${px(p.cm30)}`, "") +
    card("VIX / VIX3M", p.vix_vix3m == null ? "—" : p.vix_vix3m.toFixed(3),
         f.vix3m_inverted ? "inverted — stress" : "below 1 — calm",
         f.vix3m_inverted ? "neg" : "pos") +
    card("VVIX", px(p.vvix, 1),
         f.vvix_elevated ? "tail being bid" : (f.vvix_calm ? "little tail bid" : "neutral"),
         f.vvix_elevated ? "warn" : "") +
    `</div>`;

  const chips = `<div class="row chips">` +
    `<span class="chip info">VIX1D<span class="ct">${px(p.vix1d, 2)}</span></span>` +
    `<span class="chip info">VIX9D<span class="ct">${px(p.vix9d, 2)}</span></span>` +
    `<span class="chip info">VIX3M<span class="ct">${px(p.vix3m, 2)}</span></span>` +
    `<span class="chip info">VIX6M<span class="ct">${px(p.vix6m, 2)}</span></span>` +
    (p.vx1 ? `<span class="chip">VX1 ${esc(p.vx1.expiry)}` +
      `<span class="ct">${px(p.vx1.forward)}</span></span>` : "") +
    (p.vx2 ? `<span class="chip">VX2 ${esc(p.vx2.expiry)}` +
      `<span class="ct">${px(p.vx2.forward)}</span></span>` : "") +
    `<span class="chip warn">VIX expiry in ${p.days_to_vix_expiry}d</span>` +
    `</div>`;

  body.innerHTML = commentaryBlock(p.commentary) + cards + chips +
    `<div class="row">${vixCurve(p)}</div>`;
}

// --------------------------------------------------------------------------
// Implied correlation
// --------------------------------------------------------------------------

function corrSpark(p) {
  const s = p.spark || [];
  if (s.length < 2) return "";
  const W = 1000, H = 130, padL = 8, padR = 8, padT = 10, padB = 18;
  const vals = s.map((d) => d.value);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const range = (hi - lo) || 1;
  const X = (i) => padL + (i / (s.length - 1)) * (W - padL - padR);
  const Y = (v) => padT + (1 - (v - lo) / range) * (H - padT - padB);

  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" ` +
    `aria-label="COR1M over the last year">`;
  const d = s.map((r, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(r.value).toFixed(1)}`).join(" ");
  svg += `<path class="spark-line" d="${d}"/>`;
  svg += `<circle class="spark-now" cx="${X(s.length - 1)}" cy="${Y(p.cor1m)}" r="3.5">` +
    `<title>${esc(s[s.length - 1].date)}: ${p.cor1m}</title></circle>`;
  svg += `<text class="curve-lbl" x="${padL}" y="${H - 4}">${esc(s[0].date)}</text>`;
  svg += `<text class="curve-lbl" x="${W - padR}" y="${H - 4}" text-anchor="end">` +
    `${esc(s[s.length - 1].date)}</text>`;
  svg += `<text class="curve-lbl" x="${padL}" y="${padT + 4}">hi ${hi.toFixed(1)}</text>`;
  svg += `<text class="curve-lbl" x="${padL}" y="${H - padB}">lo ${lo.toFixed(1)}</text>`;
  svg += `</svg>`;
  return svg;
}

function pctlBar(pctl) {
  if (pctl == null) return "";
  // Low percentile = fragile, so the low end is the alarming end (RESEARCH §5).
  const colour = pctl < 10 ? "var(--down)" : pctl < 25 ? "var(--amber)"
    : pctl > 90 ? "var(--down)" : pctl > 75 ? "var(--amber)" : "var(--up)";
  return `<div style="margin-top:6px">` +
    `<div style="height:12px;border:1px solid var(--border-lt);border-radius:3px;` +
    `background:#140f07;position:relative">` +
    `<div style="position:absolute;left:${Math.max(0, Math.min(100, pctl))}%;top:-3px;` +
    `width:2px;height:16px;background:${colour}"></div></div>` +
    `<div class="card-sub" style="display:flex;justify-content:space-between">` +
    `<span>0 — crowded dispersion</span><span>macro shock — 100</span></div></div>`;
}

function renderCorrelation(body, p) {
  if (!p || p.cor1m == null) {
    body.innerHTML = `<p class="empty-note">No data yet — press <strong>Refresh</strong>.</p>`;
    return;
  }
  const f = p.flags || {};
  const alarm = f.extreme_low || f.extreme_high || f.spiking_from_lows;
  const cards =
    `<div class="row cards cards-4">` +
    card("COR1M", px(p.cor1m), `${esc(p.regime)}`, alarm ? "warn" : "") +
    card("2y percentile", p.cor1m_pctl_2y == null ? "—" : `${p.cor1m_pctl_2y}`,
         `range ${px(p.low_2y)}–${px(p.high_2y)}`,
         f.low ? "warn" : (f.high ? "neg" : "")) +
    card("1-day change", p.cor1m_change_pct == null ? "—"
         : `${p.cor1m_change_pct > 0 ? "+" : ""}${p.cor1m_change_pct}%`,
         f.spiking_from_lows ? "unwind starting" : "from " + px(p.cor1m_prev),
         f.spiking_from_lows ? "warn" : cls(p.cor1m_change)) +
    card("COR3M", px(p.cor3m), f.term_inverted ? "1M above 3M — stress"
         : `spread ${px(p.spread)}`, f.term_inverted ? "neg" : "") +
    `</div>`;

  body.innerHTML = commentaryBlock(p.commentary) + cards +
    `<div class="row">${pctlBar(p.cor1m_pctl_2y)}</div>` +
    `<div class="row">${corrSpark(p)}</div>` +
    `<div class="footnote">Percentiles against CBOE daily history from ` +
    `${esc(p.history_start)} (${(p.history_days || 0).toLocaleString()} sessions). ` +
    `Full-series rank ${p.cor1m_pctl_all}.</div>`;
}

// --------------------------------------------------------------------------
// Volume profile
// --------------------------------------------------------------------------

// 30-minute candles on the left and the composite volume profile rotated on
// the right, sharing one price axis — the standard market-profile layout, so
// the relationship between where price went and where value was accepted reads
// directly off the chart.
function auctionChart(p) {
  const bins = p.chart || [];
  const candles = p.candles || [];
  if (!bins.length && !candles.length) return "";

  const W = 1000, H = 430;
  const padL = 56, padT = 16, padB = 42;      // padL leaves room for price labels
  const cx0 = padL, cx1 = 648;                // candle region
  const px0 = 664, px1 = 878;                 // profile region
  const labelX = px1 + 8;                     // right-hand level labels

  // --- shared price scale over both charts ------------------------------- //
  const priceVals = [];
  candles.forEach((c) => { priceVals.push(c.h, c.l); });
  bins.forEach((b) => priceVals.push(b.price));
  [p.composite_poc, p.composite_vah, p.composite_val, p.spy_spot]
    .forEach((v) => { if (v != null) priceVals.push(v); });
  (p.naked_pocs || []).forEach((n) => priceVals.push(n.spy));
  if (!priceVals.length) return "";
  let lo = Math.min(...priceVals), hi = Math.max(...priceVals);
  const pad = (hi - lo) * 0.04 || 0.5;
  lo -= pad; hi += pad;
  const Y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);

  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" ` +
    `aria-label="Thirty-minute SPY candles beside the composite volume profile, ` +
    `sharing one price axis, with value area, naked POCs and low-volume nodes">`;

  // --- price gridlines --------------------------------------------------- //
  for (let g = 0; g <= 5; g++) {
    const v = lo + (hi - lo) * g / 5;
    svg += `<line class="curve-grid" x1="${cx0}" y1="${Y(v)}" x2="${px1}" y2="${Y(v)}"/>`;
    svg += `<text class="curve-lbl" x="${padL - 6}" y="${Y(v) + 3}" ` +
      `text-anchor="end">${v.toFixed(2)}</text>`;
  }

  // --- LVN corridors span both charts ------------------------------------ //
  (p.lvn_zones || []).forEach((z) => {
    const y1 = Y(z.hi), y2 = Y(z.lo);
    svg += `<rect class="prof-lvn" x="${cx0}" y="${y1}" width="${px1 - cx0}" ` +
      `height="${Math.max(y2 - y1, 1)}"><title>LVN corridor ${z.lo}–${z.hi} ` +
      `(${Math.round(z.lo_spx)}–${Math.round(z.hi_spx)} SPX) · ` +
      `${Math.round(z.mean_volume_share * 100)}% of average volume — price ` +
      `travels through fast</title></rect>`;
  });

  // --- candles ----------------------------------------------------------- //
  const n = candles.length;
  const sessionX = {};                        // session date -> first candle x
  if (n) {
    const cw = (cx1 - cx0) / n;
    const bodyW = Math.max(1.2, Math.min(cw * 0.68, 9));

    // Shade the sessions the composite profile actually summarises.
    const compIdx = candles.map((c, i) => (c.in_composite ? i : -1))
      .filter((i) => i >= 0);
    if (compIdx.length) {
      const x = cx0 + compIdx[0] * cw;
      svg += `<rect class="prof-comp" x="${x}" y="${padT}" ` +
        `width="${(compIdx[compIdx.length - 1] + 1) * cw + cx0 - x}" ` +
        `height="${H - padT - padB}"><title>Sessions in the composite ` +
        `profile (${esc(p.composite_from)} → ${esc(p.composite_to)})</title></rect>`;
    }

    let lastDay = null;
    candles.forEach((c, i) => {
      const xc = cx0 + i * cw + cw / 2;
      if (c.d !== lastDay) {
        if (lastDay !== null)
          svg += `<line class="sess-sep" x1="${cx0 + i * cw}" y1="${padT}" ` +
            `x2="${cx0 + i * cw}" y2="${H - padB}"/>`;
        sessionX[c.d] = cx0 + i * cw;
        lastDay = c.d;
      }
      const up = c.c >= c.o;
      const k = up ? "cdl-up" : "cdl-dn";
      const yO = Y(c.o), yC = Y(c.c);
      const top = Math.min(yO, yC);
      const bh = Math.max(Math.abs(yC - yO), 0.8);
      svg += `<line class="${k}-wick" x1="${xc}" y1="${Y(c.h)}" x2="${xc}" ` +
        `y2="${Y(c.l)}"/>`;
      svg += `<rect class="${k}" x="${xc - bodyW / 2}" y="${top}" ` +
        `width="${bodyW}" height="${bh}"><title>${esc(c.t)}  O ${c.o.toFixed(2)} ` +
        `H ${c.h.toFixed(2)} L ${c.l.toFixed(2)} C ${c.c.toFixed(2)}</title></rect>`;
    });

    // Date axis — one label per session, thinned if they would collide.
    const days = Object.keys(sessionX);
    const every = Math.ceil(days.length / 10);
    days.forEach((d, i) => {
      if (i % every) return;
      svg += `<text class="curve-lbl" x="${sessionX[d] + cw / 2}" ` +
        `y="${H - padB + 15}" text-anchor="middle">${esc(d.slice(5))}</text>`;
    });
    svg += `<text class="curve-lbl" x="${cx0}" y="${H - padB + 30}">` +
      `${esc(p.candle_interval || "30m")} candles · ${n} bars · ` +
      `${days.length} sessions</text>`;
  }

  // --- volume profile ---------------------------------------------------- //
  if (bins.length) {
    const maxV = Math.max(...bins.map((b) => b.volume)) || 1;
    const barH = Math.max(1.2, (H - padT - padB) / bins.length * 0.92);
    bins.forEach((b) => {
      const w = (b.volume / maxV) * (px1 - px0);
      const inVA = p.composite_val != null && p.composite_vah != null &&
        b.price >= p.composite_val && b.price <= p.composite_vah;
      svg += `<rect class="prof-bar ${inVA ? "va" : ""}" x="${px0}" ` +
        `y="${Y(b.price) - barH / 2}" width="${Math.max(w, 0.5)}" height="${barH}">` +
        `<title>${b.price.toFixed(2)} (${Math.round(b.price_spx)} SPX) · ` +
        `${(b.share * 100).toFixed(2)}% of composite volume</title></rect>`;
    });
    svg += `<text class="curve-lbl" x="${px0}" y="${H - padB + 15}">volume at price` +
      `</text>`;
  }

  // --- level overlays across both charts --------------------------------- //
  const mark = (price, klass, label, colour) => {
    if (price == null) return "";
    const y = Y(price);
    return `<line class="${klass}" x1="${cx0}" y1="${y}" x2="${px1}" y2="${y}"/>` +
      `<text class="curve-lbl" x="${labelX}" y="${y + 3}" fill="${colour}">` +
      `${label} ${price.toFixed(2)}</text>`;
  };
  svg += mark(p.composite_vah, "prof-va", "VAH", "#9c8e76");
  svg += mark(p.composite_val, "prof-va", "VAL", "#9c8e76");
  svg += mark(p.composite_poc, "prof-poc", "POC", "#ff9e1b");
  svg += mark(p.spy_spot, "prof-spot", "spot", "#41c8ff");

  // Naked POCs run from the session that left them to the right edge, so the
  // unfinished business is visible where it was created.
  (p.naked_pocs || []).forEach((nk) => {
    const y = Y(nk.spy);
    const x = sessionX[nk.date] != null ? sessionX[nk.date] : cx0;
    svg += `<line class="prof-naked-line" x1="${x}" y1="${y}" x2="${px1}" y2="${y}"/>`;
    svg += `<circle class="prof-naked" cx="${x}" cy="${y}" r="3.5">` +
      `<title>Naked POC ${nk.spy.toFixed(2)} (${Math.round(nk.spx)} SPX) left on ` +
      `${nk.date}, never traded back through</title></circle>`;
  });

  svg += `</svg>` +
    `<div class="chart-legend">` +
    `<span><i class="sw cdl-up"></i>up candle</span>` +
    `<span><i class="sw cdl-dn"></i>down candle</span>` +
    `<span><i class="sw sw-va"></i>value area</span>` +
    `<span><i class="sw sw-lvn"></i>LVN corridor</span>` +
    `<span><i class="sw sw-naked"></i>naked POC</span>` +
    `<span><i class="sw sw-comp"></i>composite window</span>` +
    `</div>`;
  return svg;
}

function renderProfile(body, p) {
  if (!p || p.spy_spot == null) {
    body.innerHTML = `<p class="empty-note">No data yet — press <strong>Refresh</strong>.</p>`;
    return;
  }
  const cards =
    `<div class="row cards cards-4">` +
    card("SPY spot", px(p.spy_spot), `SPX ${px(p.spx_spot)} · ratio ${p.ratio}`, "") +
    card("Composite POC", px(p.composite_poc),
         `${px(p.composite_poc_spx, 0)} SPX`, "") +
    card("Value area", `${px(p.composite_val)}–${px(p.composite_vah)}`,
         `${px(p.composite_val_spx, 0)}–${px(p.composite_vah_spx, 0)} SPX`, "") +
    card("Naked POCs", String((p.naked_pocs || []).length),
         (p.lvn_zones || []).length + " LVN corridors", "") +
    `</div>`;

  const naked = (p.naked_pocs || []).length
    ? `<div class="tbl-wrap"><table class="data"><thead><tr><th>Naked POC</th>` +
      `<th>SPY</th><th>SPX</th><th>From</th></tr></thead><tbody>` +
      p.naked_pocs.map((n) =>
        `<tr><td>${n.above_spot ? "above" : "below"} spot</td><td>${px(n.spy)}</td>` +
        `<td>${px(n.spx, 0)}</td><td class="dim">${esc(n.date)}</td></tr>`).join("") +
      `</tbody></table></div>` : `<p class="empty-note">No naked POCs in the window.</p>`;

  const lvn = (p.lvn_zones || []).length
    ? `<div class="tbl-wrap"><table class="data"><thead><tr><th>LVN corridor</th>` +
      `<th>SPX range</th><th>Volume</th></tr></thead><tbody>` +
      p.lvn_zones.map((z) =>
        `<tr><td>${px(z.lo)}–${px(z.hi)}</td>` +
        `<td>${px(z.lo_spx, 0)}–${px(z.hi_spx, 0)}</td>` +
        `<td class="amber">${Math.round(z.mean_volume_share * 100)}% of avg</td></tr>`
      ).join("") + `</tbody></table></div>`
    : `<p class="empty-note">No low-volume corridors detected.</p>`;

  body.innerHTML = commentaryBlock(p.commentary) + cards +
    `<div class="row">${auctionChart(p)}</div>` +
    `<div class="row cards cards-2"><div>${naked}</div><div>${lvn}</div></div>` +
    `<div class="footnote">${esc(p.limitation || "")} ` +
    `Candles cover ${p.n_sessions} sessions; the profile and its value area ` +
    `summarise the shaded ${p.composite_sessions} ` +
    `(${esc(p.composite_from)} → ${esc(p.composite_to)}). ` +
    `Built from ${esc(p.interval)} bars, $${p.bin_size} price bins.</div>`;
}

// --------------------------------------------------------------------------
// Calendar timeline
// --------------------------------------------------------------------------

function renderCalendar(body, p) {
  if (!p || !p.events) {
    body.innerHTML = `<p class="empty-note">No data yet — press <strong>Refresh</strong>.</p>`;
    return;
  }
  const win = p.window_days || 45;
  const marks = p.events.map((e, i) => {
    const left = Math.max(1, Math.min(99, (e.days / win) * 100));
    return `<div class="tl-ev ${esc(e.kind)} ${i % 2 ? "alt" : ""}" ` +
      `style="left:${left}%">` +
      `<div class="tl-dot"></div>` +
      `<div class="tl-lbl">${esc(e.label)}</div>` +
      `<div class="tl-days">${esc(e.date)} · ${e.days}d</div></div>`;
  }).join("");

  const chips = `<div class="chips">` +
    `<span class="chip">Monthly OPEX<span class="ct">${p.days_to_opex}d</span></span>` +
    `<span class="chip info">VIX expiry<span class="ct">${p.days_to_vix_expiry}d</span></span>` +
    `<span class="chip">Triple witching<span class="ct">${p.days_to_quarterly_opex}d</span></span>` +
    `<span class="chip">Quarter end<span class="ct">${p.days_to_quarter_end}d</span></span>` +
    (p.post_opex_week
      ? `<span class="chip warn">Post-OPEX week — session ${p.sessions_since_opex}</span>`
      : "") +
    (p.spy_ex_div_warning
      ? `<span class="chip neg">SPY ex-div ${esc(p.next_spy_ex_div)} — early exercise</span>`
      : "") +
    `</div>`;

  body.innerHTML =
    `<div class="timeline"><div class="tl-axis"></div>${marks}</div>` + chips +
    `<div class="footnote">${esc(p.jheqx_note || "")} VIX expiry is the Wednesday ` +
    `30 days before the following month's third Friday — a separate event from ` +
    `SPX OPEX.</div>`;
}

// --------------------------------------------------------------------------
// Tabs + refresh wiring
// --------------------------------------------------------------------------

function setStatus(msg) { $("#status-line").textContent = msg; }

function showTab(id) {
  $$(".tab").forEach((b) => {
    const on = b.dataset.tab === id;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
  $$(".tabpanel").forEach((s) => {
    const on = s.id === "panel-" + id;
    s.classList.toggle("active", on);
    s.hidden = !on;
  });
}

async function runRefresh(endpoint, btn, label) {
  const buttons = $$(".btn");
  buttons.forEach((b) => (b.disabled = true));
  btn.setAttribute("aria-busy", "true");
  setStatus(`${label}… (the SPX chain is ~13 MB, this takes a few seconds)`);
  try {
    const res = await fetch(endpoint, { method: "POST" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const updated = await res.json();
    Object.entries(updated).forEach(([key, rec]) => renderPanel(key, rec));
    const errs = Object.entries(updated)
      .filter(([, r]) => r && r.status === "error").map(([k]) => k);
    setStatus(errs.length
      ? `${label} done — ${errs.join(", ")} failed, showing cached data.`
      : `${label} done.`);
  } catch (e) {
    setStatus(`${label} failed: ${e.message}. Cached data preserved.`);
  } finally {
    btn.removeAttribute("aria-busy");
    buttons.forEach((b) => (b.disabled = false));
  }
}

async function init() {
  $$(".tab").forEach((b) =>
    b.addEventListener("click", () => showTab(b.dataset.tab)));

  $("#btn-tab1").addEventListener("click", (e) =>
    runRefresh("/api/refresh/tab1", e.currentTarget, "Refreshing SPX regime"));

  setStatus("Loading last session…");
  try {
    const res = await fetch("/api/state");
    const state = await res.json();
    renderAll(state);
    const any = Object.values(state).some((r) => r && r.updated_at);
    setStatus(any ? "Last session restored. Press Refresh for fresh data."
                  : "No saved data yet. Press Refresh to fetch.");
  } catch (e) {
    setStatus("Could not load saved state: " + e.message);
  }

  // Keep relative timestamps fresh without re-fetching.
  setInterval(() => {
    $$("[data-updated]").forEach((el) => {
      const title = el.title;
      if (title && title.startsWith("Last updated: ")) {
        const t = Date.parse(title.slice("Last updated: ".length)) / 1000;
        if (!isNaN(t)) el.textContent = relTime(t);
      }
    });
  }, 30000);
}

document.addEventListener("DOMContentLoaded", init);
