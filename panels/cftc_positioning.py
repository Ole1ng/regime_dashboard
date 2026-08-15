"""CFTC Trader Positioning panel (panel key ``cftc_positioning``).

Ported verbatim from market_almanack. It is a standalone read: the regime
classifier does not consume it, because weekly COT data on a ~3-day release lag
cannot inform a same-session regime call. It sits on Tab 1 as slow-moving
context for the fast positioning panels above it.

Surfaces how speculative (Leveraged Funds) and real-money (Asset Managers)
futures traders are positioned in US equity-index and volatility futures, using
the CFTC weekly "Commitments of Traders" report (Traders in Financial Futures,
*futures-only*). Raw contract counts are normalised to a 0-100 percentile and a
z-score over a rolling 3-year (156-week) window so the read is human-readable.

Three reads (mirroring the dashboard's positioning brief):
  1. Positioning extremes  — Leveraged Funds net %ile; record net-long = crowded
                             / vulnerable, record net-short = squeeze fuel.
  2. VIX fragility         — for VIX the read is INVERTED: a heavily net-short
                             (low %ile) Leveraged Funds book = the short-vol
                             carry trade is crowded = fragile to a vol spike.
  3. Trend / divergence    — week-over-week change in net (adding vs covering)
                             plus a net-vs-price series so divergences show.

Pipeline: fetch TFF history per contract (Socrata, keyless) -> compute net /
percentile / z / WoW for both trader groups -> align weekly price (yfinance) ->
deterministic rule-based commentary (NO LLM) -> one JSON-serialisable payload.
Persistence and HTTP are handled by the caller (``app.py`` + ``store``), exactly
like every other panel.

The math is factored into pure, network-free functions so it is unit-testable
offline (see ``panels/test_cftc_positioning.py``); only ``refresh`` and the two
``fetch_*`` helpers touch the network.
"""

from __future__ import annotations

import bisect
from datetime import date, datetime, timezone

import requests

# --------------------------------------------------------------------------- #
# Config constants
# --------------------------------------------------------------------------- #

# CFTC Socrata Open Data API — "Traders in Financial Futures; Futures Only".
# Dataset id confirmed against the live API at build time (2026-06).
CFTC_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# cftc_contract_market_code values discovered + confirmed live (2026-06) by
# filtering the dataset on market_and_exchange_names. Human-readable market name
# is noted beside each so a future code change is easy to re-verify.
CONTRACTS = [
    {"key": "sp500", "label": "E-mini S&P 500", "code": "13874A",
     "yf": "^GSPC", "invert": False},   # E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE
    {"key": "vix",   "label": "VIX Futures",    "code": "1170E1",
     "yf": "^VIX",  "invert": True},    # VIX FUTURES - CBOE FUTURES EXCHANGE
    {"key": "ndx",   "label": "E-mini Nasdaq-100", "code": "209742",
     "yf": "^NDX",  "invert": False},   # NASDAQ MINI - CHICAGO MERCANTILE EXCHANGE
]

# Exact TFF field names verified against a live sample row before relying on them.
F_DATE = "report_date_as_yyyy_mm_dd"
F_LEV_LONG = "lev_money_positions_long"
F_LEV_SHORT = "lev_money_positions_short"
F_AM_LONG = "asset_mgr_positions_long"
F_AM_SHORT = "asset_mgr_positions_short"
F_OI = "open_interest_all"
_REQUIRED_FIELDS = (F_DATE, F_LEV_LONG, F_LEV_SHORT, F_AM_LONG, F_AM_SHORT)

LOOKBACK_WEEKS = 156          # rolling 3-year normalisation window
FETCH_WEEKS = 170             # pull a little extra margin over the window
SHORT_HISTORY = 30            # fewer obs than this -> low-confidence flag

# Extreme thresholds (percentile points) — tunable config, not code.
CROWDED_PCTL = 80             # >= 80th pctl net-long -> crowded / vulnerable
SQUEEZE_PCTL = 20             # <= 20th pctl net-long -> squeeze fuel (or, for VIX,
                              #   net-short -> short-vol fragile)
STALE_DAYS = 10               # latest report older than this -> stale badge
                              #   (COT is Tue positions released the next Friday)


# --------------------------------------------------------------------------- #
# Fetch (network) — kept thin so the math below stays unit-testable offline
# --------------------------------------------------------------------------- #

