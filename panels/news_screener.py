"""Tab 3 — cross-asset news screener.

Eight topic panels built from one parallel fetch. For each panel: the most
recent headlines from trusted financial publishers, a tone reading, and a
*directional* reading that knows what the panel is about.

**Two sentiment numbers, deliberately.** ``tone`` is ``_sentiment_util``'s VADER
score with the macro lexicon — how positive the language is. ``asset`` is
``_asset_read``'s directional score — what the headline implies for the asset the
panel covers. They frequently disagree, and that disagreement is the most
informative thing on the panel: "Gold rises on weaker dollar as rate-cut bets
firm" is negative language describing a bullish setup. Neither number is the
answer on its own.

**Routing.** Google News items arrive already scoped to a panel by their query.
Publisher-feed items (CNBC, the Fed, the EIA…) arrive unlabelled and are routed
by :func:`classify`, which may legitimately put one story in several panels — an
FOMC decision is US Macro *and* US Fixed Income. Anything that matches nothing
is dropped rather than parked somewhere arbitrary.

Everything after the fetch is pure and offline: no LLM, no second network call,
same input gives the same payload.
"""

from __future__ import annotations

import re
import time

from . import _asset_read as ar
from . import _feeds
from . import _sentiment_util as senti
from . import market_context

# "Most recent news", read literally. 48 hours keeps a Monday morning readable
# after a quiet weekend without letting last Tuesday's story vote.
WINDOW_HOURS = 48
MAX_ITEMS = 25

# Tone and direction are on the same -1..+1 scale, so a gap this wide means they
# are telling genuinely different stories rather than differing in degree.
DIVERGENCE_CUT = 0.30

CAVEAT = ("Two readings, neither definitive. TONE is the language of the "
          "headlines; ASSET READ is a rule layer that maps each headline to a "
          "direction for this panel's asset. Both are pattern matching, not "
          "comprehension — check coverage before trusting the direction.")

# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

# A story is European if it carries any of these markers. Checked first, and it
# *suppresses* the two US panels: "inflation eases" is a euro-zone story when
# the headline says ECB, and letting it also vote in US Macro would import
# European policy into the US reading.
_EU_CONTEXT = re.compile(
    r"\b(?:ecb|european central bank|euro ?zone|euro area|euro|europe|"
    r"european|bank of england|\bboe\b|britain|british|\buk\b|london|"
    r"german|germany|france|french|italy|italian|spain|spanish|"
    r"bundesbank|lagarde|bailey|brussels|\bemu\b|"
    # The index and bond names are European markers in their own right: "DAX
    # climbs as inflation cools" carries no other European word, and without
    # these it reads as a US inflation story.
    r"stoxx|\bdax\b|ftse|cac ?40|\bibex\b|bunds?|gilts?|\bbtps?\b)\b", re.I)

_US_CONTEXT = re.compile(
    r"\b(?:fed|federal reserve|fomc|powell|\bus\b|u\.s\.|united states|"
    r"america|american|wall street|washington|white house|congress|"
    r"treasury|nasdaq|s&p|dow)\b", re.I)

