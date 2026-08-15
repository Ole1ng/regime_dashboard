"""Retail chatter sentiment for one ticker: StockTwits, with Reddit as a bonus.

StockTwits is the load-bearing source and is unusually good for this purpose:
its API is key-free, and a large share of messages carry a **self-declared
Bullish/Bearish tag** in ``entities.sentiment.basic``. That is a stated position,
not an inferred one, which makes it far better evidence than any classifier
applied to the same text.

Messages without a tag are scored with VADER plus the WSB slang lexicon
(``_sentiment_util.SOCIAL``), because plain VADER reads "moon", "tendies",
"bagholder" and "rekt" as exactly neutral. The two are then blended, with the
declared tags weighted more heavily than the inferred scores.

Reddit is deliberately best-effort. Its search RSS rate-limits aggressively
(HTTP 429 within a few requests) and returns thin results even when it works, so
every failure is swallowed and the panel never depends on it.

**The metric that actually matters here is velocity, not level.** A 70% bull
ratio is close to StockTwits' resting state — the platform is structurally
bullish. A tenfold jump in message count against this ticker's own 30-day
baseline is the signal, and that requires the stored history table, so it stays
empty until a few days of snapshots exist.
"""

from __future__ import annotations

import html
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone

import requests

from . import _sentiment_util as senti

STOCKTWITS_URL = "https://api.stocktwits.com/api/2/streams/symbol/{sym}.json"
REDDIT_URL = ("https://www.reddit.com/r/{sub}/search.rss"
              "?q={sym}&restrict_sr=1&sort=new&t=week")
REDDIT_SUBS = ("wallstreetbets", "stocks")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

MAX_PAGES = 3
PAGE_TIMEOUT = 10.0
REDDIT_TIMEOUT = 8.0

# Weight given to declared tags relative to inferred VADER scores when blending.
TAG_WEIGHT = 0.7

# Message counts below which the reading is too thin to trust.
THIN_MESSAGES = 20

# Velocity bands: today's message count over the stored median.
VELOCITY_SPIKE = 3.0
VELOCITY_ELEVATED = 1.5
VELOCITY_QUIET = 0.4
MIN_HISTORY_DAYS = 5

BULL_CROWDED = 0.75
BEAR_CROWDED = 0.40


def refresh(symbol: str, history: list[dict] | None = None) -> dict:
    """Panel entry point. Never raises on Reddit; StockTwits failure propagates."""
    stream, partial = _stocktwits(symbol)
    reddit = _reddit(symbol)
    return compute(stream, symbol, reddit=reddit, partial=partial,
                   history=history)


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #

def _stocktwits(symbol: str) -> tuple[list[dict], bool]:
    """Up to MAX_PAGES of the symbol stream, oldest cursor walked backwards.

    Returns (messages, partial). ``partial`` means at least one page after the
    first failed — the data is still usable, just shorter, so it is reported
    rather than raised.
    """
    messages: list[dict] = []
    max_id = None
    partial = False

    for page in range(MAX_PAGES):
        url = STOCKTWITS_URL.format(sym=symbol)
        if max_id:
            url += f"?max={max_id}"
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=PAGE_TIMEOUT)
        except Exception:
            partial = page > 0
            if page == 0:
                raise
            break

        if resp.status_code == 404:
            # No such symbol on StockTwits — an empty panel, not an error.
            return [], False
        if resp.status_code != 200:
            if page == 0:
                resp.raise_for_status()
            partial = True
            break

        body = resp.json()
        batch = body.get("messages") or []
        messages.extend(batch)

        cursor = body.get("cursor") or {}
        if not cursor.get("more") or not cursor.get("max"):
            break
        max_id = cursor["max"]

    return messages, partial


def _reddit(symbol: str) -> dict:
    """Best-effort Reddit search. Any failure returns unavailable, never raises."""
    try:
        import feedparser
    except Exception:
        return {"available": False, "reason": "feedparser missing", "items": []}

    items = []
    for sub in REDDIT_SUBS:
        try:
            url = REDDIT_URL.format(sub=sub, sym=symbol)
            resp = requests.get(url, headers=_HEADERS, timeout=REDDIT_TIMEOUT)
            if resp.status_code != 200:
                continue
            for entry in feedparser.parse(resp.content).entries:
                items.append({
                    "title": entry.get("title", ""),
                    "link": entry.get("link", ""),
                    "sub": sub,
                    "published": entry.get("updated", ""),
                })
        except Exception:
            continue

    if not items:
        return {"available": False,
                "reason": "no results or rate limited", "items": []}

    titles = [i["title"] for i in items]
    agg = senti.sentiment(titles, senti.SOCIAL)
    return {"available": True, "n": len(items), "mean": agg["mean"],
            "tone": agg["tone"], "items": items[:8]}


