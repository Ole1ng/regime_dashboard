"""Finviz quote-page scrape and parse helpers.

One HTTP request to ``finviz.com/quote.ashx`` yields three things Tab 2 needs:
the 83-field fundamentals snapshot (short interest, ownership, analyst target,
SMA distances, earnings date), the company's display name, and a 100-row news
table. Fetching them together keeps the tab to a single Finviz hit.

Why this is a scrape and not a library call: ``finvizfinance``'s ``Quote``
class is broken against the current site — ``ticker_fundament()`` raises
``AttributeError`` because the page no longer carries the element it looks for.
The screener half of that library still works and Market Almanack still uses it;
only the per-ticker quote path is affected.

**The separator trick.** Finviz packs two values into one ``<td>`` as sibling
``<span>``s. ``get_text(strip=True)`` concatenates them with nothing in between,
which makes the result unparseable::

    '52W High' -> '10.84-20.26%'      # price, then % from high
    '52W Low'  -> '6.0742.34%'        # price, then % from low  (no sign to split on!)

Passing ``separator="\\x1f"`` puts an ASCII unit separator between the spans, so
the same cells come back as ``'10.84\\x1f-20.26%'`` and ``'6.07\\x1f42.34%'`` and
split cleanly. Every value in the snapshot dict is stored post-split as a list
when it holds more than one field, and :func:`first` / :func:`second` read them.

All parsing lives here so a Finviz layout change is a one-file fix.
"""

from __future__ import annotations

import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

QUOTE_URL_TMPL = "https://finviz.com/quote.ashx?t={sym}&p=d"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# ASCII unit separator — see the module docstring. Chosen because it cannot
# occur in Finviz's own text.
SEP = "\x1f"

# Finviz writes these for "no value". Treated as None everywhere.
_MISSING = {"-", "", "--", "N/A"}

_SUFFIXES = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}

_NEWS_DATE_RE = re.compile(r"^([A-Z][a-z]{2}-\d{2}-\d{2})?\s*(\d{1,2}:\d{2}[AP]M)$")


# A healthy quote page yields ~83 label/value pairs across six tables. The guard
# sits well below that so a genuinely smaller listing still parses, but well
# above the 14 pairs a single table would give.
EXPECTED_PAIRS = 83
MIN_CELLS = 80  # i.e. 40 pairs


class NotFound(Exception):
    """Raised when Finviz has no quote page for the symbol."""


class ParseChanged(Exception):
    """Raised when the page loads but no longer has the expected shape.

    Deliberately distinct from :class:`NotFound`: an unknown ticker is normal
    and gets a tidy in-panel message, whereas this means the scrape needs
    fixing and should surface as a loud error.
    """


# --------------------------------------------------------------------------- #
# Scalar parsing
# --------------------------------------------------------------------------- #

