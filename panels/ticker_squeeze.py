"""Short interest, ownership, analyst and trend panel for one ticker.

Everything here comes from a single Finviz quote-page request (see
``panels/_finviz.py``), which is why this panel is cheap enough to sit inside
one press of ANALYSE alongside the option-chain work.

The four blocks answer four different questions about who owns the name and
what they think of it:

  * **Squeeze** — how much of the float is sold short, and how many days of
    average volume it would take to cover. Short interest is fuel, not a
    direction: a high reading cuts both ways and is scored as *potential
    energy*, with the sign supplied by trend and flow elsewhere.
  * **Ownership** — institutional and insider holdings, and crucially the
    *change* in each. Insiders selling into a rally is a different story from
    insiders holding through one.
  * **Analyst** — consensus rating and the price target relative to spot. A
    target below spot while the tape rallies is one of the cleaner bearish
    divergences available for free.
  * **Trend** — distance from the three moving averages, RSI, relative volume
    and position in the 52-week range, so sentiment is never read without the
    price context that frames it.

Finviz reports the SMA fields as *percentage distance from price*, not as the
average's level — ``SMA200 = 12.20%`` means price is 12.2% above its 200-day.
"""

from __future__ import annotations

from . import _finviz

# --------------------------------------------------------------------------- #
# Thresholds
# --------------------------------------------------------------------------- #

# Short float bands (fraction of float sold short).
SHORT_EXTREME = 0.20
SHORT_HIGH = 0.10
SHORT_MODERATE = 0.05
SHORT_LOW = 0.02

# Days-to-cover bands (short interest / average daily volume).
COVER_HIGH = 5.0
COVER_ELEVATED = 3.0
COVER_NORMAL = 1.0

# Analyst consensus, Finviz's 1 (Strong Buy) .. 5 (Strong Sell) scale.
RECOM_STRONG_BUY = 1.5
RECOM_BUY = 2.5
RECOM_HOLD = 3.5
RECOM_SELL = 4.5

RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0

# Ownership-change magnitudes worth remarking on (fractions).
OWNERSHIP_MOVE = 0.01
INSIDER_MOVE = 0.005

# Relative volume that counts as a genuine participation spike.
REL_VOLUME_SPIKE = 1.5

NEAR_HIGH = 0.05   # within 5% of the 52-week high
NEAR_LOW = 0.10    # within 10% of the 52-week low


def refresh(symbol: str, quote: dict | None = None) -> dict:
    """Panel entry point. ``quote`` lets the caller share one Finviz fetch."""
    return compute(quote or _finviz.fetch_quote(symbol), symbol)


def compute(quote: dict, symbol: str) -> dict:
    """Pure: Finviz quote dict -> panel payload."""
    snap = quote.get("snapshot") or {}
    num, pct, pct2 = _finviz.num, _finviz.pct, _finviz.pct2

    spot = num(snap.get("Price"))
    short_float = pct(snap.get("Short Float"))
    days_to_cover = num(snap.get("Short Ratio"))
    target = num(snap.get("Target Price"))
    recom = num(snap.get("Recom"))

    payload = {
        "symbol": symbol,
        "company": quote.get("company"),
        "spot": spot,
        "market_cap": num(snap.get("Market Cap")),
        "index": _finviz.clean(snap.get("Index")),
        "shortable": _shortable(snap.get("Option/Short")),

        # --- squeeze -------------------------------------------------------- #
        "short_float": short_float,
        "short_interest": num(snap.get("Short Interest")),
        "days_to_cover": days_to_cover,
        "float_shares": num(snap.get("Shs Float")),
        "squeeze_score": _squeeze_score(short_float, days_to_cover),
        "squeeze_band": _squeeze_band(short_float),

        # --- ownership ------------------------------------------------------ #
        "inst_own": pct(snap.get("Inst Own")),
        "inst_trans": pct(snap.get("Inst Trans")),
        "insider_own": pct(snap.get("Insider Own")),
        "insider_trans": pct(snap.get("Insider Trans")),

        # --- analyst -------------------------------------------------------- #
        "target_price": target,
        "target_upside": (target / spot - 1.0) if (target and spot) else None,
        "recom": recom,
        "recom_label": _recom_label(recom),

        # --- trend ---------------------------------------------------------- #
        "rsi": num(snap.get("RSI (14)")),
        "rel_volume": num(snap.get("Rel Volume")),
        "avg_volume": num(snap.get("Avg Volume")),
        "beta": num(snap.get("Beta")),
        "atr": num(snap.get("ATR (14)")),
        "volatility_week": pct(snap.get("Volatility")),
        "volatility_month": pct(_finviz.second(snap.get("Volatility"))),
        "sma20": pct(snap.get("SMA20")),
        "sma50": pct(snap.get("SMA50")),
        "sma200": pct(snap.get("SMA200")),
        "high_52w": _finviz.num(_finviz.first(snap.get("52W High"))),
        "low_52w": _finviz.num(_finviz.first(snap.get("52W Low"))),
        "from_high": pct2(snap.get("52W High")),
        "from_low": pct2(snap.get("52W Low")),
        "perf_week": pct(snap.get("Perf Week")),
        "perf_month": pct(snap.get("Perf Month")),
        "perf_quarter": pct(snap.get("Perf Quarter")),
        "perf_ytd": pct(snap.get("Perf YTD")),

        # --- context reused by other panels --------------------------------- #
        "earnings_raw": _finviz.clean(snap.get("Earnings")),
        "dividend_yield": _dividend_yield(snap),
        "sector_perf": None,
    }

    payload["trend"] = _trend_state(payload)
    payload["commentary"] = _commentary(payload)
    return payload


