"""Multi-source news aggregation and offline sentiment for one ticker.

Three feeds, merged and deduplicated:

  * **Google News RSS** — the widest net by a distance. Queried on the *company
    name* rather than the ticker: "WEN stock" returns 73 items, `"Wendy's"
    stock OR earnings` returns 100 with better coverage, because three-letter
    tickers collide with ordinary words and Google indexes the company name.
  * **Seeking Alpha** — its per-symbol combined feed, which surfaces analysis
    and transcripts the news wires do not.
  * **Finviz's news table** — 100 rows already in hand from the squeeze panel's
    scrape, so it costs nothing. Skews toward aggregators and press releases,
    which is useful coverage the other two under-weight.

Scoring is VADER with the finance lexicon from ``_sentiment_util`` — no LLM, no
network, deterministic. The important caveat, surfaced in the panel: this is
*extractive*. It measures the tone of headlines, which is not the same as
whether the news is good for the equity. A headline about a takeover premium and
one about a short-seller report can both read positive to a bag-of-words model.
"""

from __future__ import annotations

import re
import socket
import urllib.parse as urlparse
from datetime import datetime, timezone

import feedparser
import requests

from . import _sentiment_util as senti

GOOGLE_NEWS_TMPL = (
    "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
)
SEEKING_ALPHA_TMPL = "https://seekingalpha.com/api/sa/combined/{sym}.xml"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

MAX_ITEMS = 40
FEED_TIMEOUT = 15.0
LOOKBACK = "when:7d"

# Google News appends " - Publisher" to every title. Left in, the publisher
# names dominate the theme list and pollute the sentiment corpus.
_GOOGLE_SUFFIX_RE = re.compile(r"\s+-\s+[^-]{2,40}$")
_NORM_RE = re.compile(r"[^a-z0-9 ]+")

CAVEAT = ("Extractive analysis — the frequency and tone of headlines, not an "
          "interpretation of what they mean for the equity.")


def refresh(symbol: str, company: str | None = None,
            finviz_news: list[dict] | None = None) -> dict:
    """Panel entry point. ``finviz_news`` is reused from the squeeze scrape."""
    items = []
    errors = []

    for name, fetch in (
        ("Google News", lambda: _google_news(symbol, company)),
        ("Seeking Alpha", lambda: _seeking_alpha(symbol)),
    ):
        try:
            items.extend(fetch())
        except Exception as exc:  # one dead feed must not empty the panel
            errors.append(f"{name}: {type(exc).__name__}")

    if finviz_news:
        items.extend(_normalise_finviz(finviz_news))

    if not items and errors:
        raise RuntimeError("every news feed failed — " + "; ".join(errors))

    return compute(items, symbol, company=company, errors=errors)


# --------------------------------------------------------------------------- #
# Feeds
# --------------------------------------------------------------------------- #

def _parse_feed(url: str) -> list:
    """feedparser via requests, so the browser UA and timeout actually apply."""
    resp = requests.get(url, headers=_HEADERS, timeout=FEED_TIMEOUT)
    resp.raise_for_status()
    return feedparser.parse(resp.content).entries


def _google_news(symbol: str, company: str | None) -> list[dict]:
    """Query on the company name where known — see the module docstring."""
    if company:
        query = f'"{company}" stock OR earnings {LOOKBACK}'
    else:
        query = f"{symbol} stock {LOOKBACK}"
    url = GOOGLE_NEWS_TMPL.format(query=urlparse.quote(query))

    out = []
    for entry in _parse_feed(url):
        title = _GOOGLE_SUFFIX_RE.sub("", entry.get("title", "")).strip()
        if not title:
            continue
        out.append({
            "title": title,
            "link": entry.get("link", ""),
            "source": _google_source(entry),
            "published": _entry_time(entry),
            "feed": "Google News",
        })
    return out


def _google_source(entry) -> str:
    src = entry.get("source")
    if isinstance(src, dict) and src.get("title"):
        return src["title"]
    # Fall back to the publisher Google appended to the title.
    match = re.search(r"\s+-\s+([^-]{2,40})$", entry.get("title", ""))
    return match.group(1).strip() if match else "Google News"


def _seeking_alpha(symbol: str) -> list[dict]:
    out = []
    for entry in _parse_feed(SEEKING_ALPHA_TMPL.format(sym=symbol)):
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        out.append({
            "title": title,
            "link": entry.get("link", ""),
            "source": "Seeking Alpha",
            "published": _entry_time(entry),
            "feed": "Seeking Alpha",
        })
    return out


