# Market Regime Research — SPY Positioning & Auction Framework

**Purpose:** How gamma-by-strike, delta decay (charm/vanna), VIX term structure, auction market theory, implied correlation, and OpEx mechanics combine into a single conditional-distribution model for SPY/SPX.

**Core premise:** None of these six lenses is a directional forecast on its own. Each answers a *different* question about the same conditional distribution. The common error is asking gamma "up or down?" when gamma only answers "how wide and how jumpy?"

---

## The Layered Model

| Layer | Question it answers | Inputs |
|---|---|---|
| **Regime** | Is realized vol suppressed or amplified? Mean-reverting or trending? | Net dealer gamma sign, VIX term structure, implied correlation |
| **Levels** | Where does price stall vs. accelerate? | Gamma by strike ∩ auction structure (value area, HVN/LVN, naked POC) |
| **Drift** | What mechanical flow exists over the next N sessions, and when does it expire? | Charm/vanna decay profile, OpEx calendar, VIX expiry/roll |
| **Trigger** | What flips the regime? | Gamma flip breach, correlation spike, term structure inversion |

Direction comes from the **drift** layer and from which side of the **levels** you are on. The **regime** layer tells you how much to trust any of it.

---

## 1. Gamma Levels by Strike

### What it gives you

Three references:

- **Gamma flip level** — the spot price at which net dealer gamma crosses zero. Above it, dealers are net long gamma; below it, net short.
- **Call wall** — the largest positive-gamma strike above spot. Acts as resistance / magnet.
- **Put wall** — the equivalent below spot. Acts as support *while long gamma*, and as an accelerant once breached into short gamma.

### Mechanism

- **Dealers net long gamma** → they hedge *against* the move: sell rallies, buy dips. Realized vol is compressed, price pins to high-OI strikes, rotation dominates.
- **Dealers net short gamma** → they hedge *with* the move: buy strength, sell weakness. Moves feed themselves, realized vol expands, trends persist.

Typical construction (per 1% move):

```
GEX = Σ_calls (OI × Γ × 100 × S² × 0.01) − Σ_puts (OI × Γ × 100 × S² × 0.01)
```

### Caveats that matter more than the chart

1. **The sign is an assumption, not a measurement.** The standard construction (dealers long call OI, short put OI) is a heuristic from SqueezeMetrics. It is directionally right often enough to be useful and badly wrong in specific setups. CBOE open-close data, or any vendor doing trade-level buyer/seller classification, is a real upgrade.
2. **0DTE has hollowed out the static profile.** A large share of SPX gamma is now born and dies within a session. An end-of-day GEX snapshot describes a book that no longer exists by 11am. You want an intraday-refreshed profile.
3. **Use SPX + ES alongside SPY.** SPY open interest is mostly retail and ETF hedging; the institutional book lives in SPX.
4. **SPY is American-style and pays dividends.** ITM calls are exercised early around the quarterly ex-div (third Friday of Mar/Jun/Sep/Dec), which mechanically deletes open interest at exactly the moment OpEx is supposed to be pinning it.
5. **The profile is not static.** The flip level moves as spot moves and as IV changes. Re-solve it, don't cache it.

---

## 2. Delta Decay — Charm and Vanna

### Definitions

- **Charm** = ∂Δ/∂t — how option delta changes with the passage of time.
- **Vanna** = ∂Δ/∂σ — how option delta changes with implied volatility.

OTM deltas bleed toward zero as expiry nears; ITM deltas migrate toward ±1.

### Why it becomes a forecast

If dealers are short a large stack of OTM puts (the standard state, since institutions buy protection), they are short stock as the hedge. Then:

- **Time passes with spot above those strikes** → put deltas shrink → dealers **buy back stock** → upward drift. This is the documented grind-up into monthly expiration.
- **IV falls** → same effect via vanna → dealers buy back hedges → the "vol crush melt-up" after a scare resolves.

The flows are largest when three things coincide: large OTM put open interest, elevated IV, and compressing time to expiry. That is precisely the pre-OpEx setup.

### How to read a delta-decay graph

Read it as **expected mechanical flow per day for the next two weeks, holding spot and vol constant.** It is one of the few genuinely forward-looking outputs in this toolkit.

It is also the thing that reverses hardest: **the flow is finite and it ends at expiry.**

---

## 3. VIX Term Structure — First Month vs. Spot

Spot VIX is a synthetic 30-day number derived from SPX options and is not tradable. The front VX future is the market's forecast of VIX at *its own* settlement date.

### Reading the slope