# --------------------------------------------------------------------------- #
# Compute
# --------------------------------------------------------------------------- #

def compute(messages: list[dict], symbol: str, reddit: dict | None = None,
            partial: bool = False, history: list[dict] | None = None,
            now: datetime | None = None) -> dict:
    """Pure: raw StockTwits messages -> panel payload."""
    now = now or datetime.now(timezone.utc)

    bullish = bearish = 0
    untagged_scores: list[float] = []
    bodies: list[str] = []
    users: set = set()
    per_day: Counter = Counter()
    scored: list[tuple[float, dict]] = []

    for msg in messages:
        # StockTwits serves bodies with HTML entities intact ("Wendy&#39;s"),
        # which otherwise reach the tokeniser as junk terms like "wendy&".
        body = html.unescape(msg.get("body") or "")
        bodies.append(body)

        user = (msg.get("user") or {}).get("username")
        if user:
            users.add(user)

        created = _parse_time(msg.get("created_at"))
        if created:
            per_day[created.date().isoformat()] += 1

        tag = ((msg.get("entities") or {}).get("sentiment") or {}).get("basic")
        if tag == "Bullish":
            bullish += 1
            value = 1.0
        elif tag == "Bearish":
            bearish += 1
            value = -1.0
        else:
            value = senti.score(body, senti.SOCIAL)
            untagged_scores.append(value)

        scored.append((value, {
            "body": body[:220],
            "sentiment": tag,
            "score": round(value, 3),
            "created_at": msg.get("created_at"),
            "user": user,
        }))

    n = len(messages)
    tagged = bullish + bearish
    bull_pct = (bullish / tagged) if tagged else None
    untagged_mean = (sum(untagged_scores) / len(untagged_scores)
                     if untagged_scores else None)

    payload = {
        "symbol": symbol,
        "n": n,
        "empty": n == 0,
        "partial": partial,
        "bullish": bullish,
        "bearish": bearish,
        "untagged": len(untagged_scores),
        "tagged": tagged,
        "bull_pct": round(bull_pct, 3) if bull_pct is not None else None,
        "untagged_mean": round(untagged_mean, 3) if untagged_mean is not None else None,
        "unique_users": len(users),
        "per_day": [{"date": d, "n": c} for d, c in sorted(per_day.items())],
        "blended": _blend(bull_pct, tagged, untagged_mean, len(untagged_scores)),
        "top_terms": senti.top_terms(bodies, extra_stop={symbol.lower()}),
        "co_mentions": senti.cashtags(bodies, exclude=symbol),
        "top": [m for _, m in sorted(scored, key=lambda p: -abs(p[0]))[:6]],
        "reddit": reddit or {"available": False, "reason": "not fetched", "items": []},
        "thin": n < THIN_MESSAGES,
    }

    payload.update(_velocity(payload, history, now))
    payload["tone"] = _tone(payload["blended"])
    payload["commentary"] = _commentary(payload)
    return payload


def _blend(bull_pct: float | None, tagged: int,
           untagged_mean: float | None, untagged_n: int) -> float | None:
    """Combine declared tags and inferred scores into one -1..+1 reading.

    Declared tags carry more weight than inferred ones (TAG_WEIGHT), and each
    side is weighted by how many messages it rests on, so a handful of tags does
    not outvote fifty scored bodies.
    """
    tag_score = (bull_pct * 2 - 1) if bull_pct is not None else None

    if tag_score is None and untagged_mean is None:
        return None
    if tag_score is None:
        return round(untagged_mean, 3)
    if untagged_mean is None:
        return round(tag_score, 3)

    w_tag = TAG_WEIGHT * tagged
    w_inf = (1 - TAG_WEIGHT) * untagged_n
    if w_tag + w_inf == 0:
        return None
    return round((tag_score * w_tag + untagged_mean * w_inf) / (w_tag + w_inf), 3)


def _parse_time(raw) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _velocity(p: dict, history: list[dict] | None, now: datetime) -> dict:
    """Message count against this ticker's own stored baseline.

    Uses the median rather than the mean: chatter counts are heavily skewed by
    the occasional viral day, and a mean baseline would make every ordinary day
    look quiet afterwards.
    """
    rows = [r for r in (history or []) if r.get("st_msgs") is not None]
    out = {"velocity_ratio": None, "velocity_state": "unknown",
           "velocity_baseline": None, "velocity_days": len(rows),
           "velocity_needed": MIN_HISTORY_DAYS}

    if len(rows) < MIN_HISTORY_DAYS or not p["n"]:
        return out

    baseline = statistics.median(float(r["st_msgs"]) for r in rows)
    if baseline <= 0:
        return out

    ratio = p["n"] / baseline
    out["velocity_baseline"] = round(baseline, 1)
    out["velocity_ratio"] = round(ratio, 2)
    if ratio >= VELOCITY_SPIKE:
        out["velocity_state"] = "spike"
    elif ratio >= VELOCITY_ELEVATED:
        out["velocity_state"] = "elevated"
    elif ratio <= VELOCITY_QUIET:
        out["velocity_state"] = "quiet"
    else:
        out["velocity_state"] = "normal"
    return out


