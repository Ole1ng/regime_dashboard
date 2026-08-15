# Regime Dashboard

A local, offline-first dashboard in two parts:

- **Tab 1 — SPX REGIME** combines dealer gamma positioning, delta decay, VIX
  term structure, auction structure, implied correlation and the expiration
  calendar into a single conditional-distribution read on SPX.
- **Tab 2 — TICKER SENTIMENT** answers the same kind of question for one US
  equity: type a ticker, and get dealer positioning, options-derived fear
  gauges, short interest and ownership, news and retail sentiment, event risk,
  and the divergences between them.

Tab 3 remains a reserved placeholder.

The domain spec is [`RESEARCH.md`](RESEARCH.md) — every Tab 1 panel implements a
section of it. The verified data sources are in
[`DATA_SOURCES.md`](DATA_SOURCES.md).

---

## Running it

```bash
pip install -r requirements.txt
python app.py
```

Then open <http://localhost:8020>.

Nothing auto-refreshes. On Tab 1, press **Refresh** to pull fresh data (~15 s —
the SPX chain alone is 13 MB). On Tab 2, type a ticker and press **Analyse**
(~10 s across five sources). Everything is persisted to SQLite, so reopening the
page renders the last session immediately.

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

## Tab 2 — Ticker Sentiment

One ticker input, one **Analyse** button, seven panels. Three network fetches
feed all of them: the CBOE chain (positioning, volatility, implied move), one
Finviz quote page (short interest, ownership, analyst, earnings date, and 100
news rows), and one yfinance history (realised vol, post-earnings moves).

| Panel | Question it answers |
|---|---|
| **Composite Sentiment** | Where do six independent reads land, how much do they agree, and **where do they conflict** |
| **Dealer Positioning** | The Tab 1 gamma engine pointed at a single name — walls, flip level, OI gravity |
| **Options & Volatility** | 25Δ skew, put/call on book and flow, term structure, IV vs realised, max pain |
| **Squeeze & Ownership** | Short float and days to cover, institutional and insider holdings *and changes*, analyst consensus vs price |
| **Retail Chatter** | StockTwits declared positions, WSB-lexicon scoring of the rest, mention velocity |
| **Earnings & Catalysts** | Days to the print, the straddle's implied move vs the name's own history, and classified SEC filings |
| **News & Sentiment** | Google News + Seeking Alpha + Finviz, deduplicated, VADER with a finance lexicon, tone split by publisher |

### The point is divergence, not the score

The composite is navigation; the sub-score breakdown and the conflicts between
panels are the output. Encoded divergences include retail bullish into a
downtrend, retail long while the options market pays up for downside, insiders
selling into enthusiasm, price above the consensus target, squeeze fuel with
dealers short gamma, and a live offering registered into retail enthusiasm.

### Three things that are deliberately *not* directional

Each is a category error that is easy to introduce and hard to notice, so each
has a regression test that fails if someone "simplifies" it later.

- **Dealer gamma maps to volatility, not direction.** Positive gamma damps
  moves; negative amplifies them. Neither is bullish. Direction comes from spot
  versus the flip level, wall structure, and open-interest gravity; the gamma
  regime only sets an amplification flag and docks confidence.

- **Net DEX is not a signal at all.** Under this project's sign convention
  (dealers long calls, short puts) a call contributes `+delta·OI` and a put
  contributes `−delta·OI` with delta already negative — so every contract
  contributes a *positive* amount and net DEX is identically equal to gross DEX.
  Measured at exactly `+1.000000` for WEN, NVDA and TSLA. It measures the size
  of the hedging requirement, never its direction.

- **Short interest is fuel, not a direction.** A heavily shorted name that is
  rising has squeeze potential; the identical short interest on a falling name
  means the shorts are winning. The sub-score is signed by realised momentum.

### Trend metrics need history

`ticker_history` stores one flat snapshot per ticker per day. IV rank and social
mention velocity are meaningless as a point reading, so they report
`n/60` and `n/5` sessions stored rather than a fabricated percentile.

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

- **Finviz packs two values into one cell with no delimiter.** `52W Low` renders
  as `'164.0737.23%'` — price then distance, with nothing between them, and the
  percentage unsigned. Parsing with `get_text(separator="\x1f")` puts a unit
  separator between the sibling `<span>`s and makes it `'164.07\x1f37.23%'`.
  The snapshot is also split across **six** `snapshot-table2` elements;
  `select_one` returns 14 pairs instead of 83 and drops every short-interest and
  ownership field *without raising*.

- **Tab 2 never shows another ticker's cached numbers.** Tab 1's `_run` keeps a
  stale payload visible behind an error badge, which is right when the subject
  never changes. Tab 2's subject changes every press, so `_run_t2` only
  preserves the cache when its `symbol` matches the ticker being requested —
  otherwise a failed Finviz fetch would render NVDA's short interest under a WEN
  header.

- **The strike-chart window widens for low-priced names.** A fixed ±6% around
  an $8.61 stock with $1.00 strikes captures two strikes. The window steps out
  through 6/10/15/25% until at least 8 strikes are in frame; dense chains are
  unaffected and stay at ±6%.

- **A 424B is not a Form 4, and a shelf is not an offering.** Prefix-matching
  `"4"` classifies `424B5` as an insider filing. And an S-3ASR automatic shelf
  is routine plumbing for a large issuer — flagging it as dilution labels every
  mega-cap a financing risk, so the warning is gated on market cap.

- **Post-earnings history is computed over every filing, not the display
  slice.** An active filer's most recent twenty EDGAR rows are all Form 4s; WEN
  had 35 earnings 8-Ks in its full submission list and none in the first twenty.

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
  --- Tab 2 ---
  _finviz.py            Finviz quote scrape + all snapshot parse helpers
  _sentiment_util.py    VADER with finance/WSB lexicons, themes, salience
  ticker_positioning.py Dealer positioning for any optionable ticker
  vol_sentiment.py      Skew, put/call, term structure, IV-RV, max pain
  ticker_squeeze.py     Short interest, ownership, analyst, trend
  ticker_social.py      StockTwits tags + inferred scoring, velocity
  ticker_news.py        Google News + Seeking Alpha + Finviz, deduped
  ticker_events.py      Earnings, implied move, classified SEC filings
  ticker_sentiment.py   Composite score + divergence rules (pure)
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
python -m pytest panels/ -q                     # 316 tests, no network
python tools/check_contract.py                  # Tab 1 field contract
python tools/check_contract.py --panels tab2    # Tab 2, after an Analyse
python tools/check_contract.py --panels all
node tools/render_check.js                      # renderer output + geometry
python tools/calibrate.py                       # live magnitudes vs thresholds
python tools/smoke.py [panel ...]               # Tab 1 panels against live data
python tools/smoke.py t2 NVDA WEN QQQ           # Tab 2 across three shapes
```

The three smoke symbols are chosen to exercise different shapes: NVDA is a deep,
dense chain; WEN is a low-priced small cap with a thin chain, a 34% short float
and an activist 13D; QQQ is an ETF, which has no issuer filings and no short
interest, so it exercises the weight-renormalisation path.

Tab 2's contract check is opt-in because its panels hold no payload until a
ticker has been analysed — requiring them by default would fail the Tab 1 check
on a fresh database.

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
