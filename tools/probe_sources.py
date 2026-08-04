"""One-off endpoint verification (build step 3).

Hits every candidate data source, prints the response shape, and reports which
ones are usable. Run before building anything that depends on them:

    python tools/probe_sources.py

Findings get written up in DATA_SOURCES.md. This script is kept in the repo so
the probes can be re-run when a source breaks.
"""

from __future__ import annotations

import json
import sys

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

RESULTS: list[tuple[str, str, str]] = []  # (name, verdict, note)


def rec(name: str, verdict: str, note: str = "") -> None:
    RESULTS.append((name, verdict, note))
    print(f"  -> {verdict}: {note}"[:400])


def get(url: str, timeout: float = 25.0):
    return requests.get(url, headers=HEADERS, timeout=timeout)


# --------------------------------------------------------------------------- #
# 1. Option chains
# --------------------------------------------------------------------------- #

def probe_chain(label: str, url: str) -> None:
    print(f"\n=== {label} ===\n{url}")
    try:
        r = get(url)
    except Exception as exc:
        rec(label, "FAIL", f"{type(exc).__name__}: {exc}")
        return
    print(f"  HTTP {r.status_code}  {len(r.content)/1e6:.2f} MB")
    if r.status_code != 200:
        rec(label, "FAIL", f"HTTP {r.status_code}")
        return
    try:
        j = r.json()
    except Exception as exc:
        rec(label, "FAIL", f"not JSON: {exc}")
        return
    data = j.get("data") or {}
    opts = data.get("options") or []
    print(f"  top keys: {list(j.keys())}")
    print(f"  data keys: {list(data.keys())[:20]}")
    print(f"  timestamp: {j.get('timestamp')!r}")
    print(f"  current_price={data.get('current_price')!r} close={data.get('close')!r} "
          f"bid={data.get('bid')!r} ask={data.get('ask')!r}")
    print(f"  n options: {len(opts)}")
    if opts:
        print(f"  sample contract keys: {list(opts[0].keys())}")
        roots: dict[str, int] = {}
        for o in opts:
            s = o.get("option", "")
            # root = leading alpha chars before the 6-digit date
            i = 0
            while i < len(s) and s[i].isalpha():
                i += 1
            roots[s[:i]] = roots.get(s[:i], 0) + 1
        print(f"  symbol roots: {roots}")
        print("  sample symbols: " + ", ".join(o.get("option", "") for o in opts[:4]))
        sample = opts[0]
        print("  sample values: " + json.dumps(
            {k: sample.get(k) for k in
             ("option", "open_interest", "gamma", "delta", "iv", "volume",
              "last_trade_price", "bid", "ask")}))
        with_oi = sum(1 for o in opts if (o.get("open_interest") or 0) > 0)
        print(f"  contracts with OI>0: {with_oi}")
        rec(label, "OK", f"{len(opts)} contracts, roots={sorted(roots)}, OI>0={with_oi}")
    else:
        rec(label, "FAIL", "no options array")


# --------------------------------------------------------------------------- #
# 2. VIX futures candidates
# --------------------------------------------------------------------------- #

VX_CANDIDATES = [
    ("VX futures CDN (VX.json)",
     "https://cdn.cboe.com/api/global/delayed_quotes/futures/VX.json"),
    ("VX futures CDN (_VX.json)",
     "https://cdn.cboe.com/api/global/delayed_quotes/futures/_VX.json"),
    ("CFE settlement CSV (all)",
     "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX.csv"),
    ("CFE daily settlement JSON",
     "https://cdn.cboe.com/api/global/delayed_quotes/futures/settlements/VX.json"),
    ("CBOE futures product listing",
     "https://cdn.cboe.com/api/global/delayed_quotes/symbol_book/futures-roots.json"),
]


