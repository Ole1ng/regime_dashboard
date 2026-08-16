"""Directional headline scoring for the Tab 3 news screener.

Deterministic, offline, regex and lookup tables. No LLM, no network, no state.

**Why this exists.** ``_sentiment_util`` measures the *tone* of a headline, which
on a macro corpus is regularly the opposite of what the headline means for the
asset in question:

===========================================  ==============  ==================
Headline                                     Tone            Truth
===========================================  ==============  ==================
10-year Treasury yield inches higher         positive        bearish bonds
Gold rises on weaker dollar, rate-cut bets   negative        bullish gold
Inflation cools more than expected           negative        supportive
OPEC agrees deeper output cuts               negative        bullish crude
===========================================  ==============  ==================

Tone is not wrong — "cools", "weaker" and "cuts" really are negative words. It is
answering a different question. So each panel gets a second reading that knows
*what the panel is about*: a rise in yields is bearish in US Fixed Income and a
rise in crude is bullish in Oil & Energy, from the same verb.

**How it works.** Three ingredients, combined per headline:

1. **Movers** — direction verbs, in three intensity tiers, shared by all panels.
   "surges" and "edges up" are both up, and should not count the same.
2. **Subjects** — per panel, the nouns a mover can act on, each carrying the sign
   that a *rise in that noun* has for the asset the panel covers. Yields carry
   ``-1``; bonds carry ``+1``. Matched spans are masked as they are consumed, so
   "Treasury yields" resolves to the yield subject once and cannot also match the
   bond subject and cancel itself out.
3. **Drivers** — per panel, events that carry a direction with no verb at all:
   an OPEC production cut, a hawkish Fed, a mine outage, a ceasefire.

A headline that trips none of the three returns ``None``, **not** ``0.0``. Most
headlines are undirected and counting them as neutral would drag every panel to
the middle and make a strong reading impossible.

**What it cannot do.** It is a bag of patterns, not a parser. It does not handle
negation ("yields fail to rise"), it cannot tell a forecast from a fact, and a
headline about two assets moving opposite ways is resolved by proximity rather
than grammar. ``coverage`` is reported alongside every score so a reading drawn
from 3 of 25 headlines is visibly thin rather than quietly confident.
"""

from __future__ import annotations

import re

from ._feeds import PANEL_KEYS

# --------------------------------------------------------------------------- #
# Movers — shared direction verbs
# --------------------------------------------------------------------------- #

# Intensity tiers. A headline saying something "plunged" is making a stronger
# claim than one saying it "edged lower", and averaging them as equals throws
# away the only magnitude information a headline carries.
STRONG, NORMAL, MILD = 1.0, 0.7, 0.4

