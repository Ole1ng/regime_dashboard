"""Deterministic, offline text scoring shared by the Tab 2 news and social panels.

No LLM and no network. Ported from ``market_almanack/analysis.py`` (VADER +
CountVectorizer themes + salience ranking) and extended in two ways this project
needs:

  * **Two tuned analyzers instead of one.** Stock headlines and retail chatter
    speak different languages. ``NEWS`` adds a finance lexicon so "beats",
    "downgrade" and "dilution" carry the weight they actually have in a
    headline; ``SOCIAL`` adds the WSB slang lexicon lifted from
    ``wsb_scraper/sentiment/keywords.py`` so "moon", "tendies" and "bagholder"
    score at all. Out of the box VADER reads every one of those as neutral.

  * **Per-ticker stopwords.** For a single-name corpus the ticker and company
    name appear in nearly every headline, so left in they dominate the theme
    list and say nothing. :func:`themes` takes an ``extra_stop`` set.

Both analyzers are module-level singletons — ``SentimentIntensityAnalyzer()``
parses its lexicon file on construction, which is slow enough to matter when a
refresh scores a few hundred texts.
"""

from __future__ import annotations

import re
from collections import Counter

from sklearn.feature_extraction.text import CountVectorizer
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# --------------------------------------------------------------------------- #
# Stopwords
# --------------------------------------------------------------------------- #

_FINANCE_NOISE = {
    "market", "markets", "stock", "stocks", "share", "shares", "today",
    "report", "reports", "reported", "says", "said", "say", "year", "years",
    "week", "weeks", "month", "months", "day", "days", "new", "update",
    "updates", "latest", "news", "amid", "could", "would", "may", "might",
    "set", "see", "sees", "get", "gets", "u", "s", "us", "vs", "via",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "january", "february", "march", "april", "june", "july",
    "august", "september", "october", "november", "december",
    "inc", "corp", "ltd", "co", "group", "plc", "billion", "million",
    "high", "low", "close", "open", "rise", "rises", "fall", "falls",
    "near", "back", "first", "top", "key", "amp", "com", "www", "http",
    "https", "campaign",
}


def _load_stopwords() -> set[str]:
    """NLTK English stopwords + finance noise, with an offline fallback."""
    base: set[str] = set()
    try:
        import nltk
        from nltk.corpus import stopwords

        try:
            base = set(stopwords.words("english"))
        except LookupError:
            nltk.download("stopwords", quiet=True)
            base = set(stopwords.words("english"))
    except Exception:
        base = {
            "the", "a", "an", "and", "or", "but", "if", "to", "of", "in", "on",
            "for", "with", "as", "at", "by", "from", "is", "are", "was", "were",
            "be", "been", "it", "its", "this", "that", "these", "those", "has",
            "have", "had", "will", "after", "over", "up", "down", "out", "into",
            "about", "more", "than", "then", "you", "your", "he", "she", "they",
            "we", "i", "not", "no", "all", "can", "his", "her",
        }
    return base | _FINANCE_NOISE


STOPWORDS = _load_stopwords()

# --------------------------------------------------------------------------- #
# Lexicons
# --------------------------------------------------------------------------- #

# Terms that are decisive in a financial headline and that stock VADER scores at
# or near zero. Valences are on VADER's -4..+4 scale.
FINANCE_LEXICON = {
    # Results and guidance
    "beats": 2.0, "beat": 1.5, "tops": 1.5, "misses": -2.0, "missed": -1.8,
    "guidance": 0.0, "raises": 1.8, "raised": 1.5, "lifts": 1.5,
    "cuts": -1.8, "cut": -1.5, "slashes": -2.5, "slashed": -2.5,
    "warns": -2.0, "warning": -1.8, "outlook": 0.0,
    # Analyst actions
    "upgrade": 2.0, "upgrades": 2.0, "upgraded": 2.0,
    "downgrade": -2.0, "downgrades": -2.0, "downgraded": -2.0,
    "overweight": 1.5, "underweight": -1.5, "reiterates": 0.5,
    # Corporate events
    "acquisition": 1.5, "acquire": 1.5, "takeover": 1.8, "buyout": 1.8,
    "merger": 1.0, "bid": 1.0, "stake": 0.5, "activist": 0.8,
    "buyback": 1.8, "repurchase": 1.5, "dividend": 1.0,
    "spinoff": 0.5, "ipo": 0.5,
    # Distress
    "bankruptcy": -3.5, "bankrupt": -3.5, "chapter": -1.5, "default": -3.0,
    "delisting": -3.0, "delisted": -3.0, "halted": -2.5, "halt": -2.0,
    "probe": -2.0, "investigation": -2.0, "subpoena": -2.5, "lawsuit": -1.8,
    "fraud": -3.5, "restatement": -2.5, "recall": -2.0, "layoffs": -1.5,
    "dilution": -2.0, "dilutive": -2.0, "offering": -1.2,
    # Price action
    "surges": 2.0, "surge": 2.0, "soars": 2.5, "soar": 2.5, "rally": 1.8,
    "jumps": 1.8, "jump": 1.5, "climbs": 1.2, "gains": 1.2,
    "plunges": -2.5, "plunge": -2.5, "tumbles": -2.2, "tumble": -2.2,
    "slumps": -2.0, "sinks": -2.0, "slides": -1.5, "drops": -1.5,
    "crashes": -3.0, "selloff": -2.0, "rout": -2.5,
    # Positioning
    "squeeze": 1.5, "short": -0.8, "shorts": -0.8, "bearish": -2.0,
    "bullish": 2.0, "oversold": 1.0, "overbought": -1.0,
}

