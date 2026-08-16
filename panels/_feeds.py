"""Feed registry and parallel RSS fetcher for the Tab 3 news screener.

Two kinds of source, deliberately mixed:

  * **Google News search, scoped to a trusted-domain whitelist.** One query per
    panel. This is the workhorse — it returned 100 on-topic entries for every
    one of the eight panels on the verification date, and it is the *only*
    working route to Reuters and Bloomberg, neither of which publishes a usable
    public feed any more. Precision comes from the ``site:`` clause: the query
    cannot return a promotional blog because no such domain is on the list.

  * **Publisher feeds direct.** CNBC, WSJ, MarketWatch, FT, Investing.com and
    Yahoo for market coverage, plus the primary sources the wires only
    paraphrase — the Fed, the ECB, the Bank of England and the EIA. These are
    *not* panel-scoped; a broad feed carries stories for every panel at once, so
    :func:`news_screener.classify` routes them by keyword.

The generic RSS plumbing here (``parse_feed``, ``entry_time``, ``dedupe``,
``strip_google_suffix``) was lifted out of ``ticker_news`` so Tab 2 and Tab 3
share one implementation rather than two that drift; ``ticker_news`` imports
these back under its old private names.

See DATA_SOURCES.md §12 for what was probed and rejected. The short version:
Reuters' own feeds are dead, BLS and OPEC return 403 to any user agent, and
Kitco, Treasury, Mining Weekly and Eurostat all 404. Do not re-add them without
re-probing with ``tools/probe_news.py``.
"""

from __future__ import annotations

import re
import urllib.parse as urlparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

import feedparser
import requests

# --------------------------------------------------------------------------- #
# Panels
# --------------------------------------------------------------------------- #

# Ordered: this is also the render order on the tab. Keys are the store panel
# keys, so they must stay in sync with store.PANEL_KEYS.
PANELS: tuple[tuple[str, str], ...] = (
    ("t3_us_macro", "US Macro"),
    ("t3_us_equities", "US Equities"),
    ("t3_us_rates", "US Fixed Income"),
    ("t3_eu_macro", "Europe Macro"),
    ("t3_eu_markets", "Europe Equities & Fixed Income"),
    ("t3_energy", "Oil & Energy"),
    ("t3_precious", "Precious Metals"),
    ("t3_metals", "Industrial Metals"),
)

PANEL_KEYS: tuple[str, ...] = tuple(k for k, _ in PANELS)
PANEL_TITLES: dict[str, str] = dict(PANELS)

# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

FEED_TIMEOUT = 12.0
# ~40 feeds. Twelve workers keeps a full refresh near 20 seconds without opening
# so many sockets that a slow publisher starves the rest.
MAX_WORKERS = 12

# Google News appends " - Publisher" to every title. Left in, the publisher
# names dominate the theme list and pollute the sentiment corpus.
_GOOGLE_SUFFIX_RE = re.compile(r"\s+-\s+[^-]{2,40}$")
_NORM_RE = re.compile(r"[^a-z0-9 ]+")