def _normalise_finviz(rows: list[dict]) -> list[dict]:
    return [{"title": r["title"], "link": r["link"], "source": r.get("source") or "Finviz",
             "published": r.get("published"), "feed": "Finviz"}
            for r in rows if r.get("title")]


def _entry_time(entry) -> float | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Compute
# --------------------------------------------------------------------------- #

def compute(items: list[dict], symbol: str, company: str | None = None,
            errors: list[str] | None = None) -> dict:
    """Pure: merged raw items -> panel payload."""
    merged = _dedupe(items)
    merged.sort(key=lambda i: i.get("published") or 0, reverse=True)
    merged = merged[:MAX_ITEMS]

    titles = [i["title"] for i in merged]
    stop = senti.stop_for(symbol, company)

    sentiment = senti.sentiment(titles, senti.NEWS)
    themes = senti.themes(titles, extra_stop=stop)

    for item in merged:
        item["compound"] = round(senti.score(item["title"], senti.NEWS), 3)

    payload = {
        "symbol": symbol,
        "company": company,
        "count": len(merged),
        "empty": not merged,
        "sentiment": sentiment,
        "tone": sentiment["tone"],
        "mean": sentiment["mean"],
        "themes": themes,
        "salient": senti.salient(merged, themes, analyzer=senti.NEWS),
        "source_breakdown": senti.by_source(merged, senti.NEWS)[:10],
        "feeds": _feed_counts(merged),
        "items": merged,
        "errors": errors or [],
        "caveat": CAVEAT,
    }
    payload["commentary"] = _commentary(payload)
    return payload


def _dedupe(items: list[dict]) -> list[dict]:
    """Drop repeats by link and by normalised title.

    The same wire story reaches all three feeds under slightly different titles
    and always under different URLs, so link-only dedup leaves obvious
    duplicates in the corpus and skews the sentiment mean.
    """
    seen_links: set[str] = set()
    seen_titles: set[str] = set()
    out = []

    for item in items:
        link = (item.get("link") or "").split("?")[0]
        title_key = _NORM_RE.sub("", (item.get("title") or "").lower()).strip()
        title_key = " ".join(title_key.split())
        if not title_key:
            continue
        if (link and link in seen_links) or title_key in seen_titles:
            continue
        if link:
            seen_links.add(link)
        seen_titles.add(title_key)
        out.append(item)

    return out


def _feed_counts(items: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.get("feed", "?")] = counts.get(item.get("feed", "?"), 0) + 1
    return [{"feed": k, "n": v} for k, v in sorted(counts.items(),
                                                   key=lambda p: -p[1])]


# --------------------------------------------------------------------------- #
# Commentary
# --------------------------------------------------------------------------- #

def _commentary(p: dict) -> dict:
    sentences: list[str] = []
    warnings: list[str] = []
    s = p["sentiment"]
    sym = p["symbol"]

    if p["empty"]:
        return {"headline": f"No recent news found for {sym}",
                "warnings": ["⚠ No headlines returned by any feed."],
                "sentences": []}

    feeds = ", ".join(f"{f['feed']} {f['n']}" for f in p["feeds"])
    sentences.append(
        f"{s['n']} headlines across {len(p['feeds'])} feeds ({feeds}). Tone is "
        f"{s['tone'].lower()} — {s['pos_pct']}% positive, {s['neg_pct']}% "
        f"negative, {s['neu_pct']}% neutral, mean compound {s['mean']:+.3f}.")

    if p["themes"]:
        top = ", ".join(t["term"] for t in p["themes"][:6])
        sentences.append(f"Dominant terms: {top}.")

    # A split between publishers is more informative than the average.
    rows = [r for r in p["source_breakdown"] if r["n"] >= 2]
    if len(rows) >= 2:
        best = max(rows, key=lambda r: r["mean"])
        worst = min(rows, key=lambda r: r["mean"])
        if best["mean"] - worst["mean"] >= 0.4:
            sentences.append(
                f"Coverage is not uniform: {best['source']} reads "
                f"{best['mean']:+.2f} across {best['n']} items while "
                f"{worst['source']} reads {worst['mean']:+.2f} across "
                f"{worst['n']} — the story depends on who is telling it.")

    if s["n"] < 8:
        warnings.append(f"⚠ Only {s['n']} headlines — the tone reading is thin.")
    if p["errors"]:
        warnings.append("⚠ Some feeds failed: " + "; ".join(p["errors"]) + ".")

    return {"headline": f"{s['tone']} news tone — {s['n']} headlines, "
                        f"mean {s['mean']:+.3f}",
            "warnings": warnings, "sentences": sentences}
