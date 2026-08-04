# Data sources — verified live

All sources are free and unauthenticated. Verified by `tools/probe_sources.py` and
`tools/probe_vx.py`; re-run those when something breaks.

**Verification date: 2026-08-04** (all figures below are that session's live values,
recorded so a future re-probe can sanity-check the shapes).

---

## 1. Option chains — CBOE delayed quotes CDN ✅

| Underlying | URL | Result |
|---|---|---|
| SPX | `https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json` | 13.1 MB, 29,412 contracts, 21,605 with OI>0 |
| SPY | `https://cdn.cboe.com/api/global/delayed_quotes/options/SPY.json` | 6.2 MB, 13,944 contracts, 10,294 with OI>0 |

Requires a browser `User-Agent` header. ~15-min delayed.

- Envelope: `{timestamp, symbol, data:{options:[...], current_price, close, bid, ask, ...}}`
- Contract fields: `option, bid, ask, iv, open_interest, volume, delta, gamma, vega,
  theta, rho, theo, last_trade_price, ...`
- **SPX symbol roots are `SPX` (9,626) and `SPXW` (19,786)** — the weeklies dominate,
  so the regex must accept both. Confirmed live.
- SPY has the single root `SPY`.
- Observed spots on the verification date: SPX 7712.18, SPY 768.81 (ratio ≈ 10.03).

## 2. VIX term structure ✅ (via two indirect routes)

**Every direct VX futures endpoint is blocked** — all return HTTP 403:
`cdn.cboe.com/.../futures/VX.json`, `_VX.json`, `.../futures/settlements/VX.json`,
`.../term_structure/VX.json`, `cdn.cboe.com/data/us/futures/.../VX.csv`. The
`cboe.com/us/futures/api/get_quotes_combined/` API returns HTTP 420. Yahoo has no
`VX=F` / `VIX=F`. Do not waste time re-probing these.

Two working substitutes were found instead, and they cross-validate each other:

### 2a. PRIMARY — forwards from the VIX **option** chain (live, 15-min delayed)

`https://cdn.cboe.com/api/global/delayed_quotes/options/_VIX.json` (0.68 MB, 1,520
contracts, 13 expiries with two-sided quotes).

VIX options are priced off VIX **futures**, not spot VIX, so put-call parity on the
chain recovers the forward for each listed expiry:

```
F = K + e^(rT)·(C − P)     evaluated at the strike minimising |C − P|
```

Roots: `VIX` = monthly expiries (the standard VX contracts), `VIXW` = weeklies.

Live term structure recovered on the verification date (spot VIX 16.30):

| Expiry | Days | Forward |
|---|---|---|
| 2026-08-05 | 1 | 16.25 |
| 2026-08-19 (**VX1**, monthly) | 15 | 17.86 |
| 2026-09-16 (**VX2**, monthly) | 43 | 19.09 |
| 2026-10-21 | 78 | 20.17 |
| 2026-11-18 | 106 | 20.65 |
| 2026-12-16 | 134 | 20.77 |

Monotonic and well-behaved — contango, spot−VX1 basis −1.56.

### 2b. CROSS-CHECK — CFE daily settlement CSV (end-of-day)

`https://www.cboe.com/us/futures/market_statistics/settlement/csv/` → HTTP 200,
`Product,Symbol,Expiration Date,Price`.

Agreement with 2a on the verification date is close enough to validate the parity
method:

| Contract | Settlement CSV | Parity forward |
|---|---|---|
| VX/U6 (2026-09-16) | 19.1787 | 19.09 |
| VX/V6 (2026-10-21) | 20.2239 | 20.17 |
| VX/X6 (2026-11-18) | 20.6516 | 20.65 |

Caveat: this file is an **end-of-day** settlement. Intraday it may carry the prior
session, and contracts that did not trade get a repeated reference price (six
consecutive weeklies all showed 17.9466 during the intraday probe). Use it as a
cross-check only, never as the live source.

### 2c. VIX index family — constant maturity by construction

Via yfinance: `^VIX1D` 9.99, `^VIX9D` 14.54, `^VIX` 16.30, `^VIX3M` 19.06,
`^VIX6M` 21.24, `^VVIX` 89.31. `^VIX1D` is also on the CBOE quote CDN.

These are *not* a fallback — RESEARCH.md §3 asks for constant-maturity readings, and
these are constant-maturity by definition, so they carry the VIX/VIX3M and VIX9D/VIX
ratios directly.

## 3. Implied correlation — CBOE ✅ (better than expected)

Live quotes:
- `https://cdn.cboe.com/api/global/delayed_quotes/quotes/_COR1M.json` → 6.62
- `https://cdn.cboe.com/api/global/delayed_quotes/quotes/_COR3M.json` → 9.28

**Full daily history back to 2006-01-03** — no local warm-up period needed, real
percentile ranks are available immediately:
- `https://cdn.cboe.com/api/global/us_indices/daily_prices/COR1M_History.csv` (263 KB)
- `https://cdn.cboe.com/api/global/us_indices/daily_prices/COR3M_History.csv` (263 KB)

Format: `DATE,OPEN,HIGH,LOW,CLOSE` with `MM/DD/YYYY` dates.

The `/charts/_COR1M.json` endpoint is 403 — use the CSVs.

> Note: COR1M at 6.62 on the verification date is near the bottom of its 20-year
> range (the 2006 series opens around 23). That is exactly the crowded-dispersion
> fragility configuration RESEARCH.md §5 describes.

## 4. Index quotes — CBOE quote CDN ✅

`https://cdn.cboe.com/api/global/delayed_quotes/quotes/_<SYMBOL>.json` works for any
index: `_VIX`, `_VIX1D`, `_COR1M`, `_COR3M`. Returns
`{timestamp, data:{symbol, current_price, open, high, low, close, prev_day_close, ...}}`.

## 5. SPY intraday bars — yfinance ✅

| Request | Result |
|---|---|
| `period="5d", interval="1m"` | 1,717 bars / 5 sessions |
| `period="1mo", interval="5m"` | 1,670 bars / 22 sessions |
| `period="60d", interval="5m"` | 4,634 bars / 60 sessions |

Volume column present. **Regular trading hours only** — 09:30–16:00 ET. The overnight
auction is not captured; this limitation is stated on the volume-profile panel, per
the agreed compromise (free ES futures intraday data does not exist).

## 6. Spot levels — yfinance ✅

`^SPX` 7711.86, `^GSPC` 7710.64, `SPY` 768.58 — all current. Used only as a
cross-check; the option-chain payload carries its own spot.

---

## Sources deliberately not used

- Any paid or authenticated feed (ORATS, Polygon, Theta Data, SpotGamma, CBOE DataShop).
- Third-party scrapers of VX term structure (e.g. vixcentral) — the parity method above
  is first-party and more robust.
- ES futures intraday volume — no free source exists; SPY RTH is the agreed substitute.