CLASSIFIERS: dict[str, re.Pattern] = {
    "t3_us_macro": re.compile(
        r"\b(?:federal reserve|\bfed\b|fomc|powell|\bcpi\b|\bppi\b|\bpce\b|"
        r"inflation|payrolls?|jobless|unemployment|\bgdp\b|tariffs?|"
        r"trade war|shutdown|debt ceiling|consumer (?:confidence|sentiment)|"
        r"retail sales|\bism\b|rate (?:cut|hike|decision)|recession|"
        r"soft landing|stagflation|economy)\b", re.I),
    "t3_us_equities": re.compile(
        r"\b(?:s&p ?500|nasdaq|dow(?: jones)?|russell 2000|wall street|"
        r"stock market|stocks|equities|earnings (?:season|beat|miss)|"
        r"bull market|bear market|\bipo\b|buybacks?)\b", re.I),
    # Bare "bond" and "yield" are in here deliberately. In a financial headline
    # they are essentially always about fixed income, and without them the
    # panel routed *zero* publisher stories on the verification date — every
    # rates headline that did not spell out "Treasury yield" fell through.
    "t3_us_rates": re.compile(
        r"\b(?:treasury (?:yield|note|bond|bill|auction)|treasuries|"
        r"bonds?|yields?|bond market|yield curve|credit spreads?|"
        r"corporate bonds?|junk bonds?|high[- ]yield|investment grade|"
        r"fixed income|10-year|30-year|2-year|term premium|duration|"
        r"coupon|munis?|fed funds|debt (?:market|sale)|\bqt\b|"
        r"quantitative)\b", re.I),
    "t3_eu_macro": re.compile(
        r"\b(?:ecb|european central bank|euro ?zone|euro area|"
        r"bank of england|\bboe\b|lagarde|bailey|bundesbank|"
        r"(?:uk|german|french|italian|spanish|european) (?:inflation|economy|"
        r"gdp|unemployment)|\bhicp\b|\bifo\b|\bzew\b)\b", re.I),
    "t3_eu_markets": re.compile(
        r"\b(?:stoxx|\bdax\b|ftse(?: 100| mib)?|cac ?40|\bibex\b|\baex\b|"
        r"european (?:stocks|shares|equities|markets|bonds)|"
        r"bunds?|gilts?|\bbtps?\b|london stocks)\b", re.I),
    "t3_energy": re.compile(
        r"\b(?:oil|crude|opec\+?|\bwti\b|brent|petroleum|natural gas|"
        r"nat ?gas|\blng\b|gasoline|petrol|diesel|jet fuel|refiner(?:y|ies)|"
        r"shale|drilling|pipeline|energy prices?|\beia\b|barrels?)\b", re.I),
    "t3_precious": re.compile(
        r"\b(?:gold|silver|platinum|palladium|bullion|precious metals?)\b",
        re.I),
    "t3_metals": re.compile(
        r"\b(?:copper|alumini?um|nickel|zinc|\btin\b|cobalt|lithium|"
        r"iron ore|steel|base metals?|industrial metals?|\blme\b|"
        r"mining|smelters?)\b", re.I),
}

_US_PANELS = ("t3_us_macro", "t3_us_equities", "t3_us_rates")

# Panel-name vocabulary that would otherwise take every top theme slot. On a
# single-subject corpus the subject says nothing: every headline in Precious
# Metals contains "gold".
_THEME_STOP: dict[str, set[str]] = {
    "t3_us_macro": {"fed", "federal", "reserve", "us", "economy", "economic"},
    "t3_us_equities": {"stocks", "stock", "wall", "street", "sp", "nasdaq",
                       "dow", "equities"},
    "t3_us_rates": {"treasury", "treasuries", "yield", "yields", "bond",
                    "bonds", "rate", "rates"},
    "t3_eu_macro": {"ecb", "euro", "eurozone", "zone", "europe", "european",
                    "uk", "economy"},
    "t3_eu_markets": {"europe", "european", "stocks", "shares", "index"},
    "t3_energy": {"oil", "crude", "energy", "gas", "prices", "price"},
    "t3_precious": {"gold", "silver", "metals", "metal", "prices", "price"},
    "t3_metals": {"copper", "metals", "metal", "mining", "prices", "price"},
}