# --------------------------------------------------------------------------- #
# Derivations
# --------------------------------------------------------------------------- #

def _shortable(raw) -> bool | None:
    """Finviz writes 'Option/Short' as 'Yes / Yes'; the second half is shortability."""
    text = _finviz.clean(raw)
    if not text or "/" not in text:
        return None
    return text.split("/")[1].strip().lower() == "yes"


def _dividend_yield(snap: dict) -> float | None:
    """Yield out of 'Dividend TTM' -> '0.56 (6.48%)'.

    Threaded into the positioning panel's Black-Scholes inputs, where the
    generic module otherwise assumes a zero yield.
    """
    raw = _finviz.clean(snap.get("Dividend TTM")) or _finviz.clean(snap.get("Dividend Est."))
    if not raw or "(" not in raw:
        return None
    inner = raw.split("(", 1)[1].rstrip(")")
    return _finviz.pct(inner)


def _squeeze_score(short_float: float | None, days_to_cover: float | None) -> float | None:
    """0-100 potential-energy reading. Not a direction — see the module docstring.

    Short float carries twice the weight of days-to-cover: the size of the short
    base matters more than how quickly it could theoretically exit, and
    days-to-cover is the noisier input because average volume moves around.
    """
    if short_float is None and days_to_cover is None:
        return None

    float_part = min(short_float / SHORT_EXTREME, 1.0) if short_float is not None else None
    cover_part = min(days_to_cover / COVER_HIGH, 1.0) if days_to_cover is not None else None

    if float_part is None:
        return round(100 * cover_part, 1)
    if cover_part is None:
        return round(100 * float_part, 1)
    return round(100 * (2 * float_part + cover_part) / 3, 1)


def _squeeze_band(short_float: float | None) -> str:
    if short_float is None:
        return "unknown"
    if short_float >= SHORT_EXTREME:
        return "extreme"
    if short_float >= SHORT_HIGH:
        return "high"
    if short_float >= SHORT_MODERATE:
        return "moderate"
    if short_float >= SHORT_LOW:
        return "low"
    return "negligible"


def _recom_label(recom: float | None) -> str | None:
    if recom is None:
        return None
    if recom < RECOM_STRONG_BUY:
        return "Strong Buy"
    if recom < RECOM_BUY:
        return "Buy"
    if recom < RECOM_HOLD:
        return "Hold"
    if recom < RECOM_SELL:
        return "Sell"
    return "Strong Sell"


def _trend_state(p: dict) -> dict:
    """Agreement of the three SMA distances, plus where price sits in its range."""
    smas = [p["sma20"], p["sma50"], p["sma200"]]
    known = [s for s in smas if s is not None]

    if not known:
        state, label = "unknown", "Trend unknown"
    else:
        above = sum(1 for s in known if s > 0)
        if above == len(known):
            state, label = "up", "Above all moving averages"
        elif above == 0:
            state, label = "down", "Below all moving averages"
        else:
            state, label = "mixed", f"Above {above} of {len(known)} moving averages"

    return {
        "state": state,
        "label": label,
        "above_count": sum(1 for s in known if s > 0),
        "sma_count": len(known),
        "near_high": p["from_high"] is not None and abs(p["from_high"]) <= NEAR_HIGH,
        "near_low": p["from_low"] is not None and p["from_low"] <= NEAR_LOW,
    }


# --------------------------------------------------------------------------- #
# Commentary
# --------------------------------------------------------------------------- #