def _tone(blended: float | None) -> str:
    if blended is None:
        return "No data"
    if blended >= 0.15:
        return "Bullish"
    if blended <= -0.15:
        return "Bearish"
    return "Mixed"


# --------------------------------------------------------------------------- #
# Commentary
# --------------------------------------------------------------------------- #

def _commentary(p: dict) -> dict:
    sentences: list[str] = []
    warnings: list[str] = []
    sym = p["symbol"]

    if p["empty"]:
        return {"headline": f"No retail chatter found for {sym}",
                "warnings": ["⚠ StockTwits returned no messages — normal for "
                             "many small caps."],
                "sentences": []}

    if p["bull_pct"] is not None:
        sentences.append(
            f"{p['n']} StockTwits messages from {p['unique_users']} accounts. "
            f"Of the {p['tagged']} that declare a position, {p['bull_pct']:.0%} "
            f"are tagged bullish ({p['bullish']} vs {p['bearish']} bearish); "
            f"the other {p['untagged']} score "
            f"{p['untagged_mean']:+.2f} on average once WSB slang is included."
            if p["untagged_mean"] is not None else
            f"{p['n']} StockTwits messages from {p['unique_users']} accounts, "
            f"{p['bull_pct']:.0%} of declared positions bullish.")
    else:
        sentences.append(
            f"{p['n']} StockTwits messages from {p['unique_users']} accounts, "
            f"none carrying a declared position; inferred tone is "
            f"{p['tone'].lower()}.")

    # Retail is structurally bullish, so only an extreme is worth remarking on.
    if p["bull_pct"] is not None and p["bull_pct"] >= BULL_CROWDED:
        sentences.append(
            f"At {p['bull_pct']:.0%} bullish this is a crowded long among "
            f"retail. StockTwits skews bullish at baseline, so read this as "
            f"positioning rather than as insight.")
    elif p["bull_pct"] is not None and p["bull_pct"] <= BEAR_CROWDED:
        sentences.append(
            f"Only {p['bull_pct']:.0%} of declared positions are bullish — "
            f"unusual on a platform that leans long by default.")

    state = p["velocity_state"]
    if state in ("spike", "elevated"):
        sentences.append(
            f"Message volume is {p['velocity_ratio']:.1f}x its 30-day median "
            f"({p['velocity_baseline']:.0f}/day) — attention is "
            f"{'spiking' if state == 'spike' else 'building'}.")
    elif state == "quiet":
        sentences.append(
            f"Chatter is {p['velocity_ratio']:.1f}x the median — quieter than "
            f"usual, so this reading rests on few voices.")

    if p["co_mentions"]:
        pairs = ", ".join(c["symbol"] for c in p["co_mentions"][:5])
        sentences.append(f"Frequently mentioned alongside: {pairs}.")

    reddit = p["reddit"]
    if reddit.get("available"):
        plural = "post" if reddit["n"] == 1 else "posts"
        sentences.append(
            f"Reddit adds {reddit['n']} recent {plural}, tone "
            f"{reddit['tone'].lower()} ({reddit['mean']:+.2f}).")

    if p["thin"]:
        warnings.append(f"⚠ Only {p['n']} messages — too thin to lean on.")
    if p["partial"]:
        warnings.append("⚠ StockTwits paging stopped early; this is a partial sample.")
    if p["velocity_state"] == "unknown" and p["velocity_days"] < MIN_HISTORY_DAYS:
        warnings.append(f"⚠ Velocity needs {p['velocity_needed']} daily snapshots; "
                        f"{p['velocity_days']} stored so far.")

    return {"headline": _headline(p), "warnings": warnings, "sentences": sentences}


def _headline(p: dict) -> str:
    bits = [f"{p['tone']} retail chatter"]
    if p["bull_pct"] is not None:
        bits.append(f"{p['bull_pct']:.0%} bullish of {p['tagged']} declared")
    bits.append(f"{p['n']} messages")
    if p["velocity_state"] in ("spike", "elevated"):
        bits.append(f"{p['velocity_ratio']:.1f}x normal volume")
    return " — ".join(bits[:2]) + f" ({', '.join(bits[2:])})"