_MOVERS: tuple[tuple[str, int, float], ...] = (
    # --- up ---------------------------------------------------------------- #
    (r"\b(?:soars?|soared|surges?|surged|spikes?|spiked|rockets?|rocketed|"
     r"jumps?|jumped|leaps?|leapt)\b", +1, STRONG),
    (r"\b(?:rall(?:y|ies|ied)|extends? gains|record (?:high|highs)|"
     r"all-time high|multi-year high|highest since)\b", +1, STRONG),
    (r"\b(?:rises?|rose|rising|climbs?|climbed|climbing|gains?|gained|"
     r"advances?|advanced|strengthens?|strengthened|rebounds?|rebounded|"
     r"tops?|higher|firmer|up \d)\b", +1, NORMAL),
    # Transitive forms: "Weaker dollar boosts gold", "supply cuts buoy crude".
    (r"\b(?:boosts?|boosted|lifts?|lifted|buoys?|buoyed|props? up|"
     r"underpins?|supports?|bolsters?|fuels?|fuelled|fueled)\b", +1, NORMAL),
    # Widening is an increase in the subject, and the subject's own sign decides
    # what that means: spreads carry -1, so "spreads widen" resolves bearish.
    (r"\b(?:widens?|widening|widened|blows? out)\b", +1, NORMAL),
    (r"\b(?:edges? (?:up|higher)|inches? (?:up|higher)|ticks? (?:up|higher)|"
     r"nudges? (?:up|higher)|creeps? (?:up|higher)|steadies|firms?)\b",
     +1, MILD),
    # --- down -------------------------------------------------------------- #
    (r"\b(?:plunges?|plunged|crashes?|crashed|collapses?|collapsed|"
     r"tumbles?|tumbled|slumps?|slumped|sinks?|sank|routs?|"
     r"sell-?off|selloff)\b", -1, STRONG),
    (r"\b(?:record (?:low|lows)|all-time low|lowest since|"
     r"extends? losses|worst (?:day|week|month))\b", -1, STRONG),
    (r"\b(?:falls?|fell|falling|drops?|dropped|declines?|declined|"
     r"slides?|slid|retreats?|retreated|weakens?|weakened|sags?|"
     r"lower|softer|down \d)\b", -1, NORMAL),
    # Transitive forms: "Stronger dollar weighs on gold", "glut drags on crude".
    (r"\b(?:weighs?|weighed|drags?|dragged|hurts?|dents?|dented|"
     r"pressures?|pressured|under pressure|saps?|erodes?|"
     r"knocks?|batters?|battered)\b", -1, NORMAL),
    (r"\b(?:narrows?|narrowing|narrowed|tightens?|tightening|tightened)\b",
     -1, NORMAL),
    (r"\b(?:edges? (?:down|lower)|inches? (?:down|lower)|ticks? (?:down|lower)|"
     r"slips?|slipped|eases?|eased|dips?|dipped|pares? gains|"
     r"trims? gains)\b", -1, MILD),
)

MOVERS = tuple((re.compile(p), d, w) for p, d, w in _MOVERS)

# How far apart a subject and its verb may sit and still be read as one clause.
# Headlines are short; 45 characters is roughly "X rises as Y falls" plus a
# qualifier, and is what keeps "Gold rises as stocks tumble" from reading gold
# as falling.
PAIR_WINDOW = 45

# --------------------------------------------------------------------------- #
# Subjects — per panel, and the sign a RISE in each has for that panel's asset
# --------------------------------------------------------------------------- #
#
# Order is significant: patterns are matched in sequence and each match is
# masked out of the text, so the most specific must come first. In US Fixed
# Income "Treasury yields" must be consumed by the yield pattern (-1) before the
# bond pattern (+1) can see the word "Treasury" and cancel it.

SUBJECTS: dict[str, tuple[tuple[str, int], ...]] = {
    "t3_us_macro": (
        # The "asset" here is the growth and policy outlook, so the subjects are
        # the readings themselves. A rise in inflation or unemployment is
        # restrictive; a rise in growth or confidence is supportive.
        (r"\b(?:inflation|cpi|ppi|pce|price pressures?)\b", -1),
        (r"\b(?:unemployment|jobless claims?|layoffs?)\b", -1),
        (r"\b(?:gdp|growth|payrolls?|hiring|"
         r"(?:consumer|business) confidence|retail sales|pmi|ism)\b", +1),
    ),
    "t3_us_equities": (
        (r"\b(?:s&p ?500|s and p 500|nasdaq|dow(?: jones)?|russell 2000|"
         r"wall street|us stocks?|us equities|us shares|"
         r"stock market|equity market)\b", +1),
        (r"\bvolatility|\bvix\b", -1),
    ),
    "t3_us_rates": (
        # Specific-first: these three consume the word "yield" wherever it
        # appears next to an instrument, before the bond pattern runs.
        (r"\b(?:treasury|government|corporate|junk|high[- ]yield|real|"
         r"benchmark|10-year|2-year|30-year)?[ ]?yields?\b", -1),
        (r"\b(?:credit |yield |option-adjusted )?spreads?\b", -1),
        (r"\b(?:term premium|borrowing costs?)\b", -1),
        (r"\b(?:treasuries|treasury (?:notes?|bonds?|bills?)|bonds?|"
         r"bond market|fixed income|tlt|ief)\b", +1),
    ),
    "t3_eu_macro": (
        (r"\b(?:inflation|cpi|hicp|price pressures?)\b", -1),
        (r"\b(?:unemployment|layoffs?|redundancies)\b", -1),
        (r"\b(?:gdp|growth|pmi|ifo|zew|industrial production|retail sales|"
         r"(?:consumer|business) confidence)\b", +1),
    ),
    "t3_eu_markets": (
        (r"\b(?:bund|gilt|btp|oat|bono|peripheral|euro(?:pean)? bond)[ ]?"
         r"yields?\b", -1),
        (r"\b(?:spreads?)\b", -1),
        (r"\b(?:bunds|gilts|btps|euro(?:pean)? bonds)\b", +1),
        (r"\b(?:stoxx ?(?:600|50)?|dax|ftse(?: 100| mib)?|cac ?40?|ibex|aex|smi|"
         r"euro(?:pean)? (?:stocks|shares|equities)|european markets)\b", +1),
    ),
    "t3_energy": (
        (r"\b(?:oil|crude|wti|brent|petroleum|"
         r"nat(?:ural)? ?gas|lng|gasoline|petrol|diesel|jet fuel|"
         r"energy prices?)\b", +1),
    ),
    "t3_precious": (
        (r"\b(?:gold|silver|platinum|palladium|bullion|"
         r"precious metals?)\b", +1),
    ),
    "t3_metals": (
        (r"\b(?:copper|alumini?um|nickel|zinc|tin|lead|cobalt|lithium|"
         r"iron ore|steel|base metals?|industrial metals?)\b", +1),
    ),
}

