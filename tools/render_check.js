// Execute the real renderers against live payloads and check their output.
//
//     python app.py                    # in another shell
//     node tools/render_check.js
//
// The browser is the only place this code normally runs, so without a visual
// check the likely failure modes are silent: a NaN coordinate, an `undefined`
// interpolated into a label, or unbalanced SVG tags. This catches all three.

const fs = require("fs");
const path = require("path");
const http = require("http");

// Minimal DOM stub — app.js only touches document at load time to register the
// DOMContentLoaded handler and to build the $ / $$ helpers.
const noop = () => {};
global.document = {
  addEventListener: noop,
  querySelector: () => null,
  querySelectorAll: () => [],
  getElementById: () => null,
};

const src = fs.readFileSync(
  path.join(__dirname, "..", "static", "app.js"), "utf8");
const api = new Function(
  src + "\n; return { auctionChart, gexChart, charmChart, vixCurve, corrSpark," +
        " pctlBar, confluenceTable, renderProfile, renderRegime, renderGamma," +
        " renderVix, renderCorrelation, renderCalendar };")();

function fetchState() {
  return new Promise((resolve, reject) => {
    http.get("http://127.0.0.1:8020/api/state", (res) => {
      let raw = "";
      res.on("data", (d) => (raw += d));
      res.on("end", () => resolve(JSON.parse(raw)));
    }).on("error", reject);
  });
}

let failures = 0;

function check(name, html) {
  const problems = [];
  if (/NaN/.test(html)) problems.push("contains NaN");
  if (/undefined/.test(html)) problems.push("contains 'undefined'");
  if (/\bnull\b/.test(html)) problems.push("contains bare 'null'");

  // Tag balance for the elements we generate.
  for (const tag of ["svg", "rect", "line", "text", "circle", "path", "g"]) {
    const open = (html.match(new RegExp(`<${tag}[\\s>]`, "g")) || []).length;
    const selfClose = (html.match(new RegExp(`<${tag}[^>]*/>`, "g")) || []).length;
    const close = (html.match(new RegExp(`</${tag}>`, "g")) || []).length;
    if (open !== selfClose + close)
      problems.push(`<${tag}> unbalanced: ${open} open, ${close} closed, ${selfClose} self-closed`);
  }
  // Numeric attributes must be finite. The leading \s matters: without it
  // `viewBox="0 0 1000 430"` matches as an `x` attribute, since "viewBox" ends
  // in an x.
  const attrs = html.match(/\s(?:x|y|x1|y1|x2|y2|cx|cy|r|width|height)="([^"]*)"/g) || [];
  for (const a of attrs) {
    const v = a.split('="')[1].slice(0, -1);
    if (v !== "" && !Number.isFinite(Number(v)) && !/%$/.test(v)) {
      problems.push(`non-numeric attribute ${a}`);
      break;
    }
  }
  // Negative width/height are invalid SVG and render as nothing.
  const neg = (html.match(/(?:width|height|r)="(-[\d.]+)"/g) || []);
  if (neg.length) problems.push(`negative dimension: ${neg.slice(0, 3).join(", ")}`);

  if (problems.length) {
    failures++;
    console.log(`  FAIL  ${name}\n          ${problems.join("\n          ")}`);
  } else {
    console.log(`  ok    ${name}  (${html.length.toLocaleString()} chars)`);
  }
}