- **Contango (VX1 > spot VIX)** — roughly 80% of the time. Negative roll for long vol, positive carry for short vol → vol supply is profitable → dealers accumulate long gamma → grinding, pinned tape. Contango is as much a *cause* of the long-gamma regime as a symptom of it.
- **Backwardation (spot VIX > VX1)** — demand for immediate protection outruns supply. Vol sellers get stopped out, dealer gamma flips short, and the two reinforce each other.

### Calendar mechanics to build in explicitly

- **VIX expiry** is the Wednesday **30 days before the following month's third-Friday SPX AM settlement.** It typically lands the Wednesday of, or the week before, monthly OpEx week. It is a *distinct* event from SPX OpEx, and it is where the SPX option strip used for VIX settlement gets hit.
- **Always interpolate to constant maturity.** Reading raw VX1 produces a sawtooth in the slope at every roll; you will misread the discontinuity as a signal.
- **VIX ETP roll flow** (VXX, UVXY) redistributes daily between M1 and M2 and shifts as expiry approaches.

### Supporting ratios

| Measure | Reads |
|---|---|
| VIX9D / VIX | Near-term event pricing vs. baseline |
| VIX / VIX3M | > 1 = backwardation warning |
| VVIX | Vol-of-vol; tail/convexity demand |

---

## 4. Auction Market Theory

AMT is the only lens here that reflects **where participants agreed on value**, rather than where the derivative book forces flow. That is exactly why it is complementary.

### Core concepts

- **Value area** (70% of volume), **POC** (point of control), **initial balance** (first hour range).
- **Excess** — a sharp rejection tail marking a *finished* auction. A high or low made **without** excess is unfinished business and tends to be revisited.
- **Balance vs. imbalance** — rotational/accepted vs. directional/one-timeframing.
- **Day types** — normal, normal variation, trend, neutral, double distribution.
- **Naked/virgin POCs and single prints** act as magnets and as fast-travel corridors.
- **80% rule** — open outside prior value, then trade back into it and accept for two consecutive 30-minute brackets → high probability of rotating to the other side of value.

### Confluence with gamma — the whole point

- Call wall sitting **on** a prior value-area high or a high-volume node → a very strong reference. Two independent reasons for price to stall.
- A gamma level sitting in a **low-volume node or single-print zone** → the opposite. Nobody agreed on value there. Price traverses it fast, and short-gamma dealer hedging amplifies the traverse. These are your **acceleration zones** and where stops should *not* sit.

### Practical note

Build the profile on **ES futures, not SPY.** The auction runs nearly 24 hours; an RTH-only SPY profile discards the overnight auction where much of the imbalance is established.

---

## 5. Implied Correlation Index

CBOE **COR1M / COR3M** (successors to ICJ / JCJ / KCJ). Backs out implied ρ from index variance versus weighted constituent variances:

```
σ²_index ≈ Σ wᵢ²σᵢ²  +  Σ_{i≠j} wᵢ wⱼ σᵢ σⱼ ρ
```

### Read it as the transmission dial

How much single-stock energy actually reaches the index.

- **Low implied correlation** → index vol is cheap relative to single-stock vol → dispersion trades (short index vol / long single-name vol) are crowded. Individual stocks can be chaotic while the index barely moves. This **reinforces long-gamma pinning** and makes the index look deceptively calm.
- **Rising correlation** → macro is driving, everything moves together, hedges work, trends persist. Index-level gamma effects dominate.

### The fragility signal

The dangerous configuration is **implied correlation at multi-year lows**:

1. The index has been artificially quiet, so index IV is low.
2. So the gamma profile looks comfortable and vol-selling looks safe.
3. A shock jolts correlation upward → dispersion desks must buy index vol back → index vol spikes far more than the underlying news warrants → hedging flow amplifies the move.

August 2024 is the clean case study. Implied correlation is the best fragility gauge in this set precisely because it is **not** derived from index price action — it is orthogonal information.

---

## 6. OpEx Effects

Monthly third Friday. SPX is AM-settled on the SET print; SPXW and SPY are PM-settled. Quarterly (Mar/Jun/Sep/Dec) is triple witching plus the S&P index rebalance in the same close.

### What actually matters

- The large monthly OI is what **generates the charm/vanna drift** *and* what **supplies the long gamma doing the pinning**. Both vanish simultaneously on Friday, and the gamma profile resets to a much flatter book.
- Hence the **post-OpEx week** tendency: realized vol rises, the mechanical bid is gone, and any move that was suppressed gets expressed. The seasonal negative skew in that week is real **and it has a mechanism** — which is rare.
- **JHEQX** (JPM hedged-equity collar) rolls at quarter-end. Its put-spread and short-call strikes function as quarter-long reference levels, and the roll itself is a delta/vega event.
- **Adjust for the modern regime.** 0DTE has diluted monthly OpEx's relative weight versus 2019–2021. Monthly still holds the largest concentration of *longer-dated* OI, but do not size to a 2020-calibrated effect.
- Quarterly index rebalance adds significant MOC volume on the same close.