def classify(title: str, fallback: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Panels a publisher-feed headline belongs to.

    Multi-membership is intended: an FOMC decision is genuinely US Macro and US
    Fixed Income at once, and forcing a single choice would hide it from one of
    the two panels a reader would look for it in.

    ``fallback`` applies only when nothing matched — it is how a narrow feed
    (the EIA, Mining.com) keeps items whose headlines avoid the obvious
    vocabulary. An item that matches nothing and has no fallback is dropped;
    the alternative is filing "SanDisk CEO reveals what's next" under US Macro.
    """
    text = title or ""
    hits = [panel for panel, pattern in CLASSIFIERS.items()
            if pattern.search(text)]

    # The context check only ever *removes* US panels, and only when the story
    # is plainly European and has no US angle: "euro zone inflation eases"
    # matches the US Macro classifier on the word "inflation" and must not vote
    # in the US reading.
    #
    # There is deliberately no mirror-image rule removing the Europe panels. The
    # Europe classifiers are already Europe-specific — nothing matches
    # `t3_eu_markets` except a European index or bond — so a second gate could
    # only ever produce false negatives, which is exactly what it did: "DAX
    # climbs to record high" carries no other European marker and was being
    # dropped outright.
    if _EU_CONTEXT.search(text) and not _US_CONTEXT.search(text):
        hits = [p for p in hits if p not in _US_PANELS]

    return tuple(hits) if hits else tuple(fallback)


# --------------------------------------------------------------------------- #
# Refresh
# --------------------------------------------------------------------------- #

def refresh(now: float | None = None) -> dict[str, dict]:
    """Fetch every feed once and build all eight panel payloads.

    Returns ``{panel_key: payload}``. Quote strips are fetched alongside and are
    optional by design — a yfinance outage costs the strips, never the panels.
    """
    items, errors = _feeds.fetch_all()
    try:
        quotes = market_context.build()
    except Exception as exc:  # pragma: no cover - build() already swallows
        quotes = {}
        errors.append(f"market context: {type(exc).__name__}")
    return compute(items, quotes=quotes, errors=errors, now=now)


def compute(items: list[dict], quotes: dict[str, list[dict]] | None = None,
            errors: list[str] | None = None,
            now: float | None = None) -> dict[str, dict]:
    """Pure: raw fetched items -> eight panel payloads."""
    now = time.time() if now is None else now
    quotes = quotes or {}
    cutoff = now - WINDOW_HOURS * 3600

    routed: dict[str, list[dict]] = {k: [] for k in _feeds.PANEL_KEYS}
    undated = 0

    for item in items:
        published = item.get("published")
        if published is None:
            # No timestamp means it cannot be placed in a 48-hour window.
            # Admitting it anyway would quietly break the one promise this tab
            # makes about recency, so it is dropped and counted instead.
            undated += 1
            continue
        if published < cutoff or published > now + 3600:
            continue  # too old, or a feed with a clock running fast

        panels = item.get("panels")
        if panels is None:
            panels = classify(item.get("title", ""),
                              item.get("fallback_panels") or ())
        for panel in panels:
            if panel in routed:
                routed[panel].append(item)

    out: dict[str, dict] = {}
    for panel in _feeds.PANEL_KEYS:
        out[panel] = _panel_payload(
            panel, routed[panel], quotes.get(panel) or [],
            errors=errors or [], undated=undated, now=now)
    return out


def _panel_payload(panel: str, items: list[dict], quotes: list[dict],
                   errors: list[str], undated: int, now: float) -> dict:
    """One panel: dedupe, window, score both ways, describe."""
    merged = _feeds.dedupe(items)
    merged.sort(key=lambda i: i.get("published") or 0, reverse=True)
    merged = merged[:MAX_ITEMS]

    titles = [i["title"] for i in merged]
    tone = senti.sentiment(titles, senti.MACRO)
    asset = ar.panel_read(titles, panel)

    rows = []
    for item in merged:
        title = item["title"]
        rows.append({
            "title": title,
            "link": item.get("link", ""),
            "source": item.get("source") or item.get("publisher") or "Unknown",
            "published": item.get("published"),
            "compound": round(senti.score(title, senti.MACRO), 3),
            "asset": ar.asset_read(title, panel),
            "drivers": ar.drivers_fired(title, panel),
        })

    payload = {
        "panel": panel,
        "title": _feeds.PANEL_TITLES[panel],
        "count": len(merged),
        "empty": not merged,
        "window_hours": WINDOW_HOURS,
        "newest": merged[0].get("published") if merged else None,
        "sentiment": tone,
        "tone": tone["tone"],
        "mean": tone["mean"],
        "asset": asset,
        "divergence": _divergence(tone, asset, panel),
        "drivers": ar.top_drivers(titles, panel),
        "themes": senti.themes(titles, extra_stop=_THEME_STOP.get(panel)),
        "salient": _salient(rows),
        "source_breakdown": senti.by_source(merged, senti.MACRO)[:8],
        "feeds": _feed_counts(merged),
        "quotes": quotes,
        "items": rows,
        "errors": errors,
        "undated_dropped": undated,
        "caveat": CAVEAT,
    }
    payload["commentary"] = _commentary(payload)
    return payload


def _salient(rows: list[dict], top_n: int = 6) -> list[dict]:
    """The headlines carrying the panel's directional evidence.

    Deliberately *not* ``senti.salient``, which ranks by tone and theme overlap.
    On this tab the question is "what is actually saying something about the
    asset", so directional strength leads and tone only breaks ties among
    headlines that fired no rule at all.
    """
    def rank(row: dict) -> tuple[float, float]:
        asset = row.get("asset")
        return (abs(asset) if asset is not None else 0.0,
                abs(row.get("compound") or 0.0))

    ordered = sorted(rows, key=rank, reverse=True)
    return [{"title": r["title"], "link": r["link"], "source": r["source"],
             "published": r["published"], "score": r["compound"],
             "asset": r["asset"], "drivers": r["drivers"]}
            for r in ordered[:top_n]]


def _divergence(tone: dict, asset: dict, panel: str) -> dict | None:
    """Flag when the language and the direction point *opposite ways*.

    A wide gap is not enough. Mildly bearish language (-0.09) alongside a firmly
    bearish direction (-0.54) is a 0.45 gap and complete agreement — flagging it
    would call a divergence on almost every panel and make the badge worthless.
    Both readings must clear their own call threshold, in opposite directions.
    """
    score = asset.get("score")
    if score is None or tone["n"] == 0:
        return None

    mean = tone["mean"]
    tone_dir = 1 if mean >= senti.POS_CUT else -1 if mean <= senti.NEG_CUT else 0
    asset_dir = (1 if score >= ar.CALL_CUT
                 else -1 if score <= -ar.CALL_CUT else 0)
    if not tone_dir or not asset_dir or tone_dir == asset_dir:
        return None

    gap = abs(mean - score)
    if gap < DIVERGENCE_CUT:
        return None

    noun = asset.get("noun", "the asset")
    if asset_dir > 0:
        sentence = (f"Coverage reads {tone['tone'].lower()} while the direction "
                    f"is constructive for {noun} — negative language describing "
                    f"a supportive setup.")
    else:
        sentence = (f"Coverage reads {tone['tone'].lower()} while the direction "
                    f"is unhelpful for {noun} — positive language describing an "
                    f"unsupportive setup.")
    return {
        "gap": round(gap, 3),
        "severity": "high" if gap >= 0.6 else "medium",
        "label": "Tone and direction disagree",
        "sentence": sentence,
    }


def _feed_counts(items: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for item in items:
        feed = item.get("feed", "?")
        counts[feed] = counts.get(feed, 0) + 1
    return [{"feed": k, "n": v}
            for k, v in sorted(counts.items(), key=lambda p: -p[1])]


# --------------------------------------------------------------------------- #
# Commentary
# --------------------------------------------------------------------------- #

def _commentary(p: dict) -> dict:
    sentences: list[str] = []
    warnings: list[str] = []
    tone, asset = p["sentiment"], p["asset"]
    title = p["title"]

    if p["empty"]:
        return {"headline": f"No recent {title} headlines",
                "warnings": [f"⚠ Nothing published in the last "
                             f"{p['window_hours']}h reached this panel."],
                "sentences": []}

    feeds = ", ".join(f"{f['feed']} {f['n']}" for f in p["feeds"][:4])
    sentences.append(
        f"{tone['n']} headlines in the last {p['window_hours']}h ({feeds}). "
        f"Language is {tone['tone'].lower()} — {tone['pos_pct']}% positive, "
        f"{tone['neg_pct']}% negative, mean {tone['mean']:+.3f}.")

    if asset["score"] is None:
        warnings.append("⚠ No headline carried a directional signal — tone "
                        "only, no read on the asset.")
    else:
        sentences.append(
            f"Directionally the flow reads {asset['label'].lower()} at "
            f"{asset['score']:+.2f}, from {asset['n_fired']} of "
            f"{asset['n']} headlines ({asset['bull']} constructive, "
            f"{asset['bear']} unhelpful).")

    if p["divergence"]:
        sentences.append(p["divergence"]["sentence"])

    if p["drivers"]:
        top = ", ".join(f"{d['driver']} ({d['count']})" for d in p["drivers"][:4])
        sentences.append(f"Recurring drivers: {top}.")

    if tone["n"] < 8:
        warnings.append(f"⚠ Only {tone['n']} headlines — thin for a tone read.")
    if asset["score"] is not None and asset["thin"]:
        warnings.append(
            f"⚠ Directional coverage is thin: only {asset['n_fired']} of "
            f"{asset['n']} headlines fired a rule "
            f"({asset['coverage'] * 100:.0f}%). Treat the direction as "
            f"anecdotal.")
    if asset.get("two_sided"):
        warnings.append(
            f"⚠ {asset['two_sided']} headline(s) are two-sided (a strong growth "
            f"print is good for growth and bad for cuts) — scored at half "
            f"weight rather than picking a side.")
    if not p["quotes"]:
        warnings.append("⚠ Market levels unavailable — headlines only.")
    if p["errors"]:
        warnings.append(f"⚠ {len(p['errors'])} feed(s) failed: "
                        + "; ".join(p["errors"][:3])
                        + ("; …" if len(p["errors"]) > 3 else ""))

    if asset["score"] is None:
        headline = f"{tone['tone']} tone — {tone['n']} headlines, no directional read"
    else:
        headline = (f"{asset['label']} — {tone['tone'].lower()} tone, "
                    f"{tone['n']} headlines")

    return {"headline": headline, "warnings": warnings, "sentences": sentences}