(async () => {
  const state = await fetchState();
  const P = (k) => (state[k] && state[k].payload) || null;

  console.log("charts:");
  check("auctionChart", api.auctionChart(P("volume_profile")));
  check("gexChart (SPX)", api.gexChart(P("gamma_spx")));
  check("charmChart (SPX)", api.charmChart(P("gamma_spx").charm_projection));
  check("vixCurve", api.vixCurve(P("vix_structure")));
  check("corrSpark", api.corrSpark(P("correlation")));
  check("pctlBar", api.pctlBar(P("correlation").cor1m_pctl_2y));
  check("confluenceTable",
        api.confluenceTable(P("regime").confluence, P("regime").spot));

  // Geometry of the combined auction chart: candles must stay in the left
  // region and profile bars in the right one, or they overlap into mush. This
  // stands in for the visual check.
  console.log("\nauction chart geometry:");
  {
    const vp = P("volume_profile");
    const svg = api.auctionChart(vp);
    const CX0 = 56, CX1 = 648, PX0 = 664, PX1 = 878;

    const wicks = [...svg.matchAll(/class="cdl-(?:up|dn)-wick" x1="([\d.]+)"/g)]
      .map((m) => Number(m[1]));
    const bodies = [...svg.matchAll(/class="cdl-(?:up|dn)" x="([\d.]+)" y="[\d.]+" width="([\d.]+)"/g)]
      .map((m) => ({ x: Number(m[1]), w: Number(m[2]) }));
    const bars = [...svg.matchAll(/class="prof-bar[^"]*" x="([\d.]+)" y="[\d.]+" width="([\d.]+)"/g)]
      .map((m) => ({ x: Number(m[1]), w: Number(m[2]) }));

    const problems = [];
    if (wicks.length !== vp.candles.length)
      problems.push(`${wicks.length} wicks for ${vp.candles.length} candles`);
    if (bodies.length !== vp.candles.length)
      problems.push(`${bodies.length} bodies for ${vp.candles.length} candles`);
    if (bars.length !== vp.chart.length)
      problems.push(`${bars.length} profile bars for ${vp.chart.length} bins`);

    const bodyMin = Math.min(...bodies.map((b) => b.x));
    const bodyMax = Math.max(...bodies.map((b) => b.x + b.w));
    if (bodyMin < CX0 - 1 || bodyMax > CX1 + 1)
      problems.push(`candles span ${bodyMin.toFixed(1)}–${bodyMax.toFixed(1)}, outside [${CX0}, ${CX1}]`);

    const barMax = Math.max(...bars.map((b) => b.x + b.w));
    if (bars.some((b) => b.x !== PX0))
      problems.push("profile bars do not all start at the profile origin");
    if (barMax > PX1 + 1)
      problems.push(`profile bars reach ${barMax.toFixed(1)}, past ${PX1}`);
    if (bodyMax > PX0)
      problems.push("candle region overlaps the profile region");

    const naked = (svg.match(/class="prof-naked-line"/g) || []).length;
    if (naked !== (vp.naked_pocs || []).length)
      problems.push(`${naked} naked-POC lines for ${(vp.naked_pocs || []).length} POCs`);
    const lvn = (svg.match(/class="prof-lvn"/g) || []).length;
    if (lvn !== (vp.lvn_zones || []).length)
      problems.push(`${lvn} LVN bands for ${(vp.lvn_zones || []).length} zones`);
    for (const lbl of ["POC", "VAH", "VAL", "spot"])
      if (!svg.includes(`>${lbl} `)) problems.push(`missing ${lbl} label`);
    if (!svg.includes('class="prof-comp"')) problems.push("missing composite shading");

    if (problems.length) {
      failures++;
      problems.forEach((p) => console.log(`  FAIL  ${p}`));
    } else {
      console.log(`  ok    ${bodies.length} candles in [${bodyMin.toFixed(0)}, ` +
        `${bodyMax.toFixed(0)}], ${bars.length} profile bars in [${PX0}, ` +
        `${barMax.toFixed(0)}], regions disjoint`);
      console.log(`  ok    ${naked} naked-POC lines, ${lvn} LVN bands, ` +
        `POC/VAH/VAL/spot labelled, composite window shaded`);
    }
  }

  console.log("\nfull panel bodies:");
  const body = () => ({ innerHTML: "" });
  const panels = [
    ["regime", api.renderRegime, P("regime")],
    ["calendar", api.renderCalendar, P("calendar")],
    ["gamma_spx", (b, p) => api.renderGamma(b, p, true), P("gamma_spx")],
    ["gamma_spy", (b, p) => api.renderGamma(b, p, false), P("gamma_spy")],
    ["vix_structure", api.renderVix, P("vix_structure")],
    ["correlation", api.renderCorrelation, P("correlation")],
    ["volume_profile", api.renderProfile, P("volume_profile")],
  ];
  for (const [name, fn, payload] of panels) {
    const b = body();
    try {
      fn(b, payload);
      check(name, b.innerHTML);
    } catch (e) {
      failures++;
      console.log(`  FAIL  ${name} threw: ${e.message}`);
    }
  }

  console.log("\nempty-payload safety:");
  for (const [name, fn] of panels) {
    const b = body();
    try {
      fn(b, null);
      if (!b.innerHTML) { failures++; console.log(`  FAIL  ${name}: rendered nothing`); }
      else console.log(`  ok    ${name} (null payload handled)`);
    } catch (e) {
      failures++;
      console.log(`  FAIL  ${name} threw on null: ${e.message}`);
    }
  }

  console.log(failures ? `\n${failures} FAILURES` : "\nAll render checks passed.");
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error("harness error:", e); process.exit(2); });