def probe_vx() -> None:
    for label, url in VX_CANDIDATES:
        print(f"\n=== {label} ===\n{url}")
        try:
            r = get(url)
        except Exception as exc:
            rec(label, "FAIL", f"{type(exc).__name__}: {exc}")
            continue
        ct = r.headers.get("content-type", "")
        print(f"  HTTP {r.status_code}  {len(r.content)} bytes  {ct}")
        if r.status_code != 200:
            rec(label, "FAIL", f"HTTP {r.status_code}")
            continue
        body = r.text[:600]
        print("  head: " + body.replace("\n", " | ")[:500])
        rec(label, "OK", f"HTTP 200, {len(r.content)} bytes, ct={ct}")


# --------------------------------------------------------------------------- #
# 3. Implied correlation candidates
# --------------------------------------------------------------------------- #

COR_CANDIDATES = [
    ("COR1M quote", "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_COR1M.json"),
    ("COR3M quote", "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_COR3M.json"),
    ("COR1M chart", "https://cdn.cboe.com/api/global/delayed_quotes/charts/_COR1M.json"),
    ("VIX quote (control)", "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json"),
    ("COR1M historical CSV",
     "https://cdn.cboe.com/api/global/us_indices/daily_prices/COR1M_History.csv"),
    ("COR3M historical CSV",
     "https://cdn.cboe.com/api/global/us_indices/daily_prices/COR3M_History.csv"),
    ("VIX historical CSV (control)",
     "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"),
]


def probe_cor() -> None:
    for label, url in COR_CANDIDATES:
        print(f"\n=== {label} ===\n{url}")
        try:
            r = get(url)
        except Exception as exc:
            rec(label, "FAIL", f"{type(exc).__name__}: {exc}")
            continue
        print(f"  HTTP {r.status_code}  {len(r.content)} bytes")
        if r.status_code != 200:
            rec(label, "FAIL", f"HTTP {r.status_code}")
            continue
        print("  head: " + r.text[:400].replace("\n", " | "))
        rec(label, "OK", f"HTTP 200, {len(r.content)} bytes")


# --------------------------------------------------------------------------- #
# 4. yfinance tickers
# --------------------------------------------------------------------------- #

YF_TICKERS = ["^VIX", "^VIX9D", "^VIX3M", "^VIX6M", "^VVIX", "^SPX", "^GSPC", "SPY"]


def probe_yf() -> None:
    print("\n=== yfinance ===")
    try:
        import yfinance as yf
    except Exception as exc:
        rec("yfinance import", "FAIL", str(exc))
        return
    for t in YF_TICKERS:
        try:
            h = yf.Ticker(t).history(period="5d")
            if h.empty:
                rec(f"yf {t}", "FAIL", "empty history")
            else:
                last = h.iloc[-1]
                rec(f"yf {t}", "OK",
                    f"last close {float(last['Close']):.2f} on {h.index[-1].date()}")
        except Exception as exc:
            rec(f"yf {t}", "FAIL", f"{type(exc).__name__}: {exc}")

    # Intraday availability for the volume profile panel
    print("\n--- SPY intraday ---")
    for period, interval in (("5d", "1m"), ("1mo", "5m"), ("60d", "5m")):
        try:
            h = yf.Ticker("SPY").history(period=period, interval=interval)
            if h.empty:
                rec(f"yf SPY {period}/{interval}", "FAIL", "empty")
            else:
                days = len({d.date() for d in h.index})
                rec(f"yf SPY {period}/{interval}", "OK",
                    f"{len(h)} bars over {days} sessions, "
                    f"{h.index[0]} .. {h.index[-1]}, has Volume={'Volume' in h.columns}")
        except Exception as exc:
            rec(f"yf SPY {period}/{interval}", "FAIL", f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #

def main() -> int:
    probe_chain("SPY chain",
                "https://cdn.cboe.com/api/global/delayed_quotes/options/SPY.json")
    probe_chain("SPX chain",
                "https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json")
    probe_vx()
    probe_cor()
    probe_yf()

    print("\n\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for name, verdict, note in RESULTS:
        print(f"{verdict:5}  {name:32}  {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