def fetch_cot(code: str, timeout: float = 30.0) -> list[dict]:
    """Fetch weekly TFF rows for one contract, sorted oldest -> newest.

    Raises on network/HTTP failure or if an expected field is missing (a silent
    upstream schema change must fail loudly, not produce a blank panel).
    """
    params = {
        "$select": ",".join(_REQUIRED_FIELDS + (F_OI,)),
        "$where": f"cftc_contract_market_code='{code}'",
        "$order": f"{F_DATE} DESC",
        "$limit": str(FETCH_WEEKS),
    }
    resp = requests.get(CFTC_URL, params=params, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        raise ValueError(f"CFTC returned no rows for contract {code}")
    missing = [f for f in _REQUIRED_FIELDS if f not in rows[0]]
    if missing:
        raise ValueError(f"CFTC TFF schema changed; missing fields: {missing}")
    rows.reverse()  # oldest -> newest
    return rows


def fetch_prices(yf_symbol: str) -> list[tuple[date, float]]:
    """Weekly closes for a yfinance symbol as [(date, close), ...] ascending.

    Best-effort: returns [] on any failure so the panel still renders the
    positioning gauges without the price overlay.
    """
    try:
        import yfinance as yf

        hist = yf.Ticker(yf_symbol).history(period="3y", interval="1wk")
        out: list[tuple[date, float]] = []
        for idx, close in hist["Close"].items():
            if close is None:
                continue
            d = idx.date() if hasattr(idx, "date") else idx
            try:
                out.append((d, float(close)))
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda t: t[0])
        return out
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Pure helpers (no network — unit-tested)
# --------------------------------------------------------------------------- #

def _to_int(v) -> int | None:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _parse_date(raw: str) -> date | None:
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def net_series(rows: list[dict], long_key: str, short_key: str
               ) -> list[tuple[date, int]]:
    """Build an ascending [(date, net=long-short)] series, skipping bad rows."""
    series: list[tuple[date, int]] = []
    for r in rows:
        d = _parse_date(r.get(F_DATE))
        lo, sh = _to_int(r.get(long_key)), _to_int(r.get(short_key))
        if d is None or lo is None or sh is None:
            continue
        series.append((d, lo - sh))
    series.sort(key=lambda t: t[0])
    return series


def percentile_rank(window: list[float], value: float) -> float:
    """Percentile (0-100) of ``value`` within ``window`` (share of obs <= value).

    Empty/degenerate windows return 50.0 (treated as mid / no signal).
    """
    if not window:
        return 50.0
    n = len(window)
    le = sum(1 for x in window if x <= value)
    return round(le / n * 100.0, 1)


def zscore(window: list[float], value: float) -> float | None:
    n = len(window)
    if n < 2:
        return None
    mean = sum(window) / n
    var = sum((x - mean) ** 2 for x in window) / n
    sd = var ** 0.5
    if sd == 0:
        return 0.0
    return round((value - mean) / sd, 2)


def classify_state(pctl: float, invert: bool) -> str:
    """Map a percentile to a state enum the frontend colours.

    Equity indices: high %ile (net-long) -> crowded; low (net-short) -> squeeze.
    VIX (invert): low %ile (net-short vol) -> fragile; otherwise neutral.
    """
    if invert:
        return "fragile" if pctl <= SQUEEZE_PCTL else "neutral"
    if pctl >= CROWDED_PCTL:
        return "crowded"
    if pctl <= SQUEEZE_PCTL:
        return "squeeze"
    return "neutral"


_VERDICTS = {
    "crowded": "Crowded long — positioning stretched, vulnerable to an unwind.",
    "squeeze": "Heavily net-short — bearish positioning is squeeze fuel on upside.",
    "fragile": "Short-vol crowded — fragile to a volatility spike.",
    "neutral": "Positioning mid-range — no crowding extreme.",
}


def verdict_text(state: str) -> str:
    return _VERDICTS.get(state, _VERDICTS["neutral"])


def _ordinal(n: float) -> str:
    """1 -> '1st', 2 -> '2nd', 42 -> '42nd', 11 -> '11th'."""
    i = int(round(n))
    if 10 <= i % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(i % 10, "th")
    return f"{i}{suf}"


def _wow_phrase(net: int, wow: int) -> str:
    """Plain-English read of the week-over-week change (read #3)."""
    if wow == 0:
        return "net position flat week-over-week"
    if net >= 0:
        return "funds added to longs" if wow > 0 else "funds trimmed longs"
    return "funds covered shorts" if wow > 0 else "funds added to shorts"


def build_sentence(label: str, net: int, wow: int, pctl: float,
                   invert: bool) -> str:
    """One plain-English sentence combining the level and the WoW flow."""
    side = "net-long" if net >= 0 else "net-short"
    base = (f"Leveraged funds are {side} {abs(net):,} contracts "
            f"({_ordinal(pctl)} %ile over 3yr)")
    flow = _wow_phrase(net, wow)
    sign = "+" if wow > 0 else ""
    tail = f"{flow} ({sign}{wow:,} WoW)."
    if invert and net < 0 and pctl <= SQUEEZE_PCTL:
        return (base + f"; the short-vol carry trade is crowded — {flow} "
                f"({sign}{wow:,} WoW), a vol spike could force a violent unwind.")
    return base + f"; {tail}"


