"""Live market levels for the Tab 3 news screener panels.

A tone reading on its own is a curiosity. Next to the tape it becomes useful:
the interesting configuration is the one where the coverage and the price
disagree — headlines uniformly bearish crude while crude is up three days
running, or a US Fixed Income panel reading supportive while the 10-year keeps
climbing.

**One batched download for all eight panels**, not one call per panel: yfinance
happily takes the full symbol list in a single request, and eight sequential
calls would double the tab's refresh time for data that costs nothing extra to
fetch together.

**This module must never fail the tab.** Every entry point swallows its errors
and returns empty quotes; a panel with no strip still renders its headlines and
its sentiment. Prices are the garnish here, not the dish.

Symbols were verified live on 2026-08-16 (DATA_SOURCES.md §12). Two traps worth
keeping in mind:

  * ``^TNX`` is a **direct percentage** (4.696 means 4.696%), not the ×10 form
    older code assumed.
  * Yahoo has **no 2-year yield**, so the curve here is 3-month (``^IRX``) to
    10-year rather than the conventional 2s10s. It is labelled as such.
"""

from __future__ import annotations

# Row kinds decide formatting only. "yield" is quoted in percent and moves in
# basis points; everything else moves in percent.
YIELD, INDEX, FUTURES, FX = "yield", "index", "futures", "fx"

# (label, symbol, kind) per panel, in render order.
SPECS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "t3_us_macro": (
        ("Dollar (DXY)", "DX-Y.NYB", INDEX),
        ("UST 10Y", "^TNX", YIELD),
        ("VIX", "^VIX", INDEX),
    ),
    "t3_us_equities": (
        ("S&P 500", "^GSPC", INDEX),
        ("Nasdaq 100", "^NDX", INDEX),
        ("Russell 2000", "^RUT", INDEX),
        ("VIX", "^VIX", INDEX),
    ),
    "t3_us_rates": (
        ("UST 3M", "^IRX", YIELD),
        ("UST 5Y", "^FVX", YIELD),
        ("UST 10Y", "^TNX", YIELD),
        ("UST 30Y", "^TYX", YIELD),
        ("HY credit (HYG)", "HYG", INDEX),
    ),
    "t3_eu_macro": (
        ("EUR/USD", "EURUSD=X", FX),
        ("GBP/USD", "GBPUSD=X", FX),
        ("Europe ETF (VGK)", "VGK", INDEX),
    ),
    "t3_eu_markets": (
        ("Euro Stoxx 50", "^STOXX50E", INDEX),
        ("DAX", "^GDAXI", INDEX),
        ("FTSE 100", "^FTSE", INDEX),
        ("CAC 40", "^FCHI", INDEX),
    ),
    "t3_energy": (
        ("WTI", "CL=F", FUTURES),
        ("Brent", "BZ=F", FUTURES),
        ("Nat gas", "NG=F", FUTURES),
        ("Gasoline", "RB=F", FUTURES),
    ),
    "t3_precious": (
        ("Gold", "GC=F", FUTURES),
        ("Silver", "SI=F", FUTURES),
        ("Platinum", "PL=F", FUTURES),
        ("Palladium", "PA=F", FUTURES),
    ),
    "t3_metals": (
        ("Copper", "HG=F", FUTURES),
        ("Aluminium", "ALI=F", FUTURES),
        ("Metals & mining (XME)", "XME", INDEX),
    ),
}

# Derived rows: (panel, label, numerator, denominator/other, mode).
# `mode` is "curve" for a yield difference reported in bp, "spread" for a price
# difference in dollars, "ratio" for a plain quotient.
DERIVED: tuple[tuple[str, str, str, str, str], ...] = (
    # Labelled 3M-10Y, not 2s10s: Yahoo has no 2-year yield. Saying "2s10s"
    # here would be quietly wrong on the one number a rates reader checks first.
    ("t3_us_rates", "Curve 3M-10Y", "^TNX", "^IRX", "curve"),
    ("t3_us_rates", "Curve 5s30s", "^TYX", "^FVX", "curve"),
    ("t3_energy", "Brent-WTI", "BZ=F", "CL=F", "spread"),
    ("t3_precious", "Gold/Silver", "GC=F", "SI=F", "ratio"),
    ("t3_metals", "Copper/Gold", "HG=F", "GC=F", "ratio"),
)

PANEL_KEYS = tuple(SPECS)


def _all_symbols() -> list[str]:
    seen: list[str] = []
    for rows in SPECS.values():
        for _label, symbol, _kind in rows:
            if symbol not in seen:
                seen.append(symbol)
    # Derived rows may reference a symbol no panel quotes directly (the metals
    # panel needs gold for copper/gold but does not show gold).
    for _panel, _label, a, b, _mode in DERIVED:
        for symbol in (a, b):
            if symbol not in seen:
                seen.append(symbol)
    return seen


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #

