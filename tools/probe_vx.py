"""Round-2 probe: VIX futures term structure.

Round 1 (probe_sources.py) found every direct CBOE VX futures endpoint returns
403. This script tries the remaining honest options before we fall back to the
VIX index family alone. The most promising is the last one: VIX *options* are
priced off VIX futures, not spot VIX, so put-call parity on the CBOE VIX option
chain recovers the forward (i.e. the VX future) for every listed expiry.

    python tools/probe_vx.py
"""

from __future__ import annotations

import re
from datetime import datetime

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

DIRECT = [
    ("cboe.com futures quotes API",
     "https://www.cboe.com/us/futures/api/get_quotes_combined/?symbol=VX"),
    ("cboe.com futures json",
     "https://www.cboe.com/us/futures/market_statistics/settlement/csv/"),
    ("CDN term structure",
     "https://cdn.cboe.com/api/global/delayed_quotes/term_structure/VX.json"),
    ("CDN futures index quote",
     "https://cdn.cboe.com/api/global/delayed_quotes/quotes/VX.json"),
    ("VIX1D index quote",
     "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX1D.json"),
]


def probe_direct() -> None:
    for label, url in DIRECT:
        print(f"\n=== {label} ===\n{url}")
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            print(f"  HTTP {r.status_code}  {len(r.content)} bytes  "
                  f"{r.headers.get('content-type','')}")
            if r.status_code == 200:
                print("  head: " + r.text[:350].replace("\n", " | "))
        except Exception as exc:
            print(f"  FAIL {type(exc).__name__}: {exc}")


def probe_yf_futures() -> None:
    print("\n=== yfinance futures symbols ===")
    try:
        import yfinance as yf
    except Exception as exc:
        print(f"  import failed: {exc}")
        return
    for t in ("VX=F", "^VIX1D", "VIX=F", "VXX", "VIXY"):
        try:
            h = yf.Ticker(t).history(period="5d")
            print(f"  {t:8} -> {'EMPTY' if h.empty else f'{float(h.iloc[-1].Close):.2f}'}")
        except Exception as exc:
            print(f"  {t:8} -> FAIL {type(exc).__name__}")


def probe_vix_options() -> None:
    """The good one: recover VX forwards from the VIX option chain."""
    url = "https://cdn.cboe.com/api/global/delayed_quotes/options/_VIX.json"
    print(f"\n=== VIX option chain (forward via put-call parity) ===\n{url}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=40)
    except Exception as exc:
        print(f"  FAIL {type(exc).__name__}: {exc}")
        return
    print(f"  HTTP {r.status_code}  {len(r.content)/1e6:.2f} MB")
    if r.status_code != 200:
        print("  unusable")
        return
    j = r.json()
    data = j.get("data") or {}
    opts = data.get("options") or []
    print(f"  timestamp {j.get('timestamp')!r}  spot VIX {data.get('current_price')!r}")
    print(f"  n options {len(opts)}")
    if not opts:
        return
    print("  sample symbols: " + ", ".join(o["option"] for o in opts[:4]))

    sym_re = re.compile(r"^(VIX|VIXW)(\d{6})([CP])(\d{8})$")
    roots: dict[str, int] = {}
    # expiry -> strike -> {C:mid, P:mid}
    book: dict[str, dict[float, dict[str, float]]] = {}
    for o in opts:
        m = sym_re.match(o.get("option", ""))
        if not m:
            i = 0
            s = o.get("option", "")
            while i < len(s) and s[i].isalpha():
                i += 1
            roots[s[:i]] = roots.get(s[:i], 0) + 1
            continue
        roots[m.group(1)] = roots.get(m.group(1), 0) + 1
        exp, right, strike = m.group(2), m.group(3), int(m.group(4)) / 1000.0
        bid, ask = o.get("bid") or 0.0, o.get("ask") or 0.0
        if bid <= 0 or ask <= 0:
            continue
        book.setdefault(exp, {}).setdefault(strike, {})[right] = (bid + ask) / 2.0

    print(f"  roots: {roots}")
    print(f"  expiries with two-sided quotes: {len(book)}")
    print("\n  Implied forward per expiry (put-call parity, min |C-P| strike):")
    print("  expiry      days  strike   C      P      forward")
    today = datetime.now().date()
    for exp in sorted(book)[:10]:
        strikes = book[exp]
        pairs = [(k, v["C"], v["P"]) for k, v in strikes.items()
                 if "C" in v and "P" in v]
        if not pairs:
            continue
        k, c, p = min(pairs, key=lambda t: abs(t[1] - t[2]))
        fwd = k + (c - p)   # r~0 over these tenors; refine in the module
        d = datetime.strptime(exp, "%y%m%d").date()
        print(f"  20{exp[:2]}-{exp[2:4]}-{exp[4:]}  {(d - today).days:4}  "
              f"{k:6.1f}  {c:5.2f}  {p:5.2f}  {fwd:7.2f}")


if __name__ == "__main__":
    probe_direct()
    probe_yf_futures()
    probe_vix_options()