# --------------------------------------------------------------------------- #
# Drivers — events that carry a direction with no verb attached
# --------------------------------------------------------------------------- #
#
# (name, pattern, sign, weight). Weight is on the same 0..1 scale as the mover
# intensities, so a driver and a verb pairing are directly comparable.

_D = lambda name, pat, sign, w: (name, re.compile(pat), sign, w)  # noqa: E731

DRIVERS: dict[str, tuple] = {
    "t3_us_macro": (
        _D("cooling inflation",
           r"inflation (?:cools?|cooled|eases?|eased|slows?|slowed|retreats?)|"
           r"cooler[- ]than[- ](?:expected|forecast)|disinflation", +1, 0.9),
        _D("dovish policy",
           r"\bdovish\b|rate cuts?\b|cuts? (?:interest )?rates?|"
           r"soft landing|easing cycle|\bpivot\b", +1, 0.9),
        _D("fiscal support", r"\bstimulus\b|spending package|tax cuts?", +1, 0.6),
        _D("sticky inflation",
           r"sticky inflation|inflation (?:accelerat|pick|re-?accelerat)\w*|"
           r"hotter[- ]than[- ](?:expected|forecast)|price pressures? build",
           -1, 0.9),
        _D("hawkish policy",
           r"\bhawkish\b|rate (?:hikes?|rise)|raises? (?:interest )?rates?|"
           r"higher for longer|tightening cycle", -1, 0.9),
        _D("trade friction", r"\btariffs?\b|trade war|export controls?", -1, 0.7),
        _D("recession risk",
           r"\brecession\w*|\bstagflation\b|\bcontraction\b|hard landing|"
           r"\blayoffs?\b|job cuts?", -1, 0.9),
        _D("fiscal stress",
           r"government shutdown|debt ceiling|credit rating (?:cut|downgrade)|"
           r"downgrades? (?:the )?us", -1, 0.8),
    ),
    "t3_us_equities": (
        _D("earnings strength",
           r"earnings (?:beat|strength|surprise)|beats? (?:expectations|"
           r"estimates|forecasts)|raises? (?:guidance|outlook)|"
           r"buybacks?|record profits?", +1, 0.8),
        _D("broadening rally",
           r"risk[- ]on|melt[- ]?up|breaks? (?:out|above)|"
           r"bull market|santa rally", +1, 0.7),
        _D("earnings weakness",
           r"earnings (?:miss|misses|disappoint\w*)|misses? (?:expectations|"
           r"estimates)|cuts? (?:guidance|outlook)|profit warning", -1, 0.8),
        _D("risk aversion",
           r"\bcorrection\b|bear market|risk[- ]off|\bcapitulation\b|"
           r"margin calls?|volatility (?:spike|surge)|profit[- ]taking",
           -1, 0.8),
        _D("valuation stress",
           r"\bbubble\b|stretched valuations?|overvalued", -1, 0.5),
    ),
    "t3_us_rates": (
        _D("haven demand",
           r"flight to (?:quality|safety)|haven (?:bid|demand|buying)|"
           r"safe[- ]haven", +1, 0.8),
        _D("dovish policy",
           r"\bdovish\b|rate cuts?\b|cuts? (?:interest )?rates?|"
           r"easing cycle|\bqe\b|bond[- ]buying", +1, 0.9),
        _D("strong auction",
           r"strong (?:auction|demand)|auction (?:stops? through|well received)|"
           r"solid demand", +1, 0.7),
        _D("hawkish policy",
           r"\bhawkish\b|rate (?:hikes?|rise)|raises? (?:interest )?rates?|"
           r"higher for longer|quantitative tightening|\bqt\b", -1, 0.9),
        _D("weak auction",
           r"weak (?:auction|demand)|auction (?:tails?|tailed)|"
           r"poor demand|heavy (?:issuance|supply)|record (?:issuance|supply)|"
           r"(?:debt|supply|issuance) deluge|flood of (?:issuance|supply)|"
           r"deficits?|borrowing (?:binge|surge)", -1, 0.8),
        _D("credit stress",
           r"spreads? widen\w*|credit stress|default rates? (?:rise|climb)|"
           r"downgrades?|distressed", -1, 0.8),
        _D("inflation risk",
           r"sticky inflation|hotter[- ]than[- ](?:expected|forecast)|"
           r"inflation (?:accelerat|pick|re-?accelerat)\w*", -1, 0.7),
    ),
    "t3_eu_macro": (
        _D("dovish ECB",
           r"\bdovish\b|ecb (?:to )?cuts?|rate cuts?\b|easing cycle|"
           r"boe (?:to )?cuts?", +1, 0.9),
        _D("cooling inflation",
           r"inflation (?:cools?|cooled|eases?|eased|slows?|slowed)|"
           r"cooler[- ]than[- ](?:expected|forecast)|disinflation", +1, 0.9),
        _D("recovery", r"\brecovery\b|\brebound\w*|stimulus|"
                       r"fiscal (?:package|expansion)|defence spending", +1, 0.7),
        _D("hawkish ECB",
           r"\bhawkish\b|rate (?:hikes?|rise)|higher for longer|"
           r"tightening cycle", -1, 0.9),
        _D("sticky inflation",
           r"sticky inflation|hotter[- ]than[- ](?:expected|forecast)|"
           r"inflation (?:accelerat|pick)\w*", -1, 0.9),
        _D("recession risk",
           r"\brecession\w*|\bstagflation\b|\bcontraction\b|"
           r"energy crisis|\blayoffs?\b|plant closures?", -1, 0.9),
        _D("political risk",
           r"political (?:crisis|turmoil|uncertainty)|no[- ]confidence|"
           r"snap election|budget (?:crisis|standoff)|coalition collapse|"
           r"\btariffs?\b", -1, 0.7),
    ),
    "t3_eu_markets": (
        _D("dovish policy", r"\bdovish\b|ecb (?:to )?cuts?|rate cuts?\b|"
                            r"boe (?:to )?cuts?", +1, 0.8),
        _D("earnings strength",
           r"earnings (?:beat|strength)|beats? (?:expectations|estimates)|"
           r"raises? (?:guidance|outlook)", +1, 0.7),
        _D("hawkish policy",
           r"\bhawkish\b|rate (?:hikes?|rise)|higher for longer", -1, 0.8),
        _D("periphery stress",
           r"spreads? widen\w*|peripheral (?:stress|spreads)|"
           r"\bdowngrades?\b|debt (?:crisis|concerns)", -1, 0.8),
        _D("risk aversion",
           r"risk[- ]off|\bcorrection\b|bear market|sell-?off deepens|"
           r"political (?:crisis|turmoil)", -1, 0.8),
    ),
    "t3_energy": (
        _D("OPEC restraint",
           r"opec\+?[^.]{0,30}(?:cuts?|trims?|curbs?|extends? cuts|"
           r"deeper cuts|restraint)|production cuts?|output cuts?", +1, 0.9),
        _D("supply disruption",
           r"supply (?:disruption|outage|shock)|production (?:halt|halted|"
           r"outage|suspended)|refinery (?:fire|outage|shutdown)|"
           r"pipeline (?:attack|damage|halt)|\bblockade\b|"
           r"attacks? on|strait of hormuz|force majeure", +1, 0.9),
        _D("sanctions", r"\bsanctions?\b|\bembargo\b|export ban", +1, 0.7),
        _D("inventory draw",
           r"(?:inventor\w+|stockpiles?|stocks) (?:draw\w*|fall|fell|drop\w*|"
           r"decline\w*|tighten\w*)|draw(?:down)? in (?:inventor|stock)\w*",
           +1, 0.8),
        _D("demand strength",
           r"demand (?:surge|strength|beats)|record demand|cold snap|"
           r"heatwave", +1, 0.6),
        _D("oversupply",
           r"\bglut\b|oversupply|\bsurplus\b|spare capacity|"
           r"opec\+?[^.]{0,30}(?:raises?|boosts?|hikes?|increases?|unwinds?)|"
           r"record (?:output|production|supply)", -1, 0.9),
        _D("inventory build",
           r"(?:inventor\w+|stockpiles?|stocks) (?:build\w*|rise|rose|jump\w*|"
           r"climb\w*|swell\w*)|build in (?:inventor|stock)\w*", -1, 0.8),
        _D("demand weakness",
           r"demand (?:weak\w*|slump\w*|destruction|slowdown|concerns?)|"
           r"\bceasefire\b|\btruce\b|peace (?:deal|talks)", -1, 0.8),
    ),
    "t3_precious": (
        _D("weaker dollar",
           r"weaker dollar|dollar (?:falls?|fell|slips?|weakens?|slides?|"
           r"retreats?)|dollar weakness", +1, 0.8),
        _D("dovish policy",
           r"\bdovish\b|rate cuts?\b|cuts? (?:interest )?rates?|"
           r"easing cycle|real yields? (?:fall|fell|drop)", +1, 0.9),
        _D("haven demand",
           r"safe[- ]haven|haven (?:bid|demand|buying)|flight to (?:quality|"
           r"safety)|geopolitical (?:tension|risk)|central bank(?:s)? "
           r"(?:buy|buying|purchases)|etf inflows|inflation hedge", +1, 0.8),
        _D("stronger dollar",
           r"stronger dollar|dollar (?:rises?|rose|firms?|strengthens?|"
           r"climbs?|rallies)|dollar strength", -1, 0.8),
        _D("hawkish policy",
           r"\bhawkish\b|rate (?:hikes?|rise)|higher for longer|"
           r"real yields? (?:rise|rose|climb)", -1, 0.9),
        _D("risk appetite",
           r"risk[- ]on|etf outflows|profit[- ]taking|"
           r"\bceasefire\b|\btruce\b|peace (?:deal|talks)", -1, 0.7),
    ),
    "t3_metals": (
        _D("supply deficit",
           r"supply (?:deficit|shortage|disruption|tightness)|"
           r"mine (?:closure|halt|strike|outage|accident|suspension)|"
           r"smelter (?:cuts?|closure|shutdown)|export (?:ban|restriction)|"
           r"(?:inventor\w+|lme stocks?) (?:fall|fell|drop\w*|draw\w*)",
           +1, 0.9),
        _D("demand impulse",
           r"china (?:stimulus|infrastructure)|\brestocking\b|"
           r"grid (?:spending|investment)|ev demand|data ?cent(?:re|er) demand",
           +1, 0.8),
        _D("surplus",
           r"\bsurplus\b|oversupply|\bglut\b|record (?:output|production)|"
           r"(?:inventor\w+|lme stocks?) (?:build\w*|rise|rose|jump\w*|swell\w*)",
           -1, 0.9),
        _D("demand weakness",
           r"demand (?:weak\w*|slump\w*|slowdown|concerns?)|"
           r"china (?:slowdown|property (?:crisis|slump))|"
           r"construction (?:slump|weakness)|\btariffs?\b", -1, 0.8),
    ),
}