def _commentary(p: dict) -> dict:
    sentences: list[str] = []
    warnings: list[str] = []

    sym = p["symbol"]
    sf, dtc = p["short_float"], p["days_to_cover"]
    band = p["squeeze_band"]

    # --- short interest ---------------------------------------------------- #
    if sf is not None:
        cover = f", {dtc:.1f} days to cover" if dtc is not None else ""
        if band in ("extreme", "high"):
            article = "an" if band[0] in "aeiou" else "a"
            sentences.append(
                f"{sf:.1%} of {sym}'s float is sold short{cover} — {article} {band} "
                f"short base. That is fuel in either direction: it accelerates a "
                f"rally as shorts cover and deepens a decline as they press.")
        elif band == "moderate":
            sentences.append(
                f"Short interest is moderate at {sf:.1%} of float{cover} — enough "
                f"to matter on a catalyst, not enough to drive the tape on its own.")
        else:
            sentences.append(
                f"Short interest is {band} at {sf:.1%} of float{cover}; the short "
                f"side is not a meaningful part of this story.")
        if p["shortable"] is False:
            warnings.append("⚠ Finviz reports this name as not shortable — treat "
                            "the short-interest reading with care.")

    # --- analyst ------------------------------------------------------------ #
    upside, label = p["target_upside"], p["recom_label"]
    if upside is not None and label:
        if upside < 0:
            sentences.append(
                f"The consensus price target of {p['target_price']:.2f} sits "
                f"{abs(upside):.1%} *below* spot while the rating is {label} — "
                f"analysts have been overtaken by the tape and have not yet "
                f"revised. Treat the rating as stale, not as endorsement.")
        elif upside > 0.25:
            sentences.append(
                f"Consensus {label} with a {p['target_price']:.2f} target, "
                f"{upside:.1%} above spot — a wide gap that either prices in a "
                f"recovery the market has not accepted, or is itself stale.")
        else:
            sentences.append(
                f"Consensus is {label} with a {p['target_price']:.2f} target, "
                f"{upside:+.1%} against spot.")

    # --- ownership ---------------------------------------------------------- #
    ins_t, inst_t = p["insider_trans"], p["inst_trans"]
    parts = []
    if ins_t is not None and abs(ins_t) >= INSIDER_MOVE:
        parts.append(f"insiders {'bought' if ins_t > 0 else 'sold'} "
                     f"({ins_t:+.2%} of holdings)")
    elif ins_t is not None:
        parts.append("insider holdings unchanged")
    if inst_t is not None and abs(inst_t) >= OWNERSHIP_MOVE:
        parts.append(f"institutions {'added' if inst_t > 0 else 'reduced'} "
                     f"({inst_t:+.2%})")
    if parts:
        own = (f" Institutions hold {p['inst_own']:.0%} of shares."
               if p["inst_own"] is not None else "")
        sentences.append("Recently " + " and ".join(parts) + "." + own)

    # --- trend -------------------------------------------------------------- #
    trend = p["trend"]
    rsi = p["rsi"]
    bits = [trend["label"].lower()]
    if p["from_high"] is not None:
        bits.append(f"{abs(p['from_high']):.1%} off the 52-week high")
    if rsi is not None:
        if rsi >= RSI_OVERBOUGHT:
            bits.append(f"RSI {rsi:.0f} (overbought)")
        elif rsi <= RSI_OVERSOLD:
            bits.append(f"RSI {rsi:.0f} (oversold)")
        else:
            bits.append(f"RSI {rsi:.0f}")
    sentences.append("Price is " + ", ".join(bits) + ".")

    rv = p["rel_volume"]
    if rv is not None and rv >= REL_VOLUME_SPIKE:
        sentences.append(f"Relative volume {rv:.2f}x — participation is well above "
                         f"normal, so today's move carries more information than usual.")

    if p["spot"] is None:
        warnings.append("⚠ No price returned by Finviz; derived fields are unavailable.")

    headline = _headline(p)
    return {"headline": headline, "warnings": warnings, "sentences": sentences}


def _headline(p: dict) -> str:
    band = p["squeeze_band"]
    score = p["squeeze_score"]
    trend = p["trend"]["state"]

    trend_word = {"up": "uptrend", "down": "downtrend",
                  "mixed": "mixed trend", "unknown": "trend unknown"}[trend]
    article = "an" if trend_word[0] in "aeiou" else "a"

    if band in ("extreme", "high"):
        return (f"{band.title()} short base ({p['short_float']:.1%} of float) "
                f"in {article} {trend_word} — squeeze score {score:.0f}/100")
    if score is not None:
        return f"{band.title()} short interest, {trend_word} — squeeze score {score:.0f}/100"
    return f"Ownership and trend read — {trend_word}"
