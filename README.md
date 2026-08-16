# Regime Dashboard

A local, offline-first dashboard in three parts:

- **Tab 1 — SPX REGIME** combines dealer gamma positioning, delta decay, VIX
  term structure, auction structure, implied correlation and the expiration
  calendar into a single conditional-distribution read on SPX.
- **Tab 2 — TICKER SENTIMENT** answers the same kind of question for one US
  equity: type a ticker, and get dealer positioning, options-derived fear
  gauges, short interest and ownership, news and retail sentiment, event risk,
  and the divergences between them.
- **Tab 3 — NEWS SCREENER** widens the lens to eight cross-asset topic panels —
  US macro, equities and fixed income; Europe macro and markets; energy,
  precious metals and industrial metals — each with recent headlines from
  trusted publishers, live market levels, and *two* sentiment readings that
  disagree on purpose.

The domain spec is [`RESEARCH.md`](RESEARCH.md) — every Tab 1 panel implements a
section of it. The verified data sources are in
[`DATA_SOURCES.md`](DATA_SOURCES.md).

---

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env      # optional — see below
python app.py
```

Then open <http://localhost:8020>.

### One optional setting

SEC EDGAR requires every automated request to declare a contact address it can
reach you on; requests without one get a 403 and a ~10-minute IP block. So the
address is yours to supply rather than something the repo ships:

```bash
SEC_USER_AGENT="regime-dashboard/1.0 you@example.com"
```

Put it in `.env` (gitignored) or export it in your shell. **Leaving it unset is
supported** — the Earnings & Catalysts panel reports SEC filings as unavailable,
its earnings date and implied-move analysis still work, and no other panel is
affected. Nothing else in the project reads the environment.

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
| **Composite Sentiment** | Where do five independent reads land, how much do they agree, and **where do they conflict** |
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

- **Short interest is fuel, not a direction** — which is why it is not in the
  composite at all. A heavily shorted name that is rising has squeeze potential;
  the identical short interest on a falling name means the shorts are winning.
  The only way to force it into a directional sub-score is to sign it by
  something else, which invents a direction rather than measuring one. It is
  scored as potential in its own panel, and reaches the composite solely as the
  squeeze-fuel divergence. Note the Finviz snapshot it arrives on is still read
  by the composite — the analyst sub-score and several divergence rules come off
  the same page.

### Trend metrics need history

`ticker_history` stores one flat snapshot per ticker per day. IV rank and social
mention velocity are meaningless as a point reading, so they report
`n/60` and `n/5` sessions stored rather than a fabricated percentile.

---

## Tab 3 — News Screener

One **Refresh** press fans out to ~38 feeds across 12 workers and lands in about
15 seconds, yielding roughly 900 headlines. Those are routed into eight panels,
windowed to the last 48 hours, and cut to the newest 25 each.

Sources are Google News queries scoped to a trusted-domain whitelist (Reuters,
Bloomberg, FT, WSJ, CNBC, MarketWatch, AP, Barron's), the publishers' own feeds
where they work, and the primary sources the wires paraphrase — the Fed, the
ECB, the Bank of England and the EIA. Reuters and Bloomberg have no working
public feed at all and are reachable only through a Google `site:` query.

### The two readings, and why they disagree

Every panel shows **tone** and an **asset read**, and the gap between them is the
output.

*Tone* is VADER with the macro lexicon: how positive the language is. *Asset
read* is a rule layer that knows what the panel is about and maps each headline
to a direction **for that panel's asset**. They routinely point opposite ways,
because financial language and financial direction are not the same thing:

| Headline | Tone | Asset read |
|---|---|---|
| `10-year Treasury yield inches higher` | neutral/positive | **bearish bonds** |
| `Gold rises on weaker dollar as rate-cut bets firm` | −0.66 | **+0.80 bullish gold** |
| `Inflation cools more than expected` | negative ("cools") | **supportive** |
| `OPEC+ agrees deeper output cuts` | negative ("cuts") | **bullish crude** |

A live example from the verification session: Oil & Energy read **bearish tone
(−0.15)** — Hormuz attacks, a struck ship, Somali piracy, all negative language —
against **+0.78 bullish crude**, because a supply disruption is bullish oil. WTI
was up 1.42% that session. Tone alone would have had the sign backwards.

The same verb means opposite things in different panels: a rise in yields is
bearish in US Fixed Income, a rise in crude is bullish in Oil & Energy. That is
why there is no single global sentiment score here.

### Coverage is the honest limit

Most headlines are not directional. `asset_read` returns `None` — **not `0.0`** —
for those, and only the ones that fire a rule are averaged. Counting the rest as
neutral would drag every panel toward the middle in proportion to how much
off-topic news happened to land in it.

The consequence is that coverage is often 10–30%, and it is reported next to
every score, flagged `thin` below 25%, and warned about in the commentary. A
reading of −0.80 from 2 of 15 headlines is two headlines, and the panel says so.

Genuinely two-sided prints — a hot payrolls number is good for growth and bad
for rate cuts — are scored at half weight and flagged rather than forced onto
one side.

### What it cannot do

It is a bag of patterns, not a parser: no negation handling ("yields fail to
rise"), no distinction between a forecast and a fact, and a headline about two
assets moving opposite ways is resolved by word proximity rather than grammar.

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

- **Google ignores its own `when:` operator on a keyword query.** The US Fixed
  Income topic query with `when:2d` returned 100 entries whose median age was
  three months and whose oldest was eleven years — add keywords and Google
  switches to all-time relevance ranking. It *is* honoured on a bare `site:`
  query (`site:reuters.com when:1d` came back 98/98 inside 24 hours), which is
  why `_feeds.py` has two query shapes. Nothing depends on the operator: the
  48-hour window is applied locally and is the only real guarantee.

- **Four RSS feeds return HTTP 200, valid XML, and year-old news.** WSJ's two
  Dow Jones feeds and MarketWatch's realtime/marketpulse feeds had median entry
  ages of ~1.5 years while leading with entirely credible market headlines
  ("Jobless claims fall to lowest level since mid-May" — from eighteen months
  earlier). Entry count cannot detect this; `probe_news.py` reports *newest*
  age, which is what distinguishes a frozen archive from a quiet primary source.

- **`asset_read` returns `None`, never `0.0`, for an undirected headline.**
  Most headlines say nothing about the asset. Scoring them as neutral would
  drag every panel toward the middle in proportion to how much off-topic news
  landed in it, making a strong reading arithmetically impossible. The cost is
  that coverage is often 10–30%, which is reported rather than hidden.

- **"Treasury yields" is one subject, not two that cancel.** The yield pattern
  carries −1 and the bond pattern +1, and without masking each matched span as
  it is consumed both fire on the same three words and sum to exactly zero —
  rendering as a confident "no view" on every rates headline.

- **Five subject nouns are neutralised in the macro lexicon.** Stock VADER
  scores `crude` at −2.7 ("crude behaviour"), `credit` at +1.6 ("credit to
  her"), `treasuries` at +0.9 and `energy` at +1.1. On a single-topic corpus the
  subject appears in nearly every headline, so these are not noise — they are a
  fixed bias in one direction per panel. `NEWS` keeps the original values, where
  the ordinary-English meaning is the right one.

- **The Europe panels are gated on nothing.** An earlier context check required
  an explicit European marker before allowing a Europe panel, which dropped
  "DAX climbs to record high" outright — the index name *is* the marker. The
  gate now only ever removes US panels from a plainly European story.

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
  --- Tab 3 ---
  _feeds.py             Feed registry, Google query shapes, parallel fetcher
  _asset_read.py        Per-panel directional rules (movers/subjects/drivers)
  market_context.py     Batched yfinance quote strips, curves and ratios
  news_screener.py      Routing, windowing, both readings, commentary (pure)
  test_*.py             One test module per panel
static/                 index.html, app.js, style.css (no frameworks, no CDN)
tools/
  probe_sources.py      Endpoint verification
  probe_vx.py           VIX futures alternatives
  probe_news.py         Tab 3 feed health: entries, freshness, frozen archives
  calibrate.py          Live magnitudes vs commentary thresholds
  smoke.py              Run any panel's refresh() against live data
  check_contract.py     Assert live payloads carry every field app.js reads
  render_check.js       Execute the renderers and validate their SVG output
```

