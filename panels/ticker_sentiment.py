"""Composite sentiment: the synthesis layer over every other Tab 2 panel.

A pure function of the six panels that precede it, in the same way ``regime.py``
is a pure function of the Tab 1 panels — it performs no fetching and reads
whatever those panels left in the store, including stale payloads sitting behind
an error badge.

Two design decisions carry most of the weight here.

**Dealer gamma is not a direction.** Positive gamma means dealer hedging damps
moves; negative gamma means it amplifies them. Neither is bullish or bearish —
mapping "positive gamma" to "bullish" is the single most common error in reading
this data, and Tab 1's own caveats say so. Direction is therefore taken from
three genuinely directional inputs (spot versus the flip level, net delta
exposure, and the asymmetry of the call and put walls), while the gamma *regime*
acts as a modifier that flags amplification risk and reduces confidence.

**Short interest is fuel, not a direction either.** A heavily shorted name that
is rising has squeeze potential; the identical short interest on a name that is
falling means the shorts are winning. The squeeze sub-score is therefore signed
by realised momentum rather than assumed bullish.

The single number is navigation, not a verdict. Six loosely-correlated sentiment
proxies blended together is a summary — the sub-score breakdown and, above all,
the divergences between them are the actual output.
"""

from __future__ import annotations

import statistics

# --------------------------------------------------------------------------- #
# Weights — they sum to 100, and are renormalised over whatever is available.
# --------------------------------------------------------------------------- #

WEIGHTS = {
    "vol": 25,          # options: the only input where money is at risk
    "positioning": 20,  # dealer hedging pressure and level structure
    "news": 15,
    "social": 15,
    "squeeze": 15,
    "analyst": 10,
}

LABELS = {
    "vol": "Options / volatility",
    "positioning": "Dealer positioning",
    "news": "News tone",
    "social": "Retail chatter",
    "squeeze": "Short interest",
    "analyst": "Analyst view",
}

# Sample sizes below which a sub-score's weight is scaled down rather than
# dropped — a tone reading over three headlines should not count as fully as
# one over forty.
NEWS_FULL_N = 12
SOCIAL_FULL_N = 25

# Above this market cap a registered offering is ordinary treasury activity,
# not a dilution event — see ticker_events.DILUTION_CAP_LIMIT.
DILUTION_CAP_LIMIT = 2e9

BANDS = [
    (20, "strongly-bearish", "STRONGLY BEARISH"),
    (40, "bearish", "BEARISH"),
    (60, "mixed", "MIXED"),
    (80, "bullish", "BULLISH"),
    (101, "strongly-bullish", "STRONGLY BULLISH"),
]

CONFIDENCE_FLOOR = 10
MISSING_PENALTY = 15
DIVERGENCE_PENALTY = 10
DIVERGENCE_PENALTY_CAP = 40

CAVEAT = ("A blend of six loosely-correlated sentiment proxies. Read the "
          "breakdown and the divergences, not the number.")

PANEL_KEYS = ("t2_positioning", "t2_vol", "t2_news", "t2_social",
              "t2_squeeze", "t2_events")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _payload(panels: dict, key: str):
    """Accept either raw payloads or store records ({payload, status, ...}).

    Lifted from ``regime._payload`` so both tabs treat a failed panel the same
    way: a cached payload behind an error badge is still usable, an error with
    nothing cached is not.
    """
    value = panels.get(key)
    if value is None:
        return None
    if isinstance(value, dict) and "payload" in value and "status" in value:
        if value.get("status") == "error" and not value.get("payload"):
            return None
        return value.get("payload")
    return value


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _score(x: float | None, scale: float, invert: bool = False) -> float | None:
    """Map a signed reading onto 0-100 with 50 neutral.

    ``scale`` is the magnitude that saturates the sub-score. ``invert`` flips
    the sense for readings where a higher number is more bearish.
    """
    if x is None or not scale:
        return None
    unit = _clamp(x / scale)
    if invert:
        unit = -unit
    return 50.0 + 50.0 * unit