def fetch(symbols: list[str] | None = None) -> dict[str, tuple[float, float]]:
    """``{symbol: (last, previous)}`` for every symbol that returned two closes.

    Six calendar days for two trading closes: it survives a weekend plus a
    public holiday, which a 2-day request does not. Symbols that fail are simply
    absent from the result — futures and cash indices keep different holiday
    calendars, so a partial answer is the normal case, not an error.
    """
    symbols = symbols or _all_symbols()
    try:
        import yfinance as yf
        frame = yf.download(symbols, period="6d", interval="1d",
                            progress=False, auto_adjust=False,
                            threads=True)["Close"]
    except Exception:
        return {}

    out: dict[str, tuple[float, float]] = {}
    for symbol in symbols:
        try:
            series = frame[symbol].dropna()
        except Exception:
            continue
        if len(series) < 2:
            continue
        last, prev = float(series.iloc[-1]), float(series.iloc[-2])
        if last == last and prev == prev:  # NaN never equals itself
            out[symbol] = (last, prev)
    return out


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #

def _fmt_level(value: float, kind: str) -> str:
    if kind == YIELD:
        return f"{value:.3f}%"
    if kind == FX:
        return f"{value:.4f}"
    if kind == FUTURES:
        return f"${value:,.2f}"
    # Cents matter on a $117 ETF and are noise on a 30,046 index level.
    return f"{value:,.2f}" if abs(value) < 1000 else f"{value:,.0f}"


def _fmt_delta(last: float, prev: float, kind: str) -> tuple[str, int]:
    """(display, direction). Yields move in basis points, everything else in %."""
    change = last - prev
    direction = 1 if change > 0 else (-1 if change < 0 else 0)
    if kind == YIELD:
        return f"{change * 100:+.1f}bp", direction
    if prev:
        return f"{(change / prev) * 100:+.2f}%", direction
    return f"{change:+.2f}", direction


def _row(label: str, last: float, prev: float, kind: str) -> dict:
    delta, direction = _fmt_delta(last, prev, kind)
    return {"label": label, "value": round(last, 4), "kind": kind,
            "display": _fmt_level(last, kind), "delta": delta,
            "dir": direction}


def _derived_row(label: str, mode: str, a: tuple[float, float],
                 b: tuple[float, float]) -> dict:
    """A spread or ratio, plus how it moved since the previous close."""
    if mode == "curve":
        last, prev = a[0] - b[0], a[1] - b[1]
        display = f"{last * 100:+.0f}bp"
        delta = f"{(last - prev) * 100:+.1f}bp"
    elif mode == "spread":
        last, prev = a[0] - b[0], a[1] - b[1]
        display = f"${last:,.2f}"
        delta = f"{last - prev:+.2f}"
    else:  # ratio
        if not b[0] or not b[1]:
            raise ZeroDivisionError(label)
        last, prev = a[0] / b[0], a[1] / b[1]
        display = f"{last:,.1f}" if last >= 10 else f"{last:,.4f}"
        # Percent, not an absolute difference: copper/gold sits around 0.0015,
        # where a real day's move rounds to "+0.0000" and reads as unchanged.
        delta = f"{((last - prev) / prev) * 100:+.2f}%" if prev else "—"

    change = last - prev
    return {"label": label, "value": round(last, 4), "kind": mode,
            "display": display, "delta": delta,
            "dir": 1 if change > 0 else (-1 if change < 0 else 0)}


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def build(closes: dict[str, tuple[float, float]] | None = None
          ) -> dict[str, list[dict]]:
    """``{panel_key: [quote rows]}`` for every panel.

    Pass ``closes`` to build offline from known values; omit it to fetch. A
    panel whose symbols all failed gets an empty list rather than being absent,
    so the caller never has to distinguish "no data" from "no such panel".
    """
    if closes is None:
        try:
            closes = fetch()
        except Exception:
            closes = {}

    out: dict[str, list[dict]] = {panel: [] for panel in SPECS}

    for panel, rows in SPECS.items():
        for label, symbol, kind in rows:
            pair = closes.get(symbol)
            if not pair:
                continue
            try:
                out[panel].append(_row(label, pair[0], pair[1], kind))
            except Exception:
                continue

    for panel, label, a_sym, b_sym, mode in DERIVED:
        a, b = closes.get(a_sym), closes.get(b_sym)
        if not a or not b:
            continue
        try:
            out[panel].append(_derived_row(label, mode, a, b))
        except Exception:
            continue

    return out