# Genuinely two-sided prints: true in both directions at once, so the model must
# not commit. A hot payrolls number is good growth AND bad for rate cuts; an oil
# price spike is bullish crude AND restrictive for the economy. These halve the
# score and raise a flag rather than picking a side.
TWO_SIDED: dict[str, tuple] = {
    "t3_us_macro": (
        re.compile(r"(?:payrolls?|jobs report|nonfarm|employment|wage growth|"
                   r"gdp)[^.]{0,40}(?:beat\w*|strong\w*|surge\w*|"
                   r"hotter|tops?|smash\w*)"),
        re.compile(r"strong(?:er)? (?:than expected )?(?:jobs|growth|economy)"),
    ),
    "t3_eu_macro": (
        re.compile(r"(?:employment|wage growth|gdp|pmi)[^.]{0,40}"
                   r"(?:beat\w*|strong\w*|surge\w*|hotter|tops?)"),
    ),
    "t3_us_rates": (
        # Growth beats push yields up (bearish bonds) for a bullish reason.
        re.compile(r"(?:payrolls?|jobs report|nonfarm|gdp)[^.]{0,40}"
                   r"(?:beat\w*|strong\w*|surge\w*|tops?)"),
    ),
}

# The noun each panel's reading is about, for labelling.
ASSET_NOUN: dict[str, str] = {
    "t3_us_macro": "the outlook",
    "t3_us_equities": "US equities",
    "t3_us_rates": "bonds",
    "t3_eu_macro": "the outlook",
    "t3_eu_markets": "European assets",
    "t3_energy": "crude",
    "t3_precious": "precious metals",
    "t3_metals": "industrial metals",
}