def align_price(report_dates: list[date],
                prices: list[tuple[date, float]]) -> list[float | None]:
    """As-of align: for each report date take the latest price on/before it."""
    if not prices:
        return [None] * len(report_dates)
    pdates = [p[0] for p in prices]
    out: list[float | None] = []
    for d in report_dates:
        i = bisect.bisect_right(pdates, d) - 1
        out.append(prices[i][1] if i >= 0 else (prices[0][1] if prices else None))
    return out


# --------------------------------------------------------------------------- #
# Per-contract computation (pure)
# --------------------------------------------------------------------------- #

def compute_contract(cfg: dict, rows: list[dict],
                     prices: list[tuple[date, float]]) -> dict:
    """Build one contract's payload from already-fetched rows + price series."""
    invert = cfg["invert"]
    lev = net_series(rows, F_LEV_LONG, F_LEV_SHORT)
    am = net_series(rows, F_AM_LONG, F_AM_SHORT)
    if not lev:
        raise ValueError(f"no usable Leveraged Funds rows for {cfg['key']}")

    dates = [d for d, _ in lev]
    lev_net = [v for _, v in lev]
    am_by_date = {d: v for d, v in am}

    def group_block(series_vals: list[int], with_verdict: bool) -> dict:
        latest = series_vals[-1]
        window = [float(x) for x in series_vals[-LOOKBACK_WEEKS:]]
        pctl = percentile_rank(window, float(latest))
        z = zscore(window, float(latest))
        wow = latest - series_vals[-2] if len(series_vals) >= 2 else 0
        state = classify_state(pctl, invert)
        block = {
            "net": latest,
            "long": None, "short": None,  # filled below from latest raw row
            "pctl": pctl, "z": z, "wow": wow, "state": state,
        }
        if with_verdict:
            block["verdict"] = verdict_text(state)
            block["sentence"] = build_sentence(cfg["label"], latest, wow,
                                               pctl, invert)
        return block

    lev_block = group_block(lev_net, with_verdict=True)
    # Asset Managers: align to the same latest date; secondary, no verdict.
    am_vals = [am_by_date[d] for d in dates if d in am_by_date]
    am_block = group_block(am_vals, with_verdict=False) if am_vals else None

    # Fill raw latest long/short for the primary group's tooltip detail.
    latest_row = rows[-1] if rows else {}
    lev_block["long"] = _to_int(latest_row.get(F_LEV_LONG))
    lev_block["short"] = _to_int(latest_row.get(F_LEV_SHORT))
    if am_block is not None:
        am_block["long"] = _to_int(latest_row.get(F_AM_LONG))
        am_block["short"] = _to_int(latest_row.get(F_AM_SHORT))

    # Net-vs-price series for the divergence chart (Leveraged Funds).
    aligned = align_price(dates, prices)
    series = [{"date": d.isoformat(), "lev_net": v, "price": p}
              for (d, v), p in zip(lev, aligned)]

    flags = {
        "stale": (date.today() - dates[-1]).days > STALE_DAYS,
        "short_history": len(lev_net) < SHORT_HISTORY,
        "price_missing": not prices,
    }

    return {
        "key": cfg["key"],
        "label": cfg["label"],
        "invert": invert,
        "lev": lev_block,
        "am": am_block,
        "series": series,
        "flags": flags,
    }


def build_payload(contracts: list[dict], now: datetime | None = None) -> dict:
    """Assemble the full panel payload from per-contract blocks."""
    now = now or datetime.now(timezone.utc)
    as_of = max((c["series"][-1]["date"] for c in contracts if c["series"]),
                default=None)
    return {
        "as_of": as_of,
        "snapshot_ts": now.isoformat(timespec="seconds"),
        "lookback_weeks": LOOKBACK_WEEKS,
        "contracts": contracts,
        "caveat": (
            "CFTC Commitments of Traders (Traders in Financial Futures, "
            "futures-only). Tuesday positions are released the following Friday "
            "— a ~3-day lag. Positioning is a context / contrarian read, not a "
            "timing signal; normalised over a rolling 3-year window."
        ),
    }


# --------------------------------------------------------------------------- #
# Panel entry point (called by app.py)
# --------------------------------------------------------------------------- #

def refresh() -> dict:
    """Fetch + compute the full CFTC positioning snapshot. Raises on failure."""
    blocks = []
    for cfg in CONTRACTS:
        rows = fetch_cot(cfg["code"])
        prices = fetch_prices(cfg["yf"])
        blocks.append(compute_contract(cfg, rows, prices))
    return build_payload(blocks)
