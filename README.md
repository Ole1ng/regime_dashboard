# Regime Dashboard

A local, offline-first dashboard that combines dealer gamma positioning, delta
decay, VIX term structure, auction structure, implied correlation and the
expiration calendar into a single conditional-distribution read on SPX.

Tab 1 (**SPX REGIME**) is built. Tabs 2 and 3 are reserved placeholders.

The domain spec is [`RESEARCH.md`](RESEARCH.md) — every panel implements a
section of it. The verified data sources are in
[`DATA_SOURCES.md`](DATA_SOURCES.md).

---

## Running it

```bash
pip install -r requirements.txt
python app.py
```

Then open <http://localhost:8020>.

Nothing auto-refreshes. Press **Refresh** to pull fresh data (~15 s — the SPX
chain alone is 13 MB). Everything is persisted to SQLite, so reopening the page
renders the last session immediately.

All data sources are free and unauthenticated. There is no API key, no account,
and no LLM anywhere in the pipeline — every piece of analysis is deterministic
and rule-based.

---

## What the panels show

Panels appear on Tab 1 in this order, reading from the regime call down to the
context that produced it:

| Panel | Question it answers | RESEARCH.md |
|---|---|---|
| **Regime** | Which of the four regimes is in force, what would invalidate it, and which price levels have independent confirmation | Composing Them |
| **SPX Dealer Positioning** | Is the tape self-damping or self-amplifying; where are the walls; what flow is coming | §1, §2 |
| **SPY Dealer Positioning** | The same read on the retail/ETF book — an independent second opinion, and a confluence lens in its own right | §1, §2 |
| **Auction Structure** | Where participants agreed on value, and which corridors price travels fast | §4 |
| **CFTC Trader Positioning** | Where speculative and real-money futures traders are positioned, and whether that is crowded | — |
| **VIX Term Structure** | Contango or backwardation — is vol supply being paid or punished | §3 |
| **Implied Correlation** | How much single-stock energy reaches the index, and is dispersion crowded | §5 |
| **Expiration Calendar** | What is scheduled, and when the mechanical drift dies | §6, §3 |

The two dealer-positioning panels sit side by side at equal width and use
deliberately different charts. SPX splits call and put gamma to either side of
the axis — "what is stacked at this strike". SPY plots their **sum**, one bar
per strike, green where dealers are net long gamma and red where they are net
short — "which way does hedging push if price gets here". The flip is where the
running total crosses zero, so it reads directly off the colour change.

CFTC positioning is the one panel the regime classifier does not consume: COT
data is weekly, released on a ~3-day lag, and cannot inform a same-session
regime call. It is slow-moving context, not an input.

The auction panel uses the standard market-profile layout: 30-minute SPY candles
on the left, the composite volume profile rotated on the right, both on **one
shared price axis**, so the relationship between where price went and where
value was accepted reads directly off the chart. Candles cover all 10 profiled
sessions; the 5 the composite actually summarises are shaded. POC, VAH, VAL and
spot run across both halves; naked POCs are drawn from the session that left
them to the right edge; LVN corridors are shaded bands.

### The regime layer

Four states, from the research table:

- **PIN & GRIND** — positive gamma, contango, nothing destabilising. Realised
  vol suppressed, rotation inside value.
- **UNSTABLE PIN** — the pin still holds intraday, but something is undermining
  it (thin cushion to the flip, correlation spiking off the lows, curve
  flattening, post-OPEX reset).
- **ACCELERATION** — below the flip with backwardation or elevated correlation.
  Do not fade.
- **REFLEXIVE REPAIR** — still negative gamma, but the curve has re-steepened
  and correlation is calm: the conditions for a vanna/charm melt-up.

Confidence is scored on the regime *family* (pin vs trend), because UNSTABLE PIN
is PIN & GRIND plus a destabiliser — a vote for one is not evidence against the
other. What makes a state unstable is listed separately as `destabilisers`.

If an input panel fails, the regime is still classified from what remains, the
missing inputs are named on the panel, and confidence is reduced.

### Confluence scoring

Candidate levels (gamma walls, the flip, OI magnets, the SPY book, value-area
edges, naked POCs) are clustered within 0.2% of spot and scored by how many
**independent** lenses support them. Three or more marks a trade location; fewer
is a note. A level sitting inside a low-volume corridor is tagged `LVN` —
nobody agreed on value there, so it is *weakened*, not confirmed.

---

## Notable implementation details

Several things here deviate from the obvious implementation because running
against live data showed the obvious version was wrong. Each is covered by a
regression test.

- **Charm is published as a hedging flow, not a book-delta derivative.**
  `sign * charm * notional` gives the change in the dealer's *option book*
  delta; the market-facing flow is its negative. Dealers short an OTM put are
  long delta, hedge by shorting stock, and buy that stock back as the put
  decays — book delta falls while the flow is positive. Publishing the raw
  derivative as a flow inverts the canonical charm tailwind.

