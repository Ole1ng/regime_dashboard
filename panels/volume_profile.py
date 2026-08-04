"""Auction structure / volume profile — RESEARCH.md §4.

The only lens in the framework that reflects where *participants agreed on
value* rather than where the derivative book forces flow, which is exactly why
it is complementary to the gamma map. Produces:

  * per-session POC / VAH / VAL (70% value area),
  * a composite profile over the recent sessions,
  * **naked POCs** — prior session POCs price has not traded back through, which
    act as magnets,
  * **LVN corridors** — low-volume nodes where nobody agreed on value, so price
    traverses them fast. These are the acceleration zones, and the places a
    gamma level should *not* be trusted as support or resistance.

Every level is emitted in both SPY and SPX terms so the regime panel can score
confluence against the gamma strikes.

LIMITATION, stated on the panel: this is built from SPY regular-hours bars.
RESEARCH.md asks for the ES futures profile because the auction runs nearly 24
hours, but no free intraday ES source exists, so the overnight auction is not
captured. Levels established outside 09:30-16:00 ET will be missing.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import requests

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
SPX_QUOTE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_SPX.json"

BIN_SIZE = 0.10           # SPY price bin, in dollars
VALUE_AREA = 0.70         # share of volume inside the value area
SESSIONS = 10             # sessions to profile individually
COMPOSITE_SESSIONS = 5    # sessions in the composite profile
LVN_THRESHOLD = 0.30      # bin volume below this share of the mean = low volume
LVN_MIN_BINS = 3          # a corridor must span at least this many bins
INTERVAL = "5m"
PERIOD = "1mo"
# Candles are aggregated up from the 5m bars: 30 minutes is the classic
# market-profile bracket, and at ~13 per session it keeps 10 sessions legible
# on one axis where raw 5m bars would be ~1px wide.
CANDLE_RULE = "30min"
CANDLE_LABEL = "30m"


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #

def fetch_bars(period: str = PERIOD, interval: str = INTERVAL) -> pd.DataFrame:
    """SPY intraday bars, regular hours only."""
    import yfinance as yf

    df = yf.Ticker("SPY").history(period=period, interval=interval, prepost=False)
    if df is None or df.empty:
        raise ValueError("yfinance returned no SPY intraday bars")
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df = df[df["Volume"] > 0]
    if df.empty:
        raise ValueError("no SPY bars with volume")
    return df


def fetch_spx_spot(timeout: float = 20.0) -> float:
    r = requests.get(SPX_QUOTE_URL, headers=_HEADERS, timeout=timeout)
    r.raise_for_status()
    d = (r.json().get("data") or {})
    v = d.get("current_price") or d.get("close")
    if not v:
        raise ValueError("no SPX spot")
    return float(v)


# --------------------------------------------------------------------------- #
# Profile construction
# --------------------------------------------------------------------------- #

def _bin_index(price, bin_size: float):
    """Bin index for a price, robust to binary floating point.

    A naive ``floor(price / bin_size)`` is not reliable at bin edges: 99.8/0.1
    evaluates to 997.9999999999999 and floors to 997 rather than 998, which
    shifts a whole session's profile by one bin and can move the POC. Rounding
    the quotient before flooring makes edges deterministic and inclusive.
    """
    q = np.round(np.asarray(price, dtype=float) / bin_size, 9)
    idx = np.floor(q)
    return idx.astype(int) if idx.ndim else int(idx)


def build_profile(bars: pd.DataFrame, bin_size: float = BIN_SIZE) -> dict:
    """Volume-at-price from OHLCV bars.

    Each bar's volume is spread uniformly across the bins its range covers,
    which is the standard approximation when only OHLC is available (the
    intra-bar path is unknown).
    """
    if bars.empty:
        return {"bins": {}, "low": None, "high": None, "total": 0.0}
    lo = float(bars["Low"].min())
    hi = float(bars["High"].max())
    lo_idx = _bin_index(lo, bin_size)
    hi_idx = _bin_index(hi, bin_size)
    n = int(hi_idx - lo_idx) + 1
    hist = np.zeros(n, dtype=float)

    lows = _bin_index(bars["Low"].values, bin_size) - lo_idx
    highs = _bin_index(bars["High"].values, bin_size) - lo_idx
    vols = bars["Volume"].values.astype(float)
    for a, b, v in zip(lows, highs, vols):
        a, b = max(a, 0), min(b, n - 1)
        if b < a:
            continue
        hist[a:b + 1] += v / (b - a + 1)

    bins = {round((lo_idx + i) * bin_size + bin_size / 2, 4): float(hist[i])
            for i in range(n) if hist[i] > 0}
    return {"bins": bins, "low": lo, "high": hi, "total": float(hist.sum())}


def value_area(bins: dict[float, float], coverage: float = VALUE_AREA):
    """POC plus the 70% value area, expanding outward from the POC.

    Standard construction: repeatedly compare the pair of bins above the current
    area with the pair below, and absorb whichever side carries more volume.
    """
    if not bins:
        return None, None, None
    prices = sorted(bins)
    vols = [bins[p] for p in prices]
    total = sum(vols)
    if total <= 0:
        return None, None, None

    poc_i = int(np.argmax(vols))
    lo_i = hi_i = poc_i
    acc = vols[poc_i]
    target = total * coverage

    while acc < target and (lo_i > 0 or hi_i < len(prices) - 1):
        up = vols[hi_i + 1] + (vols[hi_i + 2] if hi_i + 2 < len(prices) else 0.0) \
            if hi_i < len(prices) - 1 else -1.0
        dn = vols[lo_i - 1] + (vols[lo_i - 2] if lo_i - 2 >= 0 else 0.0) \
            if lo_i > 0 else -1.0
        if up < 0 and dn < 0:
            break
        if up >= dn:
            step = min(2, len(prices) - 1 - hi_i)
            for _ in range(step):
                hi_i += 1
                acc += vols[hi_i]
        else:
            step = min(2, lo_i)
            for _ in range(step):
                lo_i -= 1
                acc += vols[lo_i]
    return prices[poc_i], prices[hi_i], prices[lo_i]     # POC, VAH, VAL


def find_lvn(bins: dict[float, float], bin_size: float = BIN_SIZE,
             threshold: float = LVN_THRESHOLD, min_bins: int = LVN_MIN_BINS):
    """Contiguous runs of unusually thin volume — the acceleration corridors.

    A corridor must have a real shelf on *both* sides. Without that check the
    thinnest run is always the tail at the top or bottom of the profile, where
    volume is trivially light simply because price only just reached there —
    live SPY returned 770.30-771.30 as its "thinnest corridor" purely because
    that was the session high. That is an edge, not a gap between two areas of
    accepted value, and price does not travel through it any faster.
    """
    if not bins:
        return []
    prices = sorted(bins)
    vols = [bins[p] for p in prices]
    mean_v = float(np.mean(vols))
    if mean_v <= 0:
        return []
    cutoff = mean_v * threshold

    runs: list[list[int]] = []
    run: list[int] = []
    for i, p in enumerate(prices):
        contiguous = (not run) or abs(p - prices[run[-1]] - bin_size) < bin_size / 2
        if vols[i] < cutoff and contiguous:
            run.append(i)
        else:
            if len(run) >= min_bins:
                runs.append(run)
            run = [i] if vols[i] < cutoff else []
    if len(run) >= min_bins:
        runs.append(run)

    out = []
    for r in runs:
        has_shelf_below = any(v >= mean_v for v in vols[:r[0]])
        has_shelf_above = any(v >= mean_v for v in vols[r[-1] + 1:])
        if not (has_shelf_below and has_shelf_above):
            continue
        out.append({
            "lo": round(prices[r[0]] - bin_size / 2, 2),
            "hi": round(prices[r[-1]] + bin_size / 2, 2),
            "bins": len(r),
            "mean_volume_share": round(
                float(np.mean([vols[i] for i in r])) / mean_v, 3),
        })
    return out


# --------------------------------------------------------------------------- #
# Compute
# --------------------------------------------------------------------------- #

def build_candles(frames: dict, day_keys: list, comp_days: set,
                  rule: str = CANDLE_RULE) -> list[dict]:
    """Aggregate the 5m bars up to `rule` candles, per session.

    Resampling is anchored to each session's own first bar rather than to the
    wall clock, so buckets line up with the 09:30 open and a session with a late
    first print does not produce a stub candle.
    """
    out: list[dict] = []
    for d in day_keys:
        f = frames[d].sort_index()
        if f.empty:
            continue
        agg = (f.resample(rule, origin="start")
               .agg({"Open": "first", "High": "max", "Low": "min",
                     "Close": "last", "Volume": "sum"})
               .dropna(subset=["Open", "High", "Low", "Close"]))
        for ts, r in agg.iterrows():
            out.append({
                "t": ts.strftime("%Y-%m-%d %H:%M"),
                "d": d.isoformat(),
                "o": round(float(r["Open"]), 2),
                "h": round(float(r["High"]), 2),
                "l": round(float(r["Low"]), 2),
                "c": round(float(r["Close"]), 2),
                "v": float(r["Volume"]),
                "in_composite": d in comp_days,
            })
    return out


def compute(bars: pd.DataFrame, spx_spot: float, spy_spot: float | None = None,
            today: date | None = None) -> dict:
    # Slice by index date rather than accumulating rows: this keeps a real
    # DatetimeIndex on each session frame, which resampling to candles needs.
    if bars.empty or not isinstance(bars.index, pd.DatetimeIndex):
        raise ValueError("no complete sessions in the SPY bar set "
                         "(expected bars indexed by timestamp)")
    idx_dates = bars.index.date
    day_keys = sorted(set(idx_dates))[-SESSIONS:]
    if not day_keys:
        raise ValueError("no complete sessions in the SPY bar set")

    frames = {d: bars[idx_dates == d] for d in day_keys}
    spy_spot = float(spy_spot if spy_spot is not None
                     else frames[day_keys[-1]]["Close"].iloc[-1])
    ratio = spx_spot / spy_spot

    def to_spx(x):
        return None if x is None else round(x * ratio, 2)

    sessions = []
    for d in day_keys:
        prof = build_profile(frames[d])
        poc, vah, val = value_area(prof["bins"])
        sessions.append({
            "date": d.isoformat(),
            "poc": poc, "vah": vah, "val": val,
            "poc_spx": to_spx(poc), "vah_spx": to_spx(vah), "val_spx": to_spx(val),
            "high": round(prof["high"], 2) if prof["high"] else None,
            "low": round(prof["low"], 2) if prof["low"] else None,
            "volume": prof["total"],
        })

    # --- naked POCs: never traded back through by a later session ---------- #
    naked = []
    for i, s in enumerate(sessions[:-1]):
        if s["poc"] is None:
            continue
        touched = any(later["low"] is not None and later["high"] is not None
                      and later["low"] <= s["poc"] <= later["high"]
                      for later in sessions[i + 1:])
        if not touched:
            naked.append({
                "date": s["date"], "spy": s["poc"], "spx": s["poc_spx"],
                "sessions_ago": len(sessions) - 1 - i,
                "above_spot": s["poc"] > spy_spot,
            })
    naked.sort(key=lambda x: abs(x["spy"] - spy_spot))

    # --- composite profile over the recent sessions ------------------------ #
    comp_days = day_keys[-COMPOSITE_SESSIONS:]
    comp_bars = pd.concat([frames[d] for d in comp_days])
    comp = build_profile(comp_bars)
    c_poc, c_vah, c_val = value_area(comp["bins"])
    lvn = find_lvn(comp["bins"])
    for z in lvn:
        z["lo_spx"] = to_spx(z["lo"])
        z["hi_spx"] = to_spx(z["hi"])
    # Nearest first — the corridor price is most likely to reach.
    lvn.sort(key=lambda z: abs((z["lo"] + z["hi"]) / 2 - spy_spot))

    total = comp["total"] or 1.0
    chart = [{"price": p, "price_spx": to_spx(p), "volume": v,
              "share": v / total}
             for p, v in sorted(comp["bins"].items())]

    payload = {
        "spy_spot": round(spy_spot, 2),
        "spx_spot": round(spx_spot, 2),
        "ratio": round(ratio, 4),
        "sessions": sessions,
        "n_sessions": len(sessions),
        "composite_sessions": len(comp_days),
        "composite_from": comp_days[0].isoformat(),
        "composite_to": comp_days[-1].isoformat(),
        "composite_poc": c_poc, "composite_vah": c_vah, "composite_val": c_val,
        "composite_poc_spx": to_spx(c_poc),
        "composite_vah_spx": to_spx(c_vah),
        "composite_val_spx": to_spx(c_val),
        "composite_high": round(comp["high"], 2) if comp["high"] else None,
        "composite_low": round(comp["low"], 2) if comp["low"] else None,
        "chart": chart,
        "candles": build_candles(frames, day_keys, set(comp_days)),
        "candle_interval": CANDLE_LABEL,
        "naked_pocs": naked[:6],
        "lvn_zones": lvn[:6],
        "interval": INTERVAL,
        "bin_size": BIN_SIZE,
        "rth_only": True,
        "limitation": ("Built from SPY regular-hours bars (09:30-16:00 ET). The "
                       "overnight auction is not captured — no free intraday ES "
                       "futures source exists."),
    }
    payload["flags"] = _flags(payload)
    payload["commentary"] = _commentary(payload)
    return payload


def _flags(p: dict) -> dict:
    spot = p["spy_spot"]
    vah, val = p["composite_vah"], p["composite_val"]
    in_value = bool(vah is not None and val is not None and val <= spot <= vah)
    nearest_lvn = p["lvn_zones"][0] if p["lvn_zones"] else None
    return {
        "in_value": in_value,
        "above_value": bool(vah is not None and spot > vah),
        "below_value": bool(val is not None and spot < val),
        "has_naked_poc": bool(p["naked_pocs"]),
        "near_lvn": bool(nearest_lvn
                         and nearest_lvn["lo"] <= spot <= nearest_lvn["hi"]),
    }


def _commentary(p: dict) -> dict:
    f = p["flags"]
    sentences: list[str] = []
    spot = p["spy_spot"]

    if f["in_value"]:
        headline = (f"Balanced — SPY {spot:.2f} inside the "
                    f"{p['composite_val']:.2f}–{p['composite_vah']:.2f} value area")
        sentences.append(
            f"Price is accepted inside the {p['composite_sessions']}-session value area "
            f"({p['composite_val']:.2f}–{p['composite_vah']:.2f}, POC "
            f"{p['composite_poc']:.2f}); rotational conditions, and the edges are the "
            f"references that matter.")
    elif f["above_value"]:
        headline = f"Above value — SPY {spot:.2f} over VAH {p['composite_vah']:.2f}"
        sentences.append(
            f"Price is trading above the composite value area high "
            f"({p['composite_vah']:.2f}); acceptance up here builds a new distribution, "
            f"rejection puts the {p['composite_poc']:.2f} POC back in play.")
    elif f["below_value"]:
        headline = f"Below value — SPY {spot:.2f} under VAL {p['composite_val']:.2f}"
        sentences.append(
            f"Price is below the composite value area low ({p['composite_val']:.2f}); "
            f"unfinished business sits back up at the {p['composite_poc']:.2f} POC.")
    else:
        headline = f"SPY {spot:.2f} — profile built"

    if p["naked_pocs"]:
        n = p["naked_pocs"][0]
        side = "above" if n["above_spot"] else "below"
        sentences.append(
            f"Nearest naked POC is {n['spy']:.2f} ({n['spx']:.0f} SPX) {side} spot, left "
            f"from {n['date']} and never traded back through — unfinished business that "
            f"tends to get revisited.")
    else:
        sentences.append(
            "No naked POCs in the window; recent auctions have all been revisited, so "
            "there is no obvious magnet from unfinished business.")

    if p["lvn_zones"]:
        z = p["lvn_zones"][0]
        where = "spanning spot" if f["near_lvn"] else (
            "above" if z["lo"] > spot else "below")
        sentences.append(
            f"Thinnest corridor is {z['lo']:.2f}–{z['hi']:.2f} ({z['lo_spx']:.0f}–"
            f"{z['hi_spx']:.0f} SPX) {where}, at {z['mean_volume_share'] * 100:.0f}% of "
            f"average volume — nobody agreed on value there, so price travels through it "
            f"fast. Do not treat a gamma level inside it as support.")

    return {"headline": headline, "warnings": [], "sentences": sentences}


def refresh(spx_spot: float | None = None) -> dict:
    """Fetch + compute. `spx_spot` can be supplied to avoid a duplicate quote."""
    bars = fetch_bars()
    return compute(bars, spx_spot if spx_spot is not None else fetch_spx_spot())