# WSB slang, lifted from wsb_scraper/sentiment/keywords.py. Retail chatter is
# unreadable without it — VADER scores "moon", "tendies" and "bagholder" at 0.
WSB_LEXICON = {
    "moon": 2.0, "mooning": 2.0, "rocket": 1.5, "tendies": 1.5,
    "yolo": 1.0, "calls": 1.0, "long": 1.0, "bullish": 2.0,
    "squeeze": 1.5, "lambo": 1.5, "apes": 0.5, "diamond": 1.0,
    "hands": 0.5, "printing": 1.5, "green": 1.0, "ripping": 1.5,
    "upside": 1.0, "breakout": 1.5, "oversold": 1.0,
    "puts": -1.0, "short": -1.0, "bearish": -2.0, "crash": -2.0,
    "dump": -1.5, "dumping": -1.5, "bankrupt": -2.0, "bankruptcy": -2.0,
    "bags": -1.5, "bagholder": -2.0, "rekt": -2.0, "theta": -0.5,
    "decay": -1.0, "worthless": -2.0, "overvalued": -1.5, "fraud": -2.0,
    "scam": -2.0, "bubble": -1.5, "overbought": -1.0, "downside": -1.0,
    "resistance": -0.5, "rejection": -1.0, "breakdown": -1.5,
}


def _analyzer(*lexicons: dict) -> SentimentIntensityAnalyzer:
    analyzer = SentimentIntensityAnalyzer()
    for lexicon in lexicons:
        analyzer.lexicon.update(lexicon)
    return analyzer


NEWS = _analyzer(FINANCE_LEXICON)
SOCIAL = _analyzer(FINANCE_LEXICON, WSB_LEXICON)

_TICKER_RE = re.compile(r"\$[A-Z]{1,5}\b")
_URL_RE = re.compile(r"https?://\S+")
_CASHTAG_RE = re.compile(r"[$#]")

# VADER's ±0.05 convention for bucketing a compound score.
POS_CUT = 0.05
NEG_CUT = -0.05


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

def score(text: str, analyzer: SentimentIntensityAnalyzer = NEWS) -> float:
    """Compound sentiment for one string, -1.0 … +1.0."""
    return analyzer.polarity_scores(text or "")["compound"]


def sentiment(texts: list[str],
              analyzer: SentimentIntensityAnalyzer = NEWS) -> dict:
    """Aggregate sentiment over a corpus.

    Returns the same shape market_almanack's ``_sentiment`` does, so the
    frontend tone rendering is shared: tone / mean / pos-neg-neu counts and
    percentages / n.
    """
    if not texts:
        return {"tone": "No data", "mean": 0.0, "pos_pct": 0, "neg_pct": 0,
                "neu_pct": 0, "pos": 0, "neg": 0, "neu": 0, "n": 0}

    scores = [score(t, analyzer) for t in texts]
    pos = sum(1 for s in scores if s >= POS_CUT)
    neg = sum(1 for s in scores if s <= NEG_CUT)
    neu = len(scores) - pos - neg
    n = len(scores)
    mean = sum(scores) / n

    if mean >= POS_CUT:
        tone = "Bullish"
    elif mean <= NEG_CUT:
        tone = "Bearish"
    else:
        tone = "Mixed"

    pos_pct, neg_pct, neu_pct = _apportion([pos, neg, neu], n)
    return {"tone": tone, "mean": round(mean, 3),
            "pos_pct": pos_pct, "neg_pct": neg_pct, "neu_pct": neu_pct,
            "pos": pos, "neg": neg, "neu": neu, "n": n}


def _apportion(counts: list[int], total: int) -> list[int]:
    """Percentages that sum to exactly 100 (largest-remainder allocation).

    Rounding each share independently gives 33/33/33 for an even three-way
    split, and the panel prints all three in one sentence where summing to 99
    reads as a bug.
    """
    if not total:
        return [0] * len(counts)
    exact = [100 * c / total for c in counts]
    out = [int(x) for x in exact]
    remainder = 100 - sum(out)
    # Hand the leftover points to the largest fractional parts, biggest first.
    order = sorted(range(len(counts)), key=lambda i: exact[i] - out[i], reverse=True)
    for i in order[:remainder]:
        out[i] += 1
    return out


