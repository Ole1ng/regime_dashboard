"""Event risk for one ticker: earnings, the implied move, and SEC filings.

Sentiment is only half a picture without knowing what is about to reprice it.
This panel supplies the other half from three free sources:

  * **Earnings date** — from the Finviz snapshot ("Aug 26 AMC"), which carries
    no year, cross-checked against yfinance's calendar where available.
  * **Implied move** — the ATM straddle for the first expiry that covers the
    earnings date, taken from the CBOE chain already in hand, expressed as a
    percentage of spot. Compared against how far the stock has *actually*
    moved after its last several reports, which is the comparison that says
    whether event vol is rich or cheap.
  * **SEC EDGAR filings** — classified by what they mean rather than listed
    raw. A cluster of Form 4s, a 13D, or an S-3 shelf are three completely
    different signals, and the dilution case in particular is worth flagging
    loudly for a small cap with retail enthusiasm behind it.

EDGAR requires a declared User-Agent carrying a contact address; sending a
browser UA is against its published policy and gets blocked. That address is
read from the ``SEC_USER_AGENT`` environment variable rather than hardcoded, so
the repository carries no contact details of its own and every user declares
their own traffic — see ``.env.example``. Leaving it unset is supported: the
filings block reports itself unavailable and the rest of the panel still works.
The ticker→CIK map is a ~1 MB file covering 10,396 symbols, so it is cached
rather than refetched.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone

import numpy as np
import requests

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"

SEC_UA_ENV = "SEC_USER_AGENT"

SEC_TIMEOUT = 15.0
MAX_FILINGS = 20
EARNINGS_SOON_DAYS = 7
HIST_QUARTERS = 8

# Implied move against the historical average absolute move.
MOVE_RICH = 1.35
MOVE_CHEAP = 0.75

_OCC_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")
_EARNINGS_RE = re.compile(r"^([A-Z][a-z]{2})\s+(\d{1,2})\s*(BMO|AMC|--)?", re.I)

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# How each EDGAR form type is read. Order matters — first match wins.
#
# "shelf" and "offering" are deliberately separate. An S-3ASR is an *automatic*
# shelf, available only to well-known seasoned issuers, and for a large cap it
# is routine financing plumbing that says nothing about dilution intent. A 424B
# is a live prospectus supplement for an offering actually being priced. Lumping
# them together labels every mega-cap a dilution risk — NVIDIA files both.
_FORM_KINDS = [
    ("offering", ("424B",)),
    ("shelf", ("S-1", "S-3", "F-1", "F-3")),
    ("insider", ("3", "4", "5")),
    ("activist", ("SC 13D", "SCHEDULE 13D")),
    ("ownership", ("SC 13G", "SCHEDULE 13G")),
    ("periodic", ("10-K", "10-Q", "20-F", "40-F", "ARS")),
    ("proxy", ("DEF 14A", "DEFA14A", "PRE 14A")),
    ("comp", ("S-8",)),
    ("event", ("8-K", "6-K")),
]

# Below this market cap a shelf or offering is a genuine overhang worth a
# warning; above it, it is ordinary treasury activity and is merely noted.
DILUTION_CAP_LIMIT = 2e9

# A "historical average post-earnings move" over one or two prints is noise.
MIN_HIST_MOVES = 3

# 8-K item 2.02 is "Results of Operations and Financial Condition" — earnings.
_EARNINGS_ITEM = "2.02"


def refresh(symbol: str, chain_json: dict | None = None,
            snapshot: dict | None = None, prices=None,
            security_type: str | None = None) -> dict:
    """Panel entry point. Everything except EDGAR is passed in already-fetched."""
    filings = _filings(symbol, security_type)
    return compute(symbol, chain_json=chain_json, snapshot=snapshot,
                   prices=prices, filings=filings)


# --------------------------------------------------------------------------- #
# EDGAR
# --------------------------------------------------------------------------- #

class SECNotConfigured(RuntimeError):
    """No contact address declared, so EDGAR must not be called."""


def _sec_contact() -> str:
    return os.environ.get(SEC_UA_ENV, "").strip()


def _sec_headers() -> dict:
    """SEC policy requires a descriptive UA with a contact address.

    Read at call time rather than at import so that ``.env`` loading order never
    matters and tests can set the variable without reimporting the module.
    """
    contact = _sec_contact()
    if not contact:
        raise SECNotConfigured(
            f"{SEC_UA_ENV} is not set — see .env.example.")
    return {"User-Agent": contact, "Accept-Encoding": "gzip, deflate"}


_CIK_CACHE: dict[str, str] = {}

# Optional persistence hooks, injected by app.py at startup so this module has
# no dependency on the store. Left as None the map is still cached in-process,
# it just does not survive a restart.
CACHE_GET = None   # (key, max_age) -> value | None
CACHE_PUT = None   # (key, value) -> None
_CIK_CACHE_KEY = "sec_cik_map"
_CIK_CACHE_TTL = 7 * 24 * 3600  # the map changes on the order of weekly


def _cik_map(force: bool = False) -> dict[str, str]:
    """Ticker -> zero-padded CIK. ~10.4k entries, ~1 MB, so worth caching."""
    global _CIK_CACHE
    if _CIK_CACHE and not force:
        return _CIK_CACHE

    if CACHE_GET and not force:
        cached = CACHE_GET(_CIK_CACHE_KEY, _CIK_CACHE_TTL)
        if cached:
            _CIK_CACHE = cached
            return _CIK_CACHE

    resp = requests.get(SEC_TICKERS_URL, headers=_sec_headers(),
                        timeout=SEC_TIMEOUT)
    resp.raise_for_status()
    _CIK_CACHE = {row["ticker"].upper(): str(row["cik_str"]).zfill(10)
                  for row in resp.json().values()}
    if CACHE_PUT:
        try:
            CACHE_PUT(_CIK_CACHE_KEY, _CIK_CACHE)
        except Exception:
            pass  # caching is an optimisation, never a failure mode
    return _CIK_CACHE


def _filings(symbol: str, security_type: str | None) -> dict:
    """Recent EDGAR filings, classified. Never raises — this panel degrades."""
    # ETFs and indices have no meaningful issuer filings; the CIK lookup would
    # either miss or return the sponsor's paperwork, which is noise.
    if security_type and security_type.lower() not in ("stock", "equity", ""):
        return {"available": False, "reason": f"{security_type} — no issuer filings",
                "recent": []}
    # Checked here as well as in _sec_headers so the panel reports something
    # actionable rather than the bare exception name the handler below produces.
    if not _sec_contact():
        return {"available": False,
                "reason": f"{SEC_UA_ENV} not set — see .env.example",
                "recent": []}
    try:
        cik = _cik_map().get(symbol.upper())
        if not cik:
            return {"available": False, "reason": "no CIK on file", "recent": []}

        resp = requests.get(SEC_SUBMISSIONS_URL.format(cik=cik),
                            headers=_sec_headers(), timeout=SEC_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}", "recent": []}

    return _classify_filings(data, cik)


def _classify_filings(data: dict, cik: str, today: date | None = None) -> dict:
    today = today or date.today()
    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []

    rows = []
    for i, form in enumerate(forms):
        filed = _iso_date(_at(recent, "filingDate", i))
        if filed is None:
            continue
        items = _at(recent, "items", i) or ""
        kind = _form_kind(form, items)
        rows.append({
            "form": form,
            "date": filed.isoformat(),
            "age_days": (today - filed).days,
            "kind": kind,
            "items": items,
            "description": _at(recent, "primaryDocDescription", i) or "",
            "url": _filing_url(cik, _at(recent, "accessionNumber", i),
                               _at(recent, "primaryDocument", i)),
        })

    counts = _filing_counts(rows)
    return {
        "available": True,
        "cik": cik,
        "company": data.get("name"),
        "sic": data.get("sicDescription"),
        "exchanges": data.get("exchanges") or [],
        # `recent` is the display slice; the counts and the earnings history
        # below are computed over every filing returned. Scanning only the
        # slice would miss almost everything on an active filer — WEN's most
        # recent 20 filings are all Form 4s, hiding all 35 earnings 8-Ks.
        "recent": rows[:MAX_FILINGS],
        "total_scanned": len(rows),
        "earnings_dates": [r["date"] for r in rows if r["kind"] == "earnings"],
        "counts": counts,
        "dilution_flag": counts["offering_90d"] > 0 or counts["shelf_90d"] > 0,
        "offering_flag": counts["offering_90d"] > 0,
        "activist_flag": counts["activist_180d"] > 0,
        "insider_cluster": counts["insider_30d"] >= 5,
    }


def _at(recent: dict, key: str, i: int):
    seq = recent.get(key) or []
    return seq[i] if i < len(seq) else None


def _form_kind(form: str, items: str) -> str:
    upper = (form or "").upper().strip()
    if upper.startswith("8-K") and _EARNINGS_ITEM in (items or ""):
        return "earnings"
    for kind, prefixes in _FORM_KINDS:
        for prefix in prefixes:
            # Exact match for the bare numeric ownership forms ("4", "3", "5")
            # so "424B5" is never mistaken for a Form 4.
            if prefix.isdigit():
                if upper == prefix or upper.startswith(prefix + "/"):
                    return kind
            elif upper.startswith(prefix):
                return kind
    return "other"


def _filing_counts(rows: list[dict]) -> dict:
    def count(kind: str, days: int) -> int:
        return sum(1 for r in rows if r["kind"] == kind and r["age_days"] <= days)

    return {
        "insider_30d": count("insider", 30),
        "insider_90d": count("insider", 90),
        "event_30d": count("event", 30) + count("earnings", 30),
        "offering_90d": count("offering", 90),
        "shelf_90d": count("shelf", 90),
        "activist_180d": count("activist", 180),
        "ownership_180d": count("ownership", 180),
    }


def _filing_url(cik: str, accession: str | None, doc: str | None) -> str | None:
    if not accession:
        return None
    return SEC_ARCHIVE_URL.format(cik=int(cik), acc=accession.replace("-", ""),
                                  doc=doc or "")


def _iso_date(raw) -> date | None:
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Earnings + implied move
# --------------------------------------------------------------------------- #

def _earnings_date(snapshot: dict | None, today: date) -> tuple[date | None, str | None, str]:
    """Parse Finviz's year-less 'Aug 26 AMC' into a real date.

    Finviz shows the *nearest* report, which may be days behind or ahead, and
    omits the year entirely. The year is inferred by choosing whichever of
    last/this/next year lands closest to today, which is correct across a
    December-January boundary where naive year-stamping fails.
    """
    raw = (snapshot or {}).get("earnings_raw")
    if not raw:
        return None, None, "none"

    match = _EARNINGS_RE.match(raw.strip())
    if not match:
        return None, None, "unparsed"

    month = _MONTHS.get(match.group(1)[:3].lower())
    day = int(match.group(2))
    when = (match.group(3) or "").upper()
    when = when if when in ("BMO", "AMC") else None
    if not month:
        return None, when, "unparsed"

    best = None
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if best is None or abs((candidate - today).days) < abs((best - today).days):
            best = candidate
    return best, when, "finviz"


def _implied_move(chain_json: dict | None, symbol: str, spot: float | None,
                  earnings: date | None, today: date) -> dict:
    """ATM straddle price / spot for the first expiry covering the earnings date."""
    out = {"implied_move": None, "implied_expiry": None, "implied_dte": None,
           "straddle": None, "atm_strike": None, "covers_earnings": None}
    data = (chain_json or {}).get("data") or {}
    options = data.get("options") or []
    if not options or not spot:
        return out

    rows = []
    for opt in options:
        match = _OCC_RE.match(opt.get("option") or "")
        if not match or match.group(1) != symbol:
            continue
        try:
            expiry = datetime.strptime(match.group(2), "%y%m%d").date()
        except ValueError:
            continue
        if expiry < today:
            continue
        bid, ask = float(opt.get("bid") or 0), float(opt.get("ask") or 0)
        mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else float(
            opt.get("last_trade_price") or 0)
        rows.append({"expiry": expiry, "is_call": match.group(3) == "C",
                     "strike": int(match.group(4)) / 1000.0, "mid": mid})
    if not rows:
        return out

    expiries = sorted({r["expiry"] for r in rows})
    # The straddle only prices the event if it expires after it.
    target = next((e for e in expiries if earnings and e >= earnings), None)
    covers = target is not None
    if target is None:
        target = expiries[0]

    same = [r for r in rows if r["expiry"] == target and r["mid"] > 0]
    if not same:
        return out

    nearest = min(abs(r["strike"] - spot) for r in same)
    at_strike = [r for r in same if abs(r["strike"] - spot) <= nearest + 1e-9]
    call = next((r["mid"] for r in at_strike if r["is_call"]), None)
    put = next((r["mid"] for r in at_strike if not r["is_call"]), None)
    if call is None or put is None:
        return out

    straddle = call + put
    return {"implied_move": round(straddle / spot, 4),
            "implied_expiry": target.isoformat(),
            "implied_dte": (target - today).days,
            "straddle": round(straddle, 3),
            "atm_strike": at_strike[0]["strike"],
            "covers_earnings": covers}


def _historical_moves(prices, filings: dict, today: date) -> dict:
    """Absolute next-day move after each of the last several 8-K earnings filings.

    EDGAR's earnings 8-K (item 2.02) is a better anchor than a vendor's earnings
    calendar: it is the actual timestamped disclosure, and it is already fetched.
    """
    out = {"n": 0, "mean_abs": None, "median_abs": None, "max_abs": None,
           "moves": []}
    if prices is None or len(prices) < 30 or not filings.get("available"):
        return out

    dates = [_iso_date(d) for d in filings.get("earnings_dates") or []]
    dates = [d for d in dates if d][:HIST_QUARTERS]
    if not dates:
        return out

    try:
        closes = prices["Close"].dropna()
        index = [ts.date() for ts in closes.index]
    except Exception:
        return out

    moves = []
    for report in dates:
        # Close before the filing to the close after it.
        before = [i for i, d in enumerate(index) if d <= report]
        after = [i for i, d in enumerate(index) if d > report]
        if not before or not after:
            continue
        prev_close = float(closes.iloc[before[-1]])
        next_close = float(closes.iloc[after[0]])
        if prev_close <= 0:
            continue
        moves.append({"date": report.isoformat(),
                      "move": round(next_close / prev_close - 1.0, 4)})

    if not moves:
        return out

    absolute = [abs(m["move"]) for m in moves]
    ups = sum(1 for m in moves if m["move"] > 0)
    return {"n": len(moves),
            "mean_abs": round(float(np.mean(absolute)), 4),
            "median_abs": round(float(np.median(absolute)), 4),
            "max_abs": round(float(np.max(absolute)), 4),
            "up": ups, "down": len(moves) - ups,
            # A straddle is direction-blind, but a name that has fallen after
            # every recent report is telling you something the straddle is not.
            "lopsided": len(moves) >= 3 and (ups == 0 or ups == len(moves)),
            "moves": moves}


# --------------------------------------------------------------------------- #
# Compute
# --------------------------------------------------------------------------- #

def compute(symbol: str, chain_json: dict | None = None,
            snapshot: dict | None = None, prices=None,
            filings: dict | None = None, now: datetime | None = None) -> dict:
    """Pure: pre-fetched inputs -> panel payload."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    filings = filings or {"available": False, "reason": "not fetched", "recent": []}

    spot = ((chain_json or {}).get("data") or {}).get("current_price")
    spot = float(spot) if spot else (snapshot or {}).get("spot")

    earnings, when, source = _earnings_date(snapshot, today)
    days_out = (earnings - today).days if earnings else None

    implied = _implied_move(chain_json, symbol, spot, earnings, today)
    historical = _historical_moves(prices, filings, today)

    # Only compare against the historical average once there are enough prints
    # for "average" to mean anything — one prior move is an anecdote.
    ratio = None
    state = "unknown"
    if (implied["implied_move"] and historical["mean_abs"]
            and historical["n"] >= MIN_HIST_MOVES):
        ratio = implied["implied_move"] / historical["mean_abs"]
        if ratio >= MOVE_RICH:
            state = "rich"
        elif ratio <= MOVE_CHEAP:
            state = "cheap"
        else:
            state = "fair"

    payload = {
        "symbol": symbol,
        "spot": spot,
        "earnings_date": earnings.isoformat() if earnings else None,
        "earnings_when": when,
        "earnings_source": source,
        "earnings_days_out": days_out,
        "earnings_upcoming": days_out is not None and days_out >= 0,
        "earnings_soon": days_out is not None and 0 <= days_out <= EARNINGS_SOON_DAYS,
        **implied,
        "historical_moves": historical,
        "move_ratio": round(ratio, 2) if ratio else None,
        "move_state": state,
        "filings": filings,
        "market_cap": (snapshot or {}).get("market_cap"),
        "ex_dividend": (snapshot or {}).get("ex_dividend"),
    }
    payload["commentary"] = _commentary(payload)
    return payload