def parse_feed(url: str, timeout: float = FEED_TIMEOUT) -> list:
    """feedparser via requests, so the browser UA and timeout actually apply."""
    resp = requests.get(url, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return feedparser.parse(resp.content).entries


def entry_time(entry) -> float | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def strip_google_suffix(title: str) -> str:
    return _GOOGLE_SUFFIX_RE.sub("", title or "").strip()


# Google labels the same publisher differently depending on which query found
# it — "Bloomberg.com" from a topic search, "Bloomberg" from a site search —
# which splits one publisher into two rows in the per-source sentiment
# breakdown and halves the sample behind each.
_SOURCE_ALIASES = {
    "bloomberg.com": "Bloomberg", "bloomberg news": "Bloomberg",
    "reuters.com": "Reuters",
    "cnbc.com": "CNBC",
    "marketwatch.com": "MarketWatch",
    "the wall street journal": "WSJ", "wsj.com": "WSJ",
    "wall street journal": "WSJ",
    "ft.com": "Financial Times", "financial times": "Financial Times",
    "the financial times": "Financial Times",
    "yahoo finance": "Yahoo Finance", "yahoo! finance": "Yahoo Finance",
    "investing.com": "Investing.com",
    "the associated press": "AP", "associated press": "AP", "apnews.com": "AP",
    "oilprice.com": "OilPrice",
    "mining.com": "Mining.com",
    "barrons.com": "Barron's", "barron's": "Barron's",
    "the economist": "The Economist",
}


def canonical_source(name: str) -> str:
    """One name per publisher, so the source breakdown does not double-count."""
    clean = (name or "").strip()
    if not clean:
        return "Unknown"
    return _SOURCE_ALIASES.get(clean.lower(), clean)


def google_source(entry) -> str:
    """Publisher for a Google News entry, from the tag or the title suffix."""
    src = entry.get("source")
    if isinstance(src, dict) and src.get("title"):
        return canonical_source(src["title"])
    match = re.search(r"\s+-\s+([^-]{2,40})$", entry.get("title", ""))
    return canonical_source(match.group(1)) if match else "Google News"


def dedupe(items: list[dict]) -> list[dict]:
    """Drop repeats by link and by normalised title.

    The same wire story reaches several feeds under slightly different titles
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


# --------------------------------------------------------------------------- #
# Google News — per-panel queries
# --------------------------------------------------------------------------- #

GOOGLE_NEWS_TMPL = (
    "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
)

# Only these domains may answer a panel query. This is what makes a bare
# keyword search usable: "gold" across the open web is jewellery advertising,
# but "gold" restricted to these eight publishers is the metals tape.
TRUSTED_SITES = (
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com",
    "cnbc.com", "marketwatch.com", "apnews.com", "barrons.com",
)

_SITE_CLAUSE = "(" + " OR ".join(f"site:{d}" for d in TRUSTED_SITES) + ")"

# ⚠ `when:` is NOT honoured on a keyword query. Measured 2026-08-16: the US
# Fixed Income topic query with `when:2d` returned 100 entries whose *median*
# age was 2,226 hours and whose oldest was eleven years. Add topic keywords and
# Google switches to all-time relevance ranking and treats the date operator as
# a hint.
#
# It IS honoured on a bare site query — see WIRE_SITES below, where
# `site:reuters.com when:1d` came back 98/98 inside 24 hours. That asymmetry is
# the whole reason this module has two kinds of Google query rather than one.
#
# So the operator stays (it still biases relevance towards recent) but nothing
# depends on it: news_screener applies a hard timestamp window, which is the
# only actual guarantee of recency this tab has.
LOOKBACK = "when:2d"

# Topic clause per panel. Quoted phrases matter: unquoted `S&P 500` is parsed as
# three tokens and drags in anything containing "500".
_TOPICS: dict[str, str] = {
    "t3_us_macro": (
        '("Federal Reserve" OR FOMC OR CPI OR "inflation data" OR '
        '"jobs report" OR "nonfarm payrolls" OR "US economy" OR '
        '"consumer confidence" OR tariffs OR "rate cut")'
    ),
    "t3_us_equities": (
        '("S&P 500" OR Nasdaq OR "Dow Jones" OR "Wall Street" OR '
        '"US stocks" OR "stock market" OR "earnings season" OR "Russell 2000")'
    ),
    "t3_us_rates": (
        '("Treasury yields" OR "10-year Treasury" OR "bond market" OR '
        '"yield curve" OR "credit spreads" OR "corporate bonds" OR '
        '"Treasury auction" OR "high yield")'
    ),
    "t3_eu_macro": (
        '("European Central Bank" OR ECB OR "euro zone" OR eurozone OR '
        '"Bank of England" OR "UK inflation" OR "European economy" OR '
        '"euro area" OR Bundesbank)'
    ),
    "t3_eu_markets": (
        '("Stoxx 600" OR DAX OR "FTSE 100" OR CAC OR "European stocks" OR '
        '"European shares" OR bunds OR gilts OR "European bonds")'
    ),
    "t3_energy": (
        '(oil OR crude OR OPEC OR WTI OR Brent OR "natural gas" OR LNG OR '
        '"energy prices" OR refinery OR "oil inventories")'
    ),
    "t3_precious": (
        '(gold OR silver OR platinum OR palladium OR bullion OR '
        '"precious metals" OR "gold price")'
    ),
    "t3_metals": (
        '(copper OR aluminium OR aluminum OR nickel OR zinc OR "iron ore" OR '
        '"base metals" OR "industrial metals" OR LME OR steel)'
    ),
}


def panel_query(panel: str) -> str:
    """The full Google News query string for one panel."""
    return f"{_TOPICS[panel]} {_SITE_CLAUSE} {LOOKBACK}"


def panel_query_url(panel: str) -> str:
    return GOOGLE_NEWS_TMPL.format(query=urlparse.quote(panel_query(panel)))


# --------------------------------------------------------------------------- #
# Wire queries — one recency stream per publisher
# --------------------------------------------------------------------------- #
#
# A *bare* site query with a date operator behaves completely differently from
# a keyword one: it returns a recency-ordered stream rather than an all-time
# relevance ranking. Measured 2026-08-16:
#
#     site:reuters.com   when:1d   98/98  inside 24h   median 12h
#     site:bloomberg.com when:1d   98/100 inside 24h   median  9h
#     site:ft.com        when:1d   76/100 inside 24h   median 21h
#     site:wsj.com       when:1d   76/100 inside 24h   median 15h
#
# This is how Reuters and Bloomberg get covered at all — neither publishes a
# working public feed. The items arrive unlabelled and are keyword-routed like
# any other publisher feed, so one query serves all eight panels.
WIRE_SITES: tuple[str, ...] = (
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com",
    "cnbc.com", "marketwatch.com",
)

WIRE_LOOKBACK = "when:1d"

# Human-readable publisher name per domain, for the source breakdown.
_WIRE_NAMES = {
    "reuters.com": "Reuters", "bloomberg.com": "Bloomberg",
    "ft.com": "Financial Times", "wsj.com": "WSJ",
    "cnbc.com": "CNBC", "marketwatch.com": "MarketWatch",
}


def wire_query_url(domain: str) -> str:
    query = f"site:{domain} {WIRE_LOOKBACK}"
    return GOOGLE_NEWS_TMPL.format(query=urlparse.quote(query))


# --------------------------------------------------------------------------- #
# Publisher feeds
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Feed:
    """One direct publisher feed.

    ``fallback_panels`` is where an item lands when the keyword classifier finds
    nothing. Broad feeds leave it empty — an unclassifiable CNBC top-news story
    genuinely belongs to no panel and should be dropped rather than dumped into
    one at random. Narrow feeds (the EIA, Mining.com) set it, because everything
    they publish is on-topic by construction even when the headline avoids the
    obvious vocabulary.
    """

    publisher: str
    url: str
    fallback_panels: tuple[str, ...] = field(default=())


_CNBC = "https://www.cnbc.com/id/{}/device/rss/rss.html"

PUBLISHER_FEEDS: tuple[Feed, ...] = (
    # --- broad market coverage: classifier-routed ---------------------------- #
    Feed("CNBC", _CNBC.format(100003114)),                    # top news
    Feed("CNBC", _CNBC.format(20910258)),                     # economy
    Feed("CNBC", _CNBC.format(10000664)),                     # finance
    Feed("CNBC", _CNBC.format(19836768), ("t3_energy",)),     # energy
    # ⚠ WSJ's own feeds (feeds.a.dj.com/rss/RSSMarketsMain.xml,
    # WSJcomUSBusiness.xml) and MarketWatch's realtimeheadlines/marketpulse are
    # deliberately absent. All four return HTTP 200 and parse cleanly — and are
    # frozen archives. Measured 2026-08-16: median entry age 13,575h for WSJ and
    # 15,161h for MarketWatch, i.e. a year and a half stale, with plausible
    # market headlines at the top that read as current. WSJ and MarketWatch are
    # covered properly by their WIRE_SITES queries instead.
    Feed("MarketWatch", "https://feeds.marketwatch.com/marketwatch/topstories/"),
    # FT sections are only loosely curated — its global-economy feed led with a
    # school-fees column on the verification date — so every FT item goes
    # through the classifier and none carries a fallback panel.
    Feed("Financial Times", "https://www.ft.com/rss/home"),
    Feed("Financial Times", "https://www.ft.com/markets?format=rss"),
    Feed("Financial Times", "https://www.ft.com/global-economy?format=rss"),
    Feed("Financial Times", "https://www.ft.com/commodities?format=rss"),
    Feed("Financial Times", "https://www.ft.com/world/europe?format=rss"),
    Feed("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    Feed("Investing.com", "https://www.investing.com/rss/news_14.rss"),   # economy
    Feed("Investing.com", "https://www.investing.com/rss/news_11.rss"),   # commodities
    Feed("Investing.com", "https://www.investing.com/rss/news_25.rss"),   # stocks
    Feed("Investing.com", "https://www.investing.com/rss/news_1.rss"),    # forex/rates
    # (investing.com/rss/commodities.rss is omitted: it carries no timestamps at
    # all, so every item is dropped by the recency window. news_11 above is the
    # commodities feed that works.)

    # --- primary sources: what the wires are paraphrasing --------------------- #
    Feed("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml",
         ("t3_us_macro",)),
    Feed("Federal Reserve", "https://www.federalreserve.gov/feeds/press_monetary.xml",
         ("t3_us_macro", "t3_us_rates")),
    Feed("Federal Reserve", "https://www.federalreserve.gov/feeds/speeches.xml",
         ("t3_us_macro",)),
    Feed("ECB", "https://www.ecb.europa.eu/rss/press.html", ("t3_eu_macro",)),
    Feed("Bank of England", "https://www.bankofengland.co.uk/rss/news",
         ("t3_eu_macro",)),
    Feed("EIA", "https://www.eia.gov/rss/todayinenergy.xml", ("t3_energy",)),
    Feed("EIA", "https://www.eia.gov/rss/press_rss.xml", ("t3_energy",)),
    Feed("OilPrice", "https://oilprice.com/rss/main", ("t3_energy",)),
    Feed("Mining.com", "https://www.mining.com/feed/", ("t3_metals",)),
)


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def _fetch_query(panel: str) -> list[dict]:
    """One panel's Google News query. Items carry an explicit panel."""
    out = []
    for entry in parse_feed(panel_query_url(panel)):
        title = strip_google_suffix(entry.get("title", ""))
        if not title:
            continue
        source = google_source(entry)
        out.append({
            "title": title,
            "link": entry.get("link", ""),
            "source": source,
            "publisher": source,
            "published": entry_time(entry),
            "feed": "Google News",
            "panels": (panel,),
        })
    return out


def _fetch_wire(domain: str) -> list[dict]:
    """One publisher's recency stream. Unlabelled — the classifier routes it."""
    name = _WIRE_NAMES.get(domain, domain)
    out = []
    for entry in parse_feed(wire_query_url(domain)):
        title = strip_google_suffix(entry.get("title", ""))
        if not title:
            continue
        out.append({
            "title": title,
            "link": entry.get("link", ""),
            "source": name,
            "publisher": name,
            "published": entry_time(entry),
            "feed": name,
            "panels": None,
            "fallback_panels": (),
        })
    return out


def _fetch_publisher(feed: Feed) -> list[dict]:
    """One publisher feed. Items carry no panel — the classifier assigns one."""
    out = []
    for entry in parse_feed(feed.url):
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        out.append({
            "title": title,
            "link": entry.get("link", ""),
            "source": feed.publisher,
            "publisher": feed.publisher,
            "published": entry_time(entry),
            "feed": feed.publisher,
            "panels": None,
            "fallback_panels": feed.fallback_panels,
        })
    return out


def fetch_all(workers: int = MAX_WORKERS) -> tuple[list[dict], list[str]]:
    """Fetch every query and publisher feed in parallel.

    Returns ``(items, errors)``. A dead feed is recorded in ``errors`` and never
    raised — thirty-odd third-party feeds means one of them is essentially
    always down, and a single 404 must not empty eight panels. Only a *total*
    failure raises, because that is a real outage (or no network) rather than
    one flaky publisher.
    """
    jobs: list[tuple[str, callable]] = []
    for panel in PANEL_KEYS:
        jobs.append((f"Google News/{PANEL_TITLES[panel]}",
                     lambda p=panel: _fetch_query(p)))
    for domain in WIRE_SITES:
        jobs.append((f"Wire/{_WIRE_NAMES.get(domain, domain)}",
                     lambda d=domain: _fetch_wire(d)))
    for feed in PUBLISHER_FEEDS:
        jobs.append((f"{feed.publisher} <{_short_url(feed.url)}>",
                     lambda f=feed: _fetch_publisher(f)))

    items: list[dict] = []
    errors: list[str] = []

    def run(job):
        name, fn = job
        try:
            return name, fn(), None
        except Exception as exc:
            return name, [], f"{name}: {type(exc).__name__}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _name, got, err in pool.map(run, jobs):
            items.extend(got)
            if err:
                errors.append(err)

    if not items and errors:
        raise RuntimeError(f"every news feed failed ({len(errors)} of "
                           f"{len(jobs)}) — {errors[0]}")
    return items, errors


# Path segments that identify the feed format rather than the feed, and so
# distinguish nothing. Without dropping these, every CNBC feed labels itself
# "rss.html" and the probe output cannot tell Economy from Energy.
_URL_NOISE = {"", "device", "rss", "rss.html", "feed", "feeds"}


def _short_url(url: str) -> str:
    """Just enough of the URL to tell two feeds from the same publisher apart."""
    parsed = urlparse.urlparse(url)
    parts = [p for p in (parsed.path or "").split("/") if p not in _URL_NOISE]
    tail = "/".join(parts) or parsed.netloc
    return f"{tail}{'?' + parsed.query if parsed.query else ''}"[:44]