# US Macro and Europe Macro read a policy and growth backdrop, not a price, so
# "bullish the outlook" is the wrong register. They get their own verdict words.
_MACRO_PANELS = {"t3_us_macro", "t3_eu_macro"}

# Above this, a reading is called rather than left neutral.
CALL_CUT = 0.25
# Below this share of headlines firing any rule, the reading is flagged thin.
THIN_COVERAGE = 0.25


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

def _mover_spans(text: str) -> list[tuple[int, int, int, float]]:
    """(start, end, direction, intensity) for every direction verb in the text."""
    out = []
    for pattern, direction, weight in MOVERS:
        for m in pattern.finditer(text):
            out.append((m.start(), m.end(), direction, weight))
    return out


def _nearest_mover(movers, start: int, end: int):
    """The closest mover to a subject span, or None if all are too far away."""
    best = None
    best_gap = PAIR_WINDOW + 1
    for m_start, m_end, direction, weight in movers:
        gap = m_start - end if m_start >= end else start - m_end
        if 0 <= gap < best_gap:
            best, best_gap = (direction, weight), gap
    return best


def asset_read(title: str, panel: str) -> float | None:
    """Directional reading of one headline for one panel, -1.0 … +1.0.

    ``None`` means no rule fired — the headline says nothing directional about
    this panel's asset. That is emphatically not the same as neutral, and the
    caller must not average it in as zero.
    """
    text = (title or "").lower()
    if not text:
        return None

    contributions: list[float] = []

    # Subjects are consumed as they match so a later, more general pattern
    # cannot re-match inside an earlier one ("Treasury yields" is one subject,
    # not a yield and a bond pulling against each other).
    movers = _mover_spans(text)
    remaining = text
    for pattern, sign in SUBJECTS.get(panel, ()):
        match = re.search(pattern, remaining)
        if not match:
            continue
        paired = _nearest_mover(movers, match.start(), match.end())
        if paired:
            direction, intensity = paired
            contributions.append(sign * direction * intensity)
        # Blank the span rather than deleting it, so every later offset — and
        # every mover position already recorded — stays valid.
        remaining = (remaining[:match.start()]
                     + " " * (match.end() - match.start())
                     + remaining[match.end():])

    for _name, pattern, sign, weight in DRIVERS.get(panel, ()):
        if pattern.search(text):
            contributions.append(sign * weight)

    if not contributions:
        return None

    score = sum(contributions) / len(contributions)
    if any(rx.search(text) for rx in TWO_SIDED.get(panel, ())):
        score *= 0.5
    return round(max(-1.0, min(1.0, score)), 3)