def _mean(values: list[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return sum(known) / len(known) if known else None


def _vote(key: str, reading: str, score: float | None,
          detail: str | None = None) -> dict:
    return {"key": key, "label": LABELS[key], "score": score,
            "weight": WEIGHTS[key], "reading": reading, "detail": detail,
            "available": score is not None}


# --------------------------------------------------------------------------- #
# Sub-scores
# --------------------------------------------------------------------------- #

def _positioning_score(p: dict | None) -> tuple[float | None, str, dict]:
    """Direction from flip/DEX/wall asymmetry; gamma regime only as a modifier."""
    flags = {"amplifying": False, "near_flip": False}
    if not p or p.get("not_found") or p.get("spot") is None:
        return None, "no options positioning", flags

    spot = p.get("spot")
    flags["amplifying"] = p.get("regime") == "negative"

    # NOTE ON DEX. Net dealer delta is deliberately NOT used as a directional
    # input. Under this project's sign convention (dealers long calls, short
    # puts) a call contributes +delta*OI and a put contributes -delta*OI with
    # delta already negative — so every contract contributes a POSITIVE amount
    # and net DEX is identically equal to gross DEX. Verified at +1.000000 for
    # WEN, NVDA and TSLA. DEX therefore measures the SIZE of the hedging
    # requirement, never its direction, and reading it directionally is the
    # same category error as calling positive gamma bullish.

    # 1. Where is spot relative to the gamma flip level?
    cushion = p.get("cushion_pct")
    s_flip = _score(cushion, 0.02)
    if cushion is not None:
        flags["near_flip"] = abs(cushion) <= 0.003

    # 2. Wall asymmetry: more room to the call wall than to the put wall is
    #    structurally permissive, and vice versa.
    #
    #    Only meaningful when the walls actually straddle spot. On a thin
    #    single-name chain both walls frequently land on the SAME strike (WEN
    #    puts both at 9.0 against an 8.61 spot), where the difference of the two
    #    distances is an artefact that saturates the term rather than measuring
    #    anything.
    call_wall, put_wall = p.get("call_wall"), p.get("put_wall")
    s_walls = None
    if call_wall and put_wall and spot and put_wall < spot < call_wall:
        asymmetry = ((call_wall - spot) - (spot - put_wall)) / (0.04 * spot)
        s_walls = _score(asymmetry, 1.0)

    # 3. Open-interest gravity: the biggest OI strike sitting above or below
    #    spot is a weak directional pull, and unlike the flip level it is
    #    almost always available on a single name.
    magnet = p.get("nearest_magnet")
    magnet = magnet.get("strike") if isinstance(magnet, dict) else magnet
    s_magnet = _score((magnet / spot - 1.0), 0.06) \
        if (magnet and spot) else None

    terms = [s_flip, s_walls, s_magnet]
    score = _mean(terms)
    if score is not None:
        # Shrink toward neutral when only some of the three structural terms
        # are available. One saturated term should not produce the same
        # confident 100 that three agreeing terms would: less evidence has to
        # mean a reading closer to neutral, not an equally extreme one.
        known = sum(1 for t in terms if t is not None)
        score = 50.0 + (score - 50.0) * (known / len(terms)) ** 0.5
    if score is None:
        # No flip level, walls on one side, no magnet: there is genuinely no
        # directional read here. Returning None drops the sub-score and
        # renormalises the weights rather than inventing a neutral 50.
        return None, "no directional structure in the chain", flags

    bits = []
    if cushion is not None:
        bits.append(f"spot {cushion:+.2%} vs flip")
    elif p.get("no_flip"):
        bits.append("no flip level")
    if magnet and spot:
        bits.append(f"OI magnet {magnet:g} ({magnet / spot - 1:+.1%})")
    if p.get("regime"):
        bits.append(f"{p['regime']} gamma")
    return score, ", ".join(bits) or "positioning read", flags


def _vol_score(p: dict | None) -> tuple[float | None, str]:
    """Skew, put/call, term structure and IV-RV. Rich downside protection = bearish."""
    if not p:
        return None, "no options data"

    # Skew normalised by ATM. A 6% put bid is ordinary; 20% is a real fear bid.
    skew = p.get("skew_25d_pct")
    s_skew = _score((skew - 0.06) if skew is not None else None, 0.14, invert=True)

    pcr_oi = p.get("pcr_oi")
    s_pcr = _score((pcr_oi - 0.80) if pcr_oi is not None else None, 0.60, invert=True)

    pcr_vol = p.get("pcr_vol")
    s_pcrv = _score((pcr_vol - 0.75) if pcr_vol is not None else None, 0.65, invert=True)

    # Contango is the resting state; inversion means near-dated stress.
    s_term = _score(p.get("term_slope"), 0.05)

    # Implied richly above realised is a fear/event premium.
    s_ivrv = _score(p.get("ivrv_spread"), 0.15, invert=True)

    parts = [(s_skew, 0.35), (s_pcr, 0.20), (s_pcrv, 0.15),
             (s_term, 0.15), (s_ivrv, 0.15)]
    available = [(s, w) for s, w in parts if s is not None]
    if not available:
        return None, "no usable volatility signal"

    total = sum(w for _, w in available)
    score = sum(s * w for s, w in available) / total

    bits = []
    if p.get("skew_state") not in (None, "unknown"):
        bits.append(f"{p['skew_state']} skew")
    if p.get("ivrv_state") not in (None, "unknown"):
        bits.append(f"IV {p['ivrv_state']} vs realised")
    if pcr_oi is not None:
        bits.append(f"P/C {pcr_oi:.2f}")
    return score, ", ".join(bits) or "volatility read"


def _news_score(p: dict | None) -> tuple[float | None, str, float]:
    """VADER mean, with the weight scaled by how many headlines back it."""
    if not p or p.get("empty"):
        return None, "no headlines", 0.0
    s = p.get("sentiment") or {}
    n = s.get("n") or 0
    if not n:
        return None, "no headlines", 0.0
    score = _score(s.get("mean"), 0.35)
    factor = min(1.0, n / NEWS_FULL_N)
    return score, f"{s.get('tone', '?').lower()}, {n} headlines", factor


def _social_score(p: dict | None) -> tuple[float | None, str, float]:
    if not p or p.get("empty"):
        return None, "no chatter", 0.0
    n = p.get("n") or 0
    blended = p.get("blended")
    if blended is None or not n:
        return None, "no scored messages", 0.0
    score = _score(blended, 1.0)
    factor = min(1.0, n / SOCIAL_FULL_N)
    bull = p.get("bull_pct")
    reading = f"{p.get('tone', '?').lower()}, {n} messages"
    if bull is not None:
        reading += f", {bull:.0%} bullish"
    return score, reading, factor


def _squeeze_score(p: dict | None) -> tuple[float | None, str]:
    """Squeeze potential, signed by realised momentum — see the module docstring."""
    if not p:
        return None, "no short-interest data"
    short_float = p.get("short_float")
    dtc = p.get("days_to_cover")
    if short_float is None and dtc is None:
        return None, "no short-interest data"

    potential = 0.0
    if short_float is not None:
        potential += 0.65 * _clamp((short_float - 0.05) / 0.20, 0.0, 1.0)
    if dtc is not None:
        potential += 0.35 * _clamp((dtc - 2.0) / 5.0, 0.0, 1.0)

    momentum = p.get("perf_month")
    if momentum is None:
        # Without a direction, short interest is genuinely ambiguous.
        return 50.0, f"{short_float:.1%} of float short, direction unknown" \
            if short_float is not None else "short interest, direction unknown"

    sign = _clamp(momentum / 0.05)
    score = 50.0 + 45.0 * potential * sign

    reading = (f"{short_float:.1%} of float short" if short_float is not None
               else f"{dtc:.1f} days to cover")
    reading += f", {momentum:+.1%} on the month"
    return score, reading


def _analyst_score(p: dict | None) -> tuple[float | None, str]:
    if not p:
        return None, "no analyst data"
    recom, upside = p.get("recom"), p.get("target_upside")
    # Finviz's scale runs 1 (Strong Buy) to 5 (Strong Sell).
    s_recom = 100.0 * (5.0 - recom) / 4.0 if recom is not None else None
    s_target = _score(upside, 0.30)
    score = _mean([s_recom, s_target])
    if score is None:
        return None, "no analyst data"

    bits = []
    if p.get("recom_label"):
        bits.append(p["recom_label"])
    if upside is not None:
        bits.append(f"target {upside:+.1%} vs spot")
    return score, ", ".join(bits)


# --------------------------------------------------------------------------- #
# Compute
# --------------------------------------------------------------------------- #

def compute(panels: dict, symbol: str,
            history: list[dict] | None = None) -> dict:
    """Pure: the six Tab 2 panels -> composite payload."""
    pos = _payload(panels, "t2_positioning")
    vol = _payload(panels, "t2_vol")
    news = _payload(panels, "t2_news")
    social = _payload(panels, "t2_social")
    squeeze = _payload(panels, "t2_squeeze")
    events = _payload(panels, "t2_events")

    s_pos, r_pos, pos_flags = _positioning_score(pos)
    s_vol, r_vol = _vol_score(vol)
    s_news, r_news, f_news = _news_score(news)
    s_social, r_social, f_social = _social_score(social)
    s_squeeze, r_squeeze = _squeeze_score(squeeze)
    s_analyst, r_analyst = _analyst_score(squeeze)

    votes = [
        _vote("vol", r_vol, s_vol),
        _vote("positioning", r_pos, s_pos),
        _vote("news", r_news, s_news),
        _vote("social", r_social, s_social),
        _vote("squeeze", r_squeeze, s_squeeze),
        _vote("analyst", r_analyst, s_analyst),
    ]

    # Thin-sample damping applies to the weight, not the score, so a small
    # sample pulls its own influence down instead of dragging the blend to 50.
    factors = {"news": f_news, "social": f_social}
    for vote in votes:
        vote["weight_eff"] = round(
            vote["weight"] * factors.get(vote["key"], 1.0), 2) \
            if vote["available"] else 0.0

    total_weight = sum(v["weight_eff"] for v in votes)
    composite = (sum(v["score"] * v["weight_eff"] for v in votes if v["available"])
                 / total_weight) if total_weight else None

    missing = [LABELS[v["key"]] for v in votes if not v["available"]]
    divergences = _divergences(pos, vol, news, social, squeeze, events,
                              {v["key"]: v["score"] for v in votes}, composite)

    payload = {
        "symbol": symbol,
        "composite": round(composite, 1) if composite is not None else None,
        "confidence": _confidence(votes, divergences, news, social, vol, pos_flags),
        "subscores": votes,
        "divergences": divergences,
        "missing": missing,
        "flags": pos_flags,
        "history": [{"date": r.get("date"), "composite": r.get("composite")}
                    for r in (history or []) if r.get("composite") is not None],
        "caveat": CAVEAT,
    }
    payload["band"], payload["label"] = _band(composite)
    payload["commentary"] = _commentary(payload)
    return payload


def _band(composite: float | None) -> tuple[str, str]:
    if composite is None:
        return "unknown", "NO READING"
    for cutoff, band, label in BANDS:
        if composite < cutoff:
            return band, label
    return "mixed", "MIXED"


def _confidence(votes: list[dict], divergences: list[dict], news, social,
                vol, flags: dict) -> float:
    """Agreement between sub-scores, docked for missing inputs and thin samples."""
    available = [v["score"] for v in votes if v["available"]]
    if not available:
        return 0.0

    # Dispersion: sub-scores that disagree wildly mean a low-confidence read,
    # exactly as regime.classify() treats disagreeing signals.
    spread = statistics.pstdev(available) if len(available) > 1 else 0.0
    confidence = 100.0 - _clamp(spread / 25.0, 0.0, 1.0) * 45.0

    confidence -= MISSING_PENALTY * sum(1 for v in votes if not v["available"])
    confidence -= min(DIVERGENCE_PENALTY * len(divergences),
                      DIVERGENCE_PENALTY_CAP)

    if news and (news.get("sentiment") or {}).get("n", 0) < 5:
        confidence -= 20
    if social and (social.get("n") or 0) < 10:
        confidence -= 20
    if vol and vol.get("thin_chain"):
        confidence -= 15
    if flags.get("near_flip"):
        confidence -= 10

    return round(max(CONFIDENCE_FLOOR, min(100.0, confidence)), 0)


# --------------------------------------------------------------------------- #
# Divergences — the actual deliverable
# --------------------------------------------------------------------------- #

def _divergences(pos, vol, news, social, squeeze, events,
                 scores: dict, composite: float | None) -> list[dict]:
    """Cross-panel conflicts, in severity order. Each is a deterministic rule."""
    out: list[dict] = []

    def add(key, severity, label, sentence):
        out.append({"key": key, "severity": severity, "label": label,
                    "sentence": sentence})

    s_social = scores.get("social")
    s_news = scores.get("news")
    perf_month = (squeeze or {}).get("perf_month")
    perf_week = (squeeze or {}).get("perf_week")
    short_float = (squeeze or {}).get("short_float")
    upside = (squeeze or {}).get("target_upside")
    insider = (squeeze or {}).get("insider_trans")
    skew_pct = (vol or {}).get("skew_25d_pct")
    amplifying = (pos or {}).get("regime") == "negative"
    spot = (pos or {}).get("spot")
    zero_gamma = (pos or {}).get("zero_gamma")

    # --- retail vs the tape ------------------------------------------------- #
    if s_social is not None and s_social >= 70 and perf_month is not None \
            and perf_month <= -0.10:
        add("crowd_vs_price", "alert", "Retail bullish into a downtrend",
            f"Retail chatter is strongly bullish while the stock is "
            f"{perf_month:.1%} over the month. Enthusiasm is not being "
            f"confirmed by price — the crowd is buying a falling tape.")

    # --- retail vs the options market --------------------------------------- #
    if s_social is not None and s_social >= 65 and skew_pct is not None \
            and skew_pct >= 0.12:
        add("crowd_vs_options", "alert", "Retail long, options bid for downside",
            f"Retail is positioned long while 25-delta puts trade "
            f"{skew_pct:.0%} of ATM over calls. The people paying for "
            f"protection and the people talking on social media are not the "
            f"same people, and they disagree.")

    # --- insiders vs retail --------------------------------------------------- #
    if insider is not None and insider <= -0.02 and s_social is not None \
            and s_social >= 70:
        add("insider_vs_retail", "warn", "Insiders selling into retail enthusiasm",
            f"Insider holdings fell {insider:.2%} while retail chatter runs "
            f"strongly bullish. The people with the best information are "
            f"reducing into the people with the least.")

    # --- price through the analyst target ------------------------------------- #
    if upside is not None and upside < 0:
        add("price_thru_target", "note", "Price above consensus target",
            f"Spot trades {abs(upside):.1%} above the consensus price target. "
            f"Either the street is stale, or the market is pricing something "
            f"the published research has not caught up with.")

    # --- news vs the tape ----------------------------------------------------- #
    if s_news is not None and s_news >= 60 and perf_week is not None \
            and perf_week <= -0.05:
        add("news_vs_price", "note", "Tape ignoring positive coverage",
            f"Headline tone is positive but the stock is {perf_week:.1%} on "
            f"the week — the tape is not buying the story.")

    # --- squeeze setup --------------------------------------------------------- #
    if short_float is not None and short_float >= 0.15 and amplifying:
        add("squeeze_setup", "alert", "Squeeze fuel with dealers short gamma",
            f"{short_float:.1%} of the float is short while dealers sit in "
            f"negative gamma, so hedging amplifies rather than damps. That is "
            f"the configuration in which short covering becomes violent — in "
            f"either direction.")

    # --- bullish consensus in an amplifying regime ------------------------------ #
    if composite is not None and composite >= 60 and spot and zero_gamma \
            and spot < zero_gamma:
        add("bull_in_amplifier", "warn", "Bullish reading below the gamma flip",
            f"The blended read is bullish, but spot sits below the {zero_gamma:g} "
            f"flip level where dealer hedging amplifies moves. A bullish view "
            f"here needs a tighter stop than the score implies.")

    # --- event premium ---------------------------------------------------------- #
    if events and events.get("move_state") == "rich" and events.get("earnings_soon"):
        add("event_vol_rich", "warn", "Event premium rich into earnings",
            f"The straddle prices {events['move_ratio']:.2f}x this name's own "
            f"average post-earnings move with {events['earnings_days_out']} days "
            f"to go — owning premium into the print is expensive.")

    # --- dilution into enthusiasm ------------------------------------------------ #
    # Gated on size: a 424B at a mega-cap is routine debt issuance, not equity
    # dilution, and firing this on NVIDIA would be plainly wrong.
    cap = (squeeze or {}).get("market_cap")
    if events and (events.get("filings") or {}).get("offering_flag") \
            and s_social is not None and s_social >= 65 \
            and cap is not None and cap < DILUTION_CAP_LIMIT:
        add("dilution_overhang", "warn", "Financing into retail enthusiasm",
            "A live offering has been registered in the last 90 days while "
            "retail chatter runs bullish — supply is arriving into the "
            "enthusiasm.")

    order = {"alert": 0, "warn": 1, "note": 2}
    out.sort(key=lambda d: order.get(d["severity"], 3))
    return out


# --------------------------------------------------------------------------- #
# Commentary
# --------------------------------------------------------------------------- #

def _commentary(p: dict) -> dict:
    sentences: list[str] = []
    warnings: list[str] = []

    if p["composite"] is None:
        return {"headline": f"No sentiment reading available for {p['symbol']}",
                "warnings": ["⚠ No panel returned usable data."],
                "sentences": []}

    available = [v for v in p["subscores"] if v["available"]]
    top = max(available, key=lambda v: abs(v["score"] - 50))
    sentences.append(
        f"{p['symbol']} reads {p['composite']:.0f}/100 ({p['label'].lower()}) "
        f"across {len(available)} of {len(p['subscores'])} inputs, at "
        f"{p['confidence']:.0f}% confidence. The strongest single pull is "
        f"{top['label'].lower()} at {top['score']:.0f} ({top['reading']}).")

    bulls = [v["label"].lower() for v in available if v["score"] >= 60]
    bears = [v["label"].lower() for v in available if v["score"] <= 40]
    if bulls and bears:
        sentences.append(
            f"The inputs are split: {', '.join(bulls)} lean bullish while "
            f"{', '.join(bears)} lean bearish. A blended score across a split "
            f"like that is a summary of disagreement, not a signal.")
    elif bulls and not bears:
        sentences.append(f"Inputs agree on the bullish side: {', '.join(bulls)}.")
    elif bears and not bulls:
        sentences.append(f"Inputs agree on the bearish side: {', '.join(bears)}.")

    for div in p["divergences"][:3]:
        sentences.append(div["sentence"])

    for div in p["divergences"]:
        if div["severity"] == "alert":
            warnings.append(f"⚠ {div['label']}.")

    if p["missing"]:
        warnings.append("⚠ Scored without " + ", ".join(m.lower() for m in p["missing"])
                        + " — weights renormalised over the rest.")
    if p["confidence"] < 45:
        warnings.append(f"⚠ Low confidence ({p['confidence']:.0f}%); the inputs "
                        f"are not lining up.")
    if p["flags"].get("amplifying"):
        warnings.append("⚠ Dealers are short gamma — hedging amplifies moves "
                        "in both directions.")

    n_div = len(p["divergences"])
    headline = (f"{p['label']} {p['composite']:.0f}/100 — {p['confidence']:.0f}% "
                f"confidence, {n_div} divergence{'s' if n_div != 1 else ''}")
    return {"headline": headline, "warnings": warnings, "sentences": sentences}