- **The charm headline is computed by repricing, not by the instantaneous
  rate.** Charm diverges as `T → 0`, so 0DTE contracts (≈11% of the live SPX
  book) swamp an instantaneous sum. The headline is the first session of the
  projection, so the card and the chart cannot disagree.

- **Walls are constrained to their own side of spot.** SPX's heavy 8000 strike
  otherwise became both the call and the put wall, giving a 0.00% wall-to-wall
  range and a put wall above spot.

- **LVN corridors require a shelf on both sides.** Otherwise the "thinnest
  corridor" is always the tail at the top or bottom of the profile, where
  volume is light purely because price just got there.

- **VIX forwards come from put-call parity on the VIX option chain.** Every
  direct CBOE VX futures endpoint returns 403. VIX options are priced off VIX
  futures, so `F = K + e^(rT)(C − P)` at the most ATM strike recovers the curve;
  it agrees with CFE monthly settlements to within ~0.2 vol points.

- **Settlement agreement is measured on monthlies only.** Untraded weeklies all
  repeat a single reference price, which otherwise raises a false disagreement
  warning every session.

- **Low implied correlation reads as fragility, not calm** — and the actionable
  signal is a sharp rise off a low base, not the level itself.

- **Price bin indices round before flooring.** `99.8 / 0.1` is
  `997.9999999999999` in IEEE and naively floors to 997, shifting a whole
  session's profile by one bin and relocating its POC.

- **The SPY panel kept the `gamma_spy` key when its module changed.**
  `spy_positioning.py` replaced `gamma_engine.refresh(SPY)` behind the same
  panel key, because `regime.confluence` reads `spot` / `call_wall` /
  `put_wall` off that key to score the SPY book as an independent lens. Renaming
  the key would not raise — the level would just quietly stop scoring — so
  `test_spy_positioning.py` asserts those fields exist *and* runs the payload
  through `regime.confluence` to prove "SPY book" still appears.
  `gamma_engine.SPY` still exists: the tests use it to check that the SPX
  thresholds are scale-separated from SPY's.

---

## Layout

```
app.py                  FastAPI entry point (port 8020)
store.py                SQLite persistence, one JSON payload per panel
panels/
  _bs.py                Black-Scholes greeks (vanna/charm/gamma/delta)
  gamma_engine.py       SPX dealer positioning, charm projection
  spy_positioning.py    SPY dealer positioning (panel key `gamma_spy`)
  cftc_positioning.py   CFTC Commitments of Traders, 3-year normalised
  vix_structure.py      VX forwards via put-call parity, term structure
  correlation.py        COR1M/COR3M with 2006-present percentiles
  volume_profile.py     SPY auction structure, naked POCs, LVN corridors
  calendar_context.py   OPEX / VIX expiry / ex-div / quarter-end rules
  regime.py             Regime classifier + confluence scorer (pure)
  test_*.py             One test module per panel
static/                 index.html, app.js, style.css (no frameworks, no CDN)
tools/
  probe_sources.py      Endpoint verification
  probe_vx.py           VIX futures alternatives
  calibrate.py          Live magnitudes vs commentary thresholds
  smoke.py              Run any panel's refresh() against live data
  check_contract.py     Assert live payloads carry every field app.js reads
  render_check.js       Execute the renderers and validate their SVG output
```

## Tests

```bash
python -m pytest panels/ -q          # 196 tests, no network
python tools/check_contract.py       # frontend field contract (server running)
node tools/render_check.js           # renderer output + chart geometry
python tools/calibrate.py            # live magnitudes vs thresholds
python tools/smoke.py [panel ...]    # one panel's refresh() against live data
```

`render_check.js` runs the real renderers against live payloads under a stubbed
DOM and checks for NaN coordinates, `undefined` in labels, unbalanced SVG tags,
negative dimensions, and — for the auction chart — that the candle and profile
regions stay disjoint. Those failures are invisible without it.

Panel modules are pure functions over fetched data, so the whole suite runs
offline against synthetic fixtures. Fixtures are anchored to values actually
observed on 2026-08-04 wherever a real-world shape matters.

## Thresholds

Commentary thresholds in `gamma_engine.Underlying` are calibrated against a live
snapshot (SPX 7710 / SPY 768) and dated in the docstring. They are scale- and
regime-dependent — re-run `tools/calibrate.py` and revisit them periodically.

## Caveats

The panel carries its own expandable caveats section, and it is worth reading.
The short version: the strong result is on **volatility, not direction**; the
dealer-positioning assumption (long calls, short puts) is a heuristic that
everything downstream inherits; these are probability tilts, not signals.