# --------------------------------------------------------------------------- #
# Commentary
# --------------------------------------------------------------------------- #

def _commentary(p: dict) -> dict:
    sentences: list[str] = []
    warnings: list[str] = []
    sym = p["symbol"]

    # --- earnings ----------------------------------------------------------- #
    days = p["earnings_days_out"]
    if p["earnings_date"] and days is not None:
        when = f" {p['earnings_when']}" if p["earnings_when"] else ""
        if days > 0:
            sentences.append(
                f"Earnings on {p['earnings_date']}{when} — {days} day"
                f"{'s' if days != 1 else ''} away.")
        elif days == 0:
            sentences.append(f"Earnings today{when}.")
        else:
            sentences.append(
                f"Last reported {abs(days)} days ago ({p['earnings_date']}); "
                f"no confirmed next date.")

    # --- implied move -------------------------------------------------------- #
    if p["implied_move"]:
        move = p["implied_move"]
        base = (f"The {p['implied_expiry']} straddle prices a "
                f"±{move:.1%} move")
        if not p["covers_earnings"]:
            sentences.append(base + " — but this expiry does not span the next "
                                    "report, so it is not an event premium.")
        elif p["move_state"] == "rich":
            sentences.append(
                base + f", {p['move_ratio']:.2f}x the "
                       f"{p['historical_moves']['mean_abs']:.1%} this name has "
                       f"actually averaged after its last "
                       f"{p['historical_moves']['n']} reports — event vol is "
                       f"expensive relative to its own history.")
        elif p["move_state"] == "cheap":
            sentences.append(
                base + f", only {p['move_ratio']:.2f}x the "
                       f"{p['historical_moves']['mean_abs']:.1%} historical "
                       f"average — the options are underpricing this event "
                       f"against its own record.")
        elif p["move_state"] == "fair":
            sentences.append(
                base + f", in line with the "
                       f"{p['historical_moves']['mean_abs']:.1%} historical average.")
        else:
            sentences.append(base + ".")

    hist = p["historical_moves"]
    if hist.get("lopsided"):
        direction = "higher" if hist["up"] else "lower"
        sentences.append(
            f"Every one of the last {hist['n']} reports moved the stock "
            f"{direction} — the straddle prices magnitude only, so that "
            f"one-sidedness is not in the implied move.")

    # --- filings -------------------------------------------------------------- #
    f = p["filings"]
    if f.get("available"):
        # Read defensively: a payload cached by an older build, or one built by
        # a caller that only filled part of the block, must not take the whole
        # panel down with a KeyError.
        counts = f.get("counts") or {}
        get = lambda k: counts.get(k, 0)
        bits = []
        if get("insider_30d"):
            bits.append(f"{get('insider_30d')} insider filings in 30 days")
        if get("event_30d"):
            bits.append(f"{get('event_30d')} 8-Ks in 30 days")
        if get("activist_180d"):
            n = get("activist_180d")
            bits.append(f"{n} activist (13D) filing{'s' if n != 1 else ''}")
        if get("ownership_180d"):
            bits.append(f"{get('ownership_180d')} 13G stake filings")
        if bits:
            sentences.append("EDGAR shows " + ", ".join(bits) + ".")

        # A shelf is routine plumbing for a large cap and an existential
        # overhang for a micro cap. The same filing therefore warrants a warning
        # in one case and a passing note in the other.
        n_dil = get("offering_90d") + get("shelf_90d")
        cap = p.get("market_cap")
        small = cap is not None and cap < DILUTION_CAP_LIMIT
        if n_dil and (small or cap is None):
            warnings.append(
                f"⚠ {n_dil} shelf/offering registration"
                f"{'s' if n_dil != 1 else ''} filed in the last 90 days — "
                f"dilution overhang on a name this size.")
        elif n_dil:
            sentences.append(
                f"{n_dil} shelf/offering registration"
                f"{'s' if n_dil != 1 else ''} in the last 90 days — routine "
                f"treasury activity at this market cap rather than a dilution "
                f"signal.")
        if f.get("activist_flag"):
            sentences.append(
                "A 13D has been filed, meaning someone has taken an activist "
                "position with intent to influence — a different situation from "
                "a passive 13G stake.")
        if f.get("insider_cluster"):
            sentences.append(
                f"{get('insider_30d')} Form 4s in 30 days is a cluster; the "
                f"filings themselves say whether that is buying or selling.")
    elif f.get("reason"):
        warnings.append(f"⚠ SEC filings unavailable: {f['reason']}.")

    if p["earnings_date"] is None:
        warnings.append("⚠ No earnings date available.")

    return {"headline": _headline(p), "warnings": warnings, "sentences": sentences}


def _headline(p: dict) -> str:
    days = p["earnings_days_out"]
    if days is not None and 0 <= days <= EARNINGS_SOON_DAYS:
        move = f", ±{p['implied_move']:.1%} implied" if p["implied_move"] else ""
        return f"Earnings in {days} day{'s' if days != 1 else ''}{move}"
    cap = p.get("market_cap")
    if p["filings"].get("dilution_flag") and (cap is None or cap < DILUTION_CAP_LIMIT):
        return "Dilution overhang — recent shelf registration on file"
    if p["filings"].get("activist_flag"):
        return "Activist 13D on file"
    if days is not None and days > 0:
        return f"Next earnings in {days} days"
    return f"No near-term catalyst identified for {p['symbol']}"