---

## Composing Them — Daily Read Order

1. **Term structure + implied correlation** → set the regime prior. Contango + low correlation = quiet. Flattening term structure + rising correlation = fragility building, *regardless of what price is doing*.
2. **Net gamma sign and distance to flip** → is the tape self-damping or self-amplifying, and how far away is the switch?
3. **Overlay gamma strikes on the ES volume profile** → keep only levels with confluence, discard the rest. Mark LVN / single-print corridors as no-fade zones.
4. **Charm/vanna decay + calendar** → what is the drift, how many sessions of it remain, what date kills it.
5. **Define the invalidation** → gamma flip level, VIX term inversion, or a correlation spike.

### The four regimes

| Regime | Signature | Expected behavior | Posture |
|---|---|---|---|
| **Pin & grind** | Long gamma, contango, low corr, pre-OpEx | RV << IV, rotation inside value, upward charm drift | Fade extremes toward the walls; short vol works; small size on breakouts |
| **Unstable pin** | Long gamma but corr rising / term flattening | Pinning still holds intraday, tail is being built | Keep the pin trade, buy cheap wings, watch flip distance shrink |
| **Acceleration** | Below flip, backwardation, high corr | RV > IV, gaps, trend days, one-timeframing | Do **not** fade. Put wall is an accelerant, not support. Trade with the auction |
| **Reflexive repair** | Short gamma, term re-steepening, corr falling | Vanna + charm rally, violent upside | Long delta or long call spreads; the melt-up is mechanically driven |

### The one scheduled regime switch

The **post-OpEx transition** is free every month: drift ends, gamma drops, dispersion of outcomes widens. Both the mechanism and the date are known in advance. If you only trade one structural edge from this framework, that is the highest-quality one.

---

## Confluence Scoring

A level is high-conviction when **three or more** independent lenses agree:

- Gamma strike (call/put wall or flip)
- Auction structure (VAH/VAL, POC, HVN)
- Prior session excess or unfinished business
- Charm/vanna drift pointing toward it
- Regime consistent with respecting levels (long gamma) rather than running them (short gamma)

A level with one supporting lens is a note. A level with four is a trade location.

---

## What This Can and Cannot Do

Be precise about the nature of the edge, or you will over-trade it.

- **The strong result is on volatility, not direction.** Dealer gamma has a solid relationship to *forward realized vol* and to mean-reversion vs. trend character. Its relationship to forward *returns* is weak.
- **Direction comes from the drift layer.** Charm/vanna is genuinely directional — but it is a two-week-horizon flow with a known expiration, not a thesis.
- **Levels are only as good as the OI behind them,** and OI changes intraday. A stale map is worse than no map, because it is confidently wrong.
- **The positioning sign assumption is the single largest error source.** Everything downstream inherits it.
- **These are probability tilts, not signals.** Realistically you are moving 50/50 to roughly 57/43 on vol regime, and less than that on direction. Position sizing and invalidation levels do more work than the analysis does.
- **Regime dependence in backtests.** Anything calibrated on 2012–2019 is calibrated on a world without meaningful 0DTE and with different dealer inventory. Re-fit on post-2022 data.

---

## Data Sources

| Need | Source |
|---|---|
| Implied correlation (COR1M/COR3M), VIX complex, open-close volume | CBOE |
| VIX futures term structure | CBOE / CFE settlement data |
| Option chains with OI + greeks | Polygon, Theta Data, ORATS, tastytrade |
| Volume profile / auction structure | CME ES futures (near-24h), not SPY RTH |
| Dealer positioning (vendor) | SpotGamma, MenthorQ, GammaLab — useful cross-check on your own GEX build |
| JHEQX strikes | Quarterly 13F / fund fact sheet, or reconstruct from SPX OI spikes |

---

## Build Notes / Next Steps

Candidate regime panel for `spx_dashboard/`:

1. Constant-maturity VX term structure (30d / 60d / 90d interpolated) + contango-backwardation flag
2. GEX by strike with flip level, call wall, put wall — intraday refresh, SPX + SPY + ES
3. Charm/vanna decay projection: expected dealer delta flow per day, next 10 sessions
4. COR1M with percentile rank vs. trailing 2 years (fragility gauge)
5. ES volume profile overlay: VAH/VAL/POC, naked POCs, LVN corridors
6. Regime classifier on top of 1–5, emitting one of the four regimes plus its invalidation level