def clean(value) -> str | None:
    """Normalise one raw cell to a string, or None when Finviz means 'missing'."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text in _MISSING else text


def first(value) -> str | None:
    """Left-hand field of a possibly glued cell ('10.84\\x1f-20.26%' -> '10.84')."""
    text = clean(value)
    if text is None:
        return None
    return clean(text.split(SEP)[0])


def second(value) -> str | None:
    """Right-hand field of a glued cell ('10.84\\x1f-20.26%' -> '-20.26%')."""
    text = clean(value)
    if text is None:
        return None
    parts = text.split(SEP)
    if len(parts) < 2:
        # A few cells are space-separated rather than span-separated
        # ('Volatility' -> '6.71% 5.22%'), so fall back to whitespace.
        parts = text.split()
    return clean(parts[1]) if len(parts) > 1 else None


def num(value) -> float | None:
    """Parse a Finviz number: handles %, thousands commas and K/M/B/T suffixes.

    Reads only the first field of a glued cell, which is what every caller
    wants — use :func:`second` explicitly for the right-hand half.
    """
    text = first(value)
    if text is None:
        return None
    # A handful of cells pack two values with a plain space rather than sibling
    # spans ('Volatility' -> '6.71% 5.22%', 'Dividend Est.' -> '0.43 (5.03%)'),
    # so the leading token is the one that is actually being asked for.
    text = text.split()[0] if " " in text else text
    text = text.replace(",", "").replace("%", "").replace("$", "").strip()
    if not text or text in _MISSING:
        return None
    mult = 1.0
    if text[-1] in _SUFFIXES:
        mult = _SUFFIXES[text[-1]]
        text = text[:-1]
    try:
        return float(text) * mult
    except ValueError:
        return None


def pct(value) -> float | None:
    """Parse a percentage into a fraction: '33.93%' -> 0.3393."""
    raw = num(value)
    return None if raw is None else raw / 100.0


def pct2(value) -> float | None:
    """Parse the *second* field of a glued cell as a fraction.

    Used for '52W High' -> '10.84\\x1f-20.26%', where the useful number is the
    distance, not the price.
    """
    text = second(value)
    if text is None:
        return None
    return pct(text)


# --------------------------------------------------------------------------- #
# Fetch + parse
# --------------------------------------------------------------------------- #

def fetch_quote(symbol: str, timeout: float = 20.0) -> dict:
    """Fetch and parse one Finviz quote page.

    Raises :class:`NotFound` for an unknown symbol (Finviz answers 404, and also
    serves a 200 page with no snapshot table for some delisted names). Transport
    errors propagate so the caller can show a genuine error badge.
    """
    resp = requests.get(QUOTE_URL_TMPL.format(sym=symbol), headers=_HEADERS,
                        timeout=timeout)
    if resp.status_code == 404:
        raise NotFound(f"Finviz has no quote page for {symbol}")
    resp.raise_for_status()
    return parse_quote(resp.text, symbol)


def parse_quote(html: str, symbol: str) -> dict:
    """Pure parse of a Finviz quote page. Split out so tests can use a fixture."""
    soup = BeautifulSoup(html, "html.parser")

    # Finviz splits the snapshot across SIX `table.snapshot-table2` elements.
    # `select` (plural) walks all of them for ~168 cells / ~83 pairs;
    # `select_one` would silently return the first table only — 14 pairs, with
    # every short-interest and ownership field missing and no error raised.
    cells = [td.get_text(separator=SEP, strip=True)
             for td in soup.select("table.snapshot-table2 td")]
    if not cells:
        raise NotFound(f"Finviz returned no snapshot table for {symbol}")
    if len(cells) < MIN_CELLS:
        raise ParseChanged(
            f"Finviz snapshot for {symbol} had {len(cells) // 2} fields, "
            f"expected ~{EXPECTED_PAIRS} — the page layout has changed.")

    snapshot = dict(zip(cells[0::2], cells[1::2]))

    return {
        "symbol": symbol,
        "company": _company_name(soup, symbol),
        "snapshot": snapshot,
        "news": _news_rows(soup),
    }


def _company_name(soup: BeautifulSoup, symbol: str) -> str | None:
    """Company display name, used to build a better news query than the ticker.

    The <title> is the most stable carrier: "WEN - Wendy's Co Stock Price and
    Quote". Falls back to None rather than guessing.
    """
    if not soup.title:
        return None
    title = soup.title.get_text(strip=True)
    if " - " not in title:
        return None
    name = title.split(" - ", 1)[1]
    for tail in (" Stock Price and Quote", " Stock Quote", " Stock Price"):
        if name.endswith(tail):
            name = name[: -len(tail)]
    name = name.strip()
    return name or None


def _news_rows(soup: BeautifulSoup, today: date | None = None) -> list[dict]:
    """The quote page's news table: 100 rows of {title, link, source, published}.

    Finviz prints a full 'Aug-14-26 03:15PM' stamp only on the first row of each
    day; later rows that day carry the time alone. The date is carried forward
    across rows, which is why this is a loop with state rather than a mapping.
    """
    today = today or date.today()
    out: list[dict] = []
    current_date: date | None = None

    for row in soup.select("#news-table tr"):
        anchor = row.find("a", class_="tab-link-news") or row.find("a")
        if anchor is None:
            continue
        tds = row.find_all("td")
        if not tds:
            continue

        stamp = tds[0].get_text(strip=True)
        published, current_date = _news_timestamp(stamp, current_date, today)

        span = row.find("span")
        source = span.get_text(strip=True).strip("()") if span else None

        title = anchor.get_text(strip=True)
        link = anchor.get("href")
        if not title or not link:
            continue

        out.append({
            "title": title,
            "link": link,
            "source": source or "Finviz",
            "published": published,
        })

    return out


def _news_timestamp(stamp: str, current_date: date | None, today: date):
    """Resolve one news stamp to (epoch_seconds | None, date_to_carry_forward)."""
    match = _NEWS_DATE_RE.match(stamp.replace("\xa0", " ").strip())
    if not match:
        return None, current_date

    day_part, time_part = match.groups()
    if day_part:
        try:
            current_date = datetime.strptime(day_part, "%b-%d-%y").date()
        except ValueError:
            return None, current_date
    if current_date is None:
        current_date = today

    try:
        clock = datetime.strptime(time_part, "%I:%M%p").time()
    except ValueError:
        return None, current_date

    return datetime.combine(current_date, clock).timestamp(), current_date