`ticker_news.py` (Tab 2) and `news_screener.py` (Tab 3) share `_feeds.py`'s RSS
plumbing — one `parse_feed`, one `dedupe`, one Google-suffix strip — so the two
tabs cannot drift apart on the details that bit once already.

## Tests

```bash
python -m pytest panels/ -q                     # 413 tests, no network
python tools/check_contract.py                  # Tab 1 field contract
python tools/check_contract.py --panels tab2    # Tab 2, after an Analyse
python tools/check_contract.py --panels tab3    # Tab 3, after a Refresh
python tools/check_contract.py --panels all
node tools/render_check.js                      # renderer output + geometry
python tools/calibrate.py                       # live magnitudes vs thresholds
python tools/probe_news.py                      # Tab 3 feed health
python tools/smoke.py [panel ...]               # Tab 1 panels against live data
python tools/smoke.py t2 NVDA WEN QQQ           # Tab 2 across three shapes
python tools/smoke.py t3                        # Tab 3 news screener
```

The three smoke symbols are chosen to exercise different shapes: NVDA is a deep,
dense chain; WEN is a low-priced small cap with a thin chain, a 34% short float
and an activist 13D; QQQ is an ETF, which has no issuer filings and no Finviz
analyst coverage, so it exercises the weight-renormalisation path.

The Tab 2 and Tab 3 contract checks are opt-in because their panels hold no
payload until they have been refreshed once — requiring them by default would
fail the Tab 1 check on a fresh database.

`probe_news.py` reports, per feed, the entry count, the count inside the 48-hour
window, and the **newest** and median entry ages. The newest/median split is the
point: it separates a legitimately quiet primary source (the Fed between
meetings — recent newest, old median) from a *frozen* archive that returns HTTP
200, parses cleanly and serves eighteen-month-old headlines that read as
current. Four feeds were caught and removed that way; see `DATA_SOURCES.md` §12c.

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