def themes(texts: list[str], extra_stop: set[str] | None = None,
           top_n: int = 12) -> list[dict]:
    """Top unigrams + bigrams by document frequency.

    ``extra_stop`` should carry the ticker and the words of the company name —
    on a single-name corpus they otherwise take every top slot.
    """
    if len(texts) < 2:
        return []
    stop = list(STOPWORDS | {w.lower() for w in (extra_stop or set())})

    for min_df in (2, 1):  # relax for tiny corpora
        try:
            vec = CountVectorizer(
                ngram_range=(1, 2),
                min_df=min_df,
                stop_words=stop,
                token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z'&-]+\b",
            )
            matrix = vec.fit_transform(texts)
        except ValueError:
            continue
        counts = matrix.sum(axis=0).A1
        terms = vec.get_feature_names_out()
        pairs = sorted(zip(terms, counts), key=lambda p: p[1], reverse=True)
        return [{"term": t, "count": int(c)} for t, c in pairs[:top_n]]
    return []


def salient(items: list[dict], theme_list: list[dict], top_n: int = 6,
            analyzer: SentimentIntensityAnalyzer = NEWS) -> list[dict]:
    """Rank items by |sentiment| x (1 + count of top-theme terms present).

    The +1 keeps an emotive headline that happens to use no theme vocabulary in
    contention against a flat one that uses several.
    """
    terms = [t["term"] for t in theme_list]
    scored = []
    for item in items:
        title = item.get("title") or ""
        low = title.lower()
        compound = score(title, analyzer)
        hits = sum(1 for term in terms if term in low)
        scored.append((abs(compound) * (1 + hits), compound, item))

    scored.sort(key=lambda row: row[0], reverse=True)
    return [
        {"title": item["title"], "link": item.get("link", ""),
         "source": item.get("source", ""), "published": item.get("published"),
         "score": round(compound, 3)}
        for _, compound, item in scored[:top_n]
    ]


def by_source(items: list[dict],
              analyzer: SentimentIntensityAnalyzer = NEWS) -> list[dict]:
    """Mean sentiment per publisher, most-covered first.

    A wire service and a promotional blog reporting the same event read very
    differently; splitting by source makes that visible instead of averaging it
    away.
    """
    grouped: dict[str, list[float]] = {}
    for item in items:
        src = (item.get("source") or "Unknown").strip()
        grouped.setdefault(src, []).append(score(item.get("title") or "", analyzer))

    rows = [{"source": src, "n": len(vals),
             "mean": round(sum(vals) / len(vals), 3)}
            for src, vals in grouped.items()]
    rows.sort(key=lambda r: (-r["n"], r["source"]))
    return rows


def top_terms(texts: list[str], extra_stop: set[str] | None = None,
              top_n: int = 10) -> list[dict]:
    """Plain word frequency, used for social chatter where bigrams add little."""
    stop = STOPWORDS | {w.lower() for w in (extra_stop or set())}
    counts: Counter = Counter()
    for text in texts:
        clean = _CASHTAG_RE.sub(" ", _URL_RE.sub(" ", text or "")).lower()
        for word in re.findall(r"[a-z][a-z'&-]{2,}", clean):
            if word not in stop:
                counts[word] += 1
    return [{"term": t, "count": c} for t, c in counts.most_common(top_n)]


def stop_for(symbol: str, company: str | None) -> set[str]:
    """Stopword set masking the subject itself out of its own theme list.

    Headlines are inconsistent about possessives — "Wendy's" and "Wendys" both
    occur freely — so each name word is added in both forms. Without the
    apostrophe-stripped variant the company name still takes the top theme slot
    and crowds out the terms that actually say something.
    """
    out = {symbol.lower(), f"${symbol}".lower()}
    for word in re.split(r"[^A-Za-z']+", company or ""):
        if len(word) > 1:
            low = word.lower()
            out.add(low)
            out.add(low.replace("'", ""))
            out.add(low.rstrip("'s") if low.endswith("'s") else low + "s")
    return out


def cashtags(texts: list[str], exclude: str | None = None,
             top_n: int = 8) -> list[dict]:
    """Co-mentioned $TICKERs — what else the crowd is pairing this name with."""
    counts: Counter = Counter()
    for text in texts:
        for tag in _TICKER_RE.findall(text or ""):
            if exclude and tag.upper() == f"${exclude.upper()}":
                continue
            counts[tag.upper()] += 1
    return [{"symbol": t, "count": c} for t, c in counts.most_common(top_n)]
