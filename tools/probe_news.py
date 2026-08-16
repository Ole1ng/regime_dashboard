"""Verify every Tab 3 news feed is alive, parsing, and actually recent.

    python tools/probe_news.py            # all feeds
    python tools/probe_news.py --wires    # just the Google News queries

Run this first whenever a screener panel goes quiet. It answers the three
questions that matter, in order:

1. **Does the URL still return a feed?** Publishers retire RSS without notice —
   Reuters, Kitco and the BLS all did between this project's two verification
   dates.
2. **Does it parse to entries?** Several dead feeds return HTTP 200 with an HTML
   error page, which ``feedparser`` accepts and yields zero entries for.
3. **Is any of it recent?** The trap this tab was built around: Google honours
   ``when:`` on a bare ``site:`` query but ignores it on a keyword query, where
   it will happily return an eleven-year-old article. A feed that is alive,
   parsing and entirely stale contributes nothing, because news_screener applies
   a hard 48-hour window — so the ``<48h`` column, not the entry count, is the
   number that decides whether a feed is pulling its weight.

Exits non-zero if any feed returns no entries at all.
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover - non-standard stdout
    pass

from panels import _feeds  # noqa: E402


def _probe(name: str, url: str) -> dict:
    started = time.time()
    try:
        entries = _feeds.parse_feed(url)
    except Exception as exc:
        return {"name": name, "url": url, "error": f"{type(exc).__name__}",
                "n": 0, "fresh": 0, "median_h": None,
                "secs": time.time() - started, "first": ""}

    now = time.time()
    ages = []
    for entry in entries:
        stamp = _feeds.entry_time(entry)
        if stamp:
            ages.append((now - stamp) / 3600)
    ages.sort()

    return {
        "name": name,
        "url": url,
        "error": None,
        "n": len(entries),
        "dated": len(ages),
        "fresh": sum(1 for a in ages if a <= _feeds_window()),
        # Newest and median together separate the two ways a feed goes quiet.
        # A low-frequency primary source (the Fed between meetings) has a recent
        # newest and an old median; a frozen archive has both old, and that is
        # the failure worth catching — it serves year-old headlines that read
        # as current.
        "newest_h": ages[0] if ages else None,
        "median_h": ages[len(ages) // 2] if ages else None,
        "secs": time.time() - started,
        "first": _feeds.strip_google_suffix(
            (entries[0].get("title") or "") if entries else ""),
    }


def _feeds_window() -> float:
    from panels import news_screener
    return float(news_screener.WINDOW_HOURS)


def main() -> int:
    wires_only = "--wires" in sys.argv[1:]

    jobs: list[tuple[str, str]] = []
    for panel in _feeds.PANEL_KEYS:
        jobs.append((f"query/{_feeds.PANEL_TITLES[panel]}",
                     _feeds.panel_query_url(panel)))
    for domain in _feeds.WIRE_SITES:
        jobs.append((f"wire/{domain}", _feeds.wire_query_url(domain)))
    if not wires_only:
        for feed in _feeds.PUBLISHER_FEEDS:
            jobs.append((f"{feed.publisher}/{_feeds._short_url(feed.url)}",
                         feed.url))

    window = _feeds_window()
    print(f"Probing {len(jobs)} feeds (freshness window {window:.0f}h)\n")

    with ThreadPoolExecutor(max_workers=_feeds.MAX_WORKERS) as pool:
        results = list(pool.map(lambda j: _probe(*j), jobs))

    # A feed whose newest item is older than this is not quiet, it is frozen.
    FROZEN_H = 30 * 24

    print(f"{'feed':<40} {'n':>4} {'<' + str(int(window)) + 'h':>6} "
          f"{'newest':>7} {'med':>8} {'s':>5}  first title")
    print("-" * 118)

    dead, stale, frozen = [], [], []
    for r in sorted(results, key=lambda r: (r["n"] == 0, -r["fresh"])):
        if r["error"]:
            print(f"{r['name']:<40} {'ERR':>4} {'':>6} {'':>7} {'':>8} "
                  f"{r['secs']:>5.1f}  {r['error']}")
            dead.append(r["name"])
            continue
        newest = f"{r['newest_h']:.0f}h" if r["newest_h"] is not None else "—"
        median = f"{r['median_h']:.0f}h" if r["median_h"] is not None else "—"
        print(f"{r['name']:<40} {r['n']:>4} {r['fresh']:>6} {newest:>7} "
              f"{median:>8} {r['secs']:>5.1f}  {r['first'][:40]}")
        if r["n"] == 0:
            dead.append(r["name"])
        elif r["newest_h"] is None or r["newest_h"] > FROZEN_H:
            frozen.append(r["name"])
        elif r["fresh"] == 0:
            stale.append(r["name"])

    total_fresh = sum(r["fresh"] for r in results)
    print(f"\n{total_fresh} entries inside the {window:.0f}h window across "
          f"{len(results)} feeds.")

    if stale:
        print(f"\n{len(stale)} feed(s) quiet — newest item outside the "
              f"{window:.0f}h window but recent enough to be alive:")
        for name in stale:
            print(f"  quiet  {name}")
        print("  Normal for a low-frequency primary source (the Fed between "
              "meetings) and over a weekend.")

    if frozen:
        print(f"\n{len(frozen)} FROZEN feed(s) — parsing fine, newest item "
              f"over {FROZEN_H // 24} days old:")
        for name in frozen:
            print(f"  FROZEN {name}")
        print("  These are the dangerous ones: they serve stale headlines that "
              "read as current.\n  The recency window discards them, so they "
              "cost requests and contribute nothing. Remove them.")

    if dead:
        print(f"\n{len(dead)} DEAD feed(s) — no entries at all:")
        for name in dead:
            print(f"  FAIL   {name}")
        print("\nUpdate panels/_feeds.py and DATA_SOURCES.md §12 before "
              "re-running.")
        return 1

    print("\nOK — every feed returned entries.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
