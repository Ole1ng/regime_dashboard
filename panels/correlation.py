"""Implied correlation — RESEARCH.md §5.

CBOE's COR1M / COR3M back implied average correlation out of index variance
versus the weighted constituent variances:

    sigma_index^2 ~= SUM w_i^2 sigma_i^2  +  SUM_{i!=j} w_i w_j sigma_i sigma_j rho

Read it as the **transmission dial**: how much single-stock energy actually
reaches the index.

The reading that matters is the *percentile*, and its interpretation is
inverted relative to intuition. **Low implied correlation is a fragility
signal, not calm.** Low correlation means index vol is cheap against
single-stock vol, dispersion trades (short index vol / long single-name vol)
are crowded, and the index has been artificially quiet — so index IV is low and
the gamma profile looks comfortable. A shock that jolts correlation upward
forces dispersion desks to buy index vol back, spiking it far more than the
news warrants. August 2024 is the case study.

The most actionable state is therefore **a sharp rise from a low base**: the
unwind starting. That is flagged explicitly.

Full daily history back to 2006 is available from CBOE (DATA_SOURCES.md), so
percentiles are real from the first run — no warm-up period.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime

import numpy as np
import requests

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

QUOTE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_{sym}.json"
HISTORY_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{sym}_History.csv"

SPARK_DAYS = 260          # ~1 year of daily closes for the panel sparkline
TRADING_DAYS_YEAR = 252

# Percentile bands (RESEARCH.md §5). Low = fragile, high = macro-driven.
PCTL_EXTREME_LOW = 10.0
PCTL_LOW = 25.0
PCTL_HIGH = 75.0
PCTL_EXTREME_HIGH = 90.0
# A "spike from the lows" needs both a low base and a real one-day jump.
SPIKE_PCT = 8.0


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #

def fetch_quote(sym: str, timeout: float = 25.0) -> dict:
    r = requests.get(QUOTE_URL.format(sym=sym), headers=_HEADERS, timeout=timeout)
    r.raise_for_status()
    d = (r.json().get("data") or {})
    price = d.get("current_price")
    if price is None:
        price = d.get("close")
    if price is None:
        raise ValueError(f"no price for {sym}")
    return {
        "symbol": d.get("symbol", sym),
        "value": float(price),
        "prev_close": float(d["prev_day_close"]) if d.get("prev_day_close") else None,
        "open": d.get("open"), "high": d.get("high"), "low": d.get("low"),
    }


def fetch_history(sym: str, timeout: float = 30.0) -> list[tuple[date, float]]:
    """Daily closes, oldest first. CBOE serves COR1M/COR3M back to 2006."""
    r = requests.get(HISTORY_URL.format(sym=sym), headers=_HEADERS, timeout=timeout)
    r.raise_for_status()
    out: list[tuple[date, float]] = []
    for row in csv.DictReader(io.StringIO(r.text)):
        raw = (row.get("DATE") or "").strip()
        close = (row.get("CLOSE") or "").strip()
        if not raw or not close:
            continue
        for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
            try:
                d = datetime.strptime(raw, fmt).date()
                break
            except ValueError:
                d = None
        if d is None:
            continue
        try:
            v = float(close)
        except ValueError:
            continue
        if v > 0:
            out.append((d, v))
    out.sort(key=lambda t: t[0])
    if not out:
        raise ValueError(f"no usable history rows for {sym}")
    return out


# --------------------------------------------------------------------------- #
# Percentiles
# --------------------------------------------------------------------------- #

def percentile_rank(values, current: float) -> float | None:
    """Share of `values` strictly below `current`, as 0-100."""
    arr = np.asarray(list(values), dtype=float)
    if arr.size == 0:
        return None
    return float((arr < current).sum()) / arr.size * 100.0


def _window(history: list[tuple[date, float]], years: float | None):
    if years is None:
        return [v for _, v in history]
    n = int(TRADING_DAYS_YEAR * years)
    return [v for _, v in history[-n:]]


# --------------------------------------------------------------------------- #
# Compute
# --------------------------------------------------------------------------- #

def compute(cor1m: dict, cor3m: dict,
            hist1m: list[tuple[date, float]],
            hist3m: list[tuple[date, float]] | None = None) -> dict:
    v1, v3 = cor1m["value"], cor3m["value"]

    pctl_2y = percentile_rank(_window(hist1m, 2), v1)
    pctl_5y = percentile_rank(_window(hist1m, 5), v1)
    pctl_all = percentile_rank(_window(hist1m, None), v1)
    pctl3_2y = percentile_rank(_window(hist3m or [], 2), v3) if hist3m else None

    prev = cor1m.get("prev_close")
    change = (v1 - prev) if prev else None
    change_pct = (change / prev * 100.0) if (change is not None and prev) else None

    lo2y = min(_window(hist1m, 2), default=None)
    hi2y = max(_window(hist1m, 2), default=None)

    spark = [{"date": d.isoformat(), "value": v}
             for d, v in hist1m[-SPARK_DAYS:]]

    regime, regime_note = _classify(pctl_2y)
    flags = {
        "extreme_low": pctl_2y is not None and pctl_2y < PCTL_EXTREME_LOW,
        "low": pctl_2y is not None and pctl_2y < PCTL_LOW,
        "high": pctl_2y is not None and pctl_2y > PCTL_HIGH,
        "extreme_high": pctl_2y is not None and pctl_2y > PCTL_EXTREME_HIGH,
        # The unwind trigger: crowded dispersion starting to come off.
        "spiking_from_lows": bool(
            pctl_2y is not None and pctl_2y < PCTL_LOW
            and change_pct is not None and change_pct > SPIKE_PCT),
        "term_inverted": v1 > v3,   # near-term correlation above longer-term
    }

    payload = {
        "cor1m": round(v1, 2),
        "cor3m": round(v3, 2),
        "cor1m_prev": prev,
        "cor1m_change": round(change, 2) if change is not None else None,
        "cor1m_change_pct": round(change_pct, 2) if change_pct is not None else None,
        "spread": round(v1 - v3, 2),
        "cor1m_pctl_2y": round(pctl_2y, 1) if pctl_2y is not None else None,
        "cor1m_pctl_5y": round(pctl_5y, 1) if pctl_5y is not None else None,
        "cor1m_pctl_all": round(pctl_all, 1) if pctl_all is not None else None,
        "cor3m_pctl_2y": round(pctl3_2y, 1) if pctl3_2y is not None else None,
        "low_2y": round(lo2y, 2) if lo2y is not None else None,
        "high_2y": round(hi2y, 2) if hi2y is not None else None,
        "history_start": hist1m[0][0].isoformat(),
        "history_end": hist1m[-1][0].isoformat(),
        "history_days": len(hist1m),
        "spark": spark,
        "regime": regime,
        "regime_note": regime_note,
        "flags": flags,
    }
    payload["commentary"] = _commentary(payload)
    return payload


def _classify(pctl: float | None) -> tuple[str, str]:
    if pctl is None:
        return "unknown", "No history available for a percentile rank."
    if pctl < PCTL_EXTREME_LOW:
        return "crowded dispersion", (
            "Index vol is exceptionally cheap against single-stock vol. The index "
            "looks calm because correlation is suppressing it, not because risk is "
            "low — the classic pre-unwind configuration.")
    if pctl < PCTL_LOW:
        return "low correlation", (
            "Dispersion is crowded; single names can churn while the index barely "
            "moves. This reinforces long-gamma pinning and understates fragility.")
    if pctl > PCTL_EXTREME_HIGH:
        return "macro shock", (
            "Everything is moving together. Hedges work, single-name selection does "
            "not, and index-level gamma effects dominate the tape.")
    if pctl > PCTL_HIGH:
        return "high correlation", (
            "Macro is driving. Trends persist and index gamma effects are amplified "
            "rather than diluted.")
    return "neutral", "Correlation is mid-range; no strong transmission signal."


def ordinal(n: float) -> str:
    """1 -> '1st', 2 -> '2nd', 11 -> '11th', 21 -> '21st'."""
    i = int(round(n))
    if 10 <= i % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(i % 10, "th")
    return f"{i}{suffix}"


def _commentary(p: dict) -> dict:
    f = p["flags"]
    sentences: list[str] = []
    warnings: list[str] = []

    pctl = p["cor1m_pctl_2y"]
    headline = f"COR1M {p['cor1m']:.2f} — {p['regime']}"
    if pctl is not None:
        headline += f" ({ordinal(pctl)} pctl, 2y)"

    if pctl is not None:
        sentences.append(
            f"COR1M at {p['cor1m']:.2f} sits in the {ordinal(pctl)} percentile of the "
            f"last two years (range {p['low_2y']:.2f}–{p['high_2y']:.2f}), and the "
            f"{ordinal(p['cor1m_pctl_all'])} of the full series since "
            f"{p['history_start'][:4]}.")
    sentences.append(p["regime_note"])

    if f["spiking_from_lows"]:
        sentences.append(
            f"Correlation is jumping off the lows — up {p['cor1m_change_pct']:.1f}% today "
            f"from {p['cor1m_prev']:.2f}. This is the dispersion unwind starting: desks "
            f"short index vol against long single-name vol have to buy index vol back, "
            f"which spikes it far beyond what the news alone justifies.")
    elif f["extreme_low"]:
        sentences.append(
            "Watch for the first sharp uptick — a rise off a base this low is the "
            "signal, not the level itself.")

    if f["term_inverted"]:
        sentences.append(
            f"COR1M above COR3M ({p['cor1m']:.2f} vs {p['cor3m']:.2f}) puts near-term "
            f"correlation above the longer tenor — stress is being priced now rather "
            f"than later.")
    else:
        sentences.append(
            f"COR1M sits {abs(p['spread']):.2f} below COR3M, the normal ordering.")

    if f["extreme_high"]:
        sentences.append(
            "At this level the index is a single macro instrument; treat gamma levels "
            "as the dominant map and expect single-name signals to be noise.")

    return {"headline": headline, "warnings": warnings, "sentences": sentences}


def refresh() -> dict:
    hist3m = None
    try:
        hist3m = fetch_history("COR3M")
    except Exception:
        pass    # the COR3M percentile is a nice-to-have, not load-bearing
    return compute(fetch_quote("COR1M"), fetch_quote("COR3M"),
                   fetch_history("COR1M"), hist3m)