def drivers_fired(title: str, panel: str) -> list[str]:
    """Names of the driver rules a headline trips — the 'why' behind its score."""
    text = (title or "").lower()
    return [name for name, pattern, _s, _w in DRIVERS.get(panel, ())
            if pattern.search(text)]


def label_for(score: float | None, panel: str) -> str:
    """Verdict word for a panel score. Macro panels read policy, not price."""
    noun = ASSET_NOUN.get(panel, "the asset")
    if score is None:
        return "No directional read"
    if panel in _MACRO_PANELS:
        if score >= CALL_CUT:
            return "Supportive"
        if score <= -CALL_CUT:
            return "Restrictive"
        return "Balanced"
    if score >= CALL_CUT:
        return f"Bullish {noun}"
    if score <= -CALL_CUT:
        return f"Bearish {noun}"
    return f"Neutral {noun}"


def panel_read(titles: list[str], panel: str) -> dict:
    """Aggregate the directional reading over one panel's headlines.

    ``coverage`` is the share of headlines that fired any rule at all. It is
    reported, rendered and warned about because it is the honest limit of this
    method: a +0.8 drawn from two headlines out of twenty-five is not a strong
    signal, it is two headlines.
    """
    scores = [asset_read(t, panel) for t in titles]
    fired = [s for s in scores if s is not None]
    n = len(titles)

    if not fired:
        return {"score": None, "label": label_for(None, panel), "noun":
                ASSET_NOUN.get(panel, "the asset"), "coverage": 0.0,
                "n_fired": 0, "n": n, "thin": True, "two_sided": 0,
                "bull": 0, "bear": 0}

    score = round(sum(fired) / len(fired), 3)
    coverage = round(len(fired) / n, 3) if n else 0.0
    two_sided = sum(1 for t in titles
                    if any(rx.search((t or "").lower())
                           for rx in TWO_SIDED.get(panel, ())))

    return {
        "score": score,
        "label": label_for(score, panel),
        "noun": ASSET_NOUN.get(panel, "the asset"),
        "coverage": coverage,
        "n_fired": len(fired),
        "n": n,
        "thin": coverage < THIN_COVERAGE or len(fired) < 4,
        "two_sided": two_sided,
        "bull": sum(1 for s in fired if s >= CALL_CUT),
        "bear": sum(1 for s in fired if s <= -CALL_CUT),
    }


def top_drivers(titles: list[str], panel: str, top_n: int = 5) -> list[dict]:
    """Which driver rules are doing the work, most-fired first."""
    counts: dict[str, int] = {}
    for title in titles:
        for name in drivers_fired(title, panel):
            counts[name] = counts.get(name, 0) + 1
    rows = [{"driver": k, "count": v} for k, v in counts.items()]
    rows.sort(key=lambda r: (-r["count"], r["driver"]))
    return rows[:top_n]


# Every panel must have subjects, an asset noun and an entry in the store, or a
# panel silently renders with no directional read at all.
assert set(SUBJECTS) == set(PANEL_KEYS), "SUBJECTS out of sync with PANEL_KEYS"
assert set(ASSET_NOUN) == set(PANEL_KEYS), "ASSET_NOUN out of sync with PANEL_KEYS"
assert set(DRIVERS) <= set(PANEL_KEYS), "DRIVERS names an unknown panel"
assert set(TWO_SIDED) <= set(PANEL_KEYS), "TWO_SIDED names an unknown panel"
