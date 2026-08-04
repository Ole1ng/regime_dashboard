"""Expiration / event calendar — RESEARCH.md §6 and §3.

Pure date arithmetic, no network, no I/O. Everything takes an injectable
``today`` so it is fully testable.

The three rules worth stating precisely, because they are easy to get wrong:

* **Monthly OPEX** — the third Friday of the month. SPX settles AM (the SET
  print) on that day; SPXW and SPY settle PM.
* **VIX expiry** — the Wednesday **30 days before the following month's third
  Friday**. Not the third Wednesday, and not tied to this month's OPEX. It lands
  mid-month, usually the Wednesday of or before OPEX week, and is a *distinct*
  event from SPX OPEX. Rolled back to the prior business day on a holiday.
* **SPY quarterly ex-dividend** — SPY distributes quarterly with an ex-date on
  the third Friday of Mar/Jun/Sep/Dec, i.e. coincident with quarterly OPEX. That
  is what strips ITM-call open interest out of the SPY chain via early exercise
  exactly when OPEX is meant to be pinning it (RESEARCH.md §1).
"""

from __future__ import annotations

from datetime import date, timedelta

QUARTER_MONTHS = (3, 6, 9, 12)

# How many sessions after monthly OPEX still count as "post-OPEX week".
POST_OPEX_WINDOW = 5
# Warn about SPY early-exercise risk this many days before a quarterly ex-div.
EX_DIV_WARN_DAYS = 5


# --------------------------------------------------------------------------- #
# US equity market holidays (needed for trading-day counts and expiry rolls)
# --------------------------------------------------------------------------- #

def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm. Good Friday is Easter minus two days."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """nth occurrence of `weekday` (Mon=0) in a month."""
    d = date(year, month, 1)
    first = d + timedelta(days=(weekday - d.weekday()) % 7)
    return first + timedelta(days=7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    """Saturday holidays observe Friday, Sunday holidays observe Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def market_holidays(year: int) -> set[date]:
    """NYSE full-day closures for a year (excludes half-days)."""
    return {
        _observed(date(year, 1, 1)),                  # New Year's Day
        _nth_weekday(year, 1, 0, 3),                  # MLK Jr Day
        _nth_weekday(year, 2, 0, 3),                  # Presidents' Day
        _easter(year) - timedelta(days=2),            # Good Friday
        _last_weekday(year, 5, 0),                    # Memorial Day
        _observed(date(year, 6, 19)),                 # Juneteenth
        _observed(date(year, 7, 4)),                  # Independence Day
        _nth_weekday(year, 9, 0, 1),                  # Labor Day
        _nth_weekday(year, 11, 3, 4),                 # Thanksgiving
        _observed(date(year, 12, 25)),                # Christmas
    }


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in market_holidays(d.year)


def prev_business_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def trading_days_ahead(start: date, n: int) -> list[date]:
    """The next `n` trading days strictly after `start`."""
    out, d = [], start
    while len(out) < n:
        d += timedelta(days=1)
        if is_trading_day(d):
            out.append(d)
    return out


def trading_days_between(a: date, b: date) -> int:
    """Trading days in (a, b]. Negative if b precedes a."""
    if b < a:
        return -trading_days_between(b, a)
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if is_trading_day(d):
            n += 1
    return n


# --------------------------------------------------------------------------- #
# Expiration rules
# --------------------------------------------------------------------------- #

def third_friday(year: int, month: int) -> date:
    return _nth_weekday(year, month, 4, 3)


def _add_month(year: int, month: int) -> tuple[int, int]:
    return (year + (month == 12), 1 if month == 12 else month + 1)


def _sub_month(year: int, month: int) -> tuple[int, int]:
    return (year - (month == 1), 12 if month == 1 else month - 1)


def next_monthly_opex(today: date) -> date:
    this = third_friday(today.year, today.month)
    if this >= today:
        return this
    return third_friday(*_add_month(today.year, today.month))


def prev_monthly_opex(today: date) -> date:
    this = third_friday(today.year, today.month)
    if this <= today:
        return this
    return third_friday(*_sub_month(today.year, today.month))


def next_quarterly_opex(today: date) -> date:
    """Third Friday of the next Mar/Jun/Sep/Dec — triple witching."""
    y, m = today.year, today.month
    for _ in range(13):
        if m in QUARTER_MONTHS:
            d = third_friday(y, m)
            if d >= today:
                return d
        y, m = _add_month(y, m)
    raise RuntimeError("no quarterly OPEX found")


def vix_expiry_for_month(year: int, month: int) -> date:
    """The VIX expiry that falls in `month`.

    Defined as 30 days before the *following* month's third Friday, which lands
    on a Wednesday; rolled back to the prior business day if that is a holiday.
    """
    ny, nm = _add_month(year, month)
    d = third_friday(ny, nm) - timedelta(days=30)
    if not is_trading_day(d):
        d = prev_business_day(d)
    return d


def next_vix_expiry(today: date) -> date:
    this = vix_expiry_for_month(today.year, today.month)
    if this >= today:
        return this
    return vix_expiry_for_month(*_add_month(today.year, today.month))


def next_spy_ex_div(today: date) -> date:
    """SPY's quarterly ex-dividend date — the third Friday of Mar/Jun/Sep/Dec."""
    return next_quarterly_opex(today)


def quarter_end(today: date) -> date:
    q_month = QUARTER_MONTHS[(today.month - 1) // 3]
    y = today.year
    d = date(y, q_month, 1)
    nxt = date(y + (q_month == 12), 1 if q_month == 12 else q_month + 1, 1)
    d = nxt - timedelta(days=1)
    if d < today:
        y2, m2 = _add_month(y, q_month)
        q2 = QUARTER_MONTHS[(m2 - 1) // 3]
        nxt2 = date(y2 + (q2 == 12), 1 if q2 == 12 else q2 + 1, 1)
        d = nxt2 - timedelta(days=1)
    return d


# --------------------------------------------------------------------------- #
# Panel payload
# --------------------------------------------------------------------------- #

JHEQX_NOTE = ("JPM's hedged-equity collar (JHEQX) rolls at quarter end; expect fresh "
              "SPX open-interest spikes at the new put-spread and short-call strikes, "
              "which then act as quarter-long reference levels.")


def compute(today: date | None = None) -> dict:
    """Full calendar payload. `today` is injectable for tests."""
    today = today or date.today()

    nxt_opex = next_monthly_opex(today)
    prv_opex = prev_monthly_opex(today)
    q_opex = next_quarterly_opex(today)
    vix_exp = next_vix_expiry(today)
    ex_div = next_spy_ex_div(today)
    q_end = quarter_end(today)

    days_since_opex = (today - prv_opex).days
    sessions_since_opex = trading_days_between(prv_opex, today)
    post_opex_week = 1 <= sessions_since_opex <= POST_OPEX_WINDOW

    events = [
        {"date": vix_exp.isoformat(), "label": "VIX expiry", "kind": "vix",
         "days": (vix_exp - today).days},
        {"date": nxt_opex.isoformat(),
         "label": "Triple witching" if nxt_opex == q_opex else "Monthly OPEX",
         "kind": "opex_q" if nxt_opex == q_opex else "opex",
         "days": (nxt_opex - today).days},
        {"date": q_end.isoformat(), "label": "Quarter end (JHEQX roll)",
         "kind": "quarter", "days": (q_end - today).days},
    ]
    if q_opex != nxt_opex:
        events.append({"date": q_opex.isoformat(), "label": "Triple witching",
                       "kind": "opex_q", "days": (q_opex - today).days})
    # A second VIX expiry inside the 45-day window is common.
    vix2 = next_vix_expiry(vix_exp + timedelta(days=1))
    if (vix2 - today).days <= 45:
        events.append({"date": vix2.isoformat(), "label": "VIX expiry",
                       "kind": "vix", "days": (vix2 - today).days})
    nxt_opex2 = next_monthly_opex(nxt_opex + timedelta(days=1))
    if (nxt_opex2 - today).days <= 45:
        events.append({"date": nxt_opex2.isoformat(),
                       "label": "Triple witching" if nxt_opex2 == q_opex else "Monthly OPEX",
                       "kind": "opex_q" if nxt_opex2 == q_opex else "opex",
                       "days": (nxt_opex2 - today).days})

    events = [e for e in events if 0 <= e["days"] <= 45]
    events.sort(key=lambda e: e["days"])
    # de-duplicate same date+label
    seen, uniq = set(), []
    for e in events:
        k = (e["date"], e["label"])
        if k not in seen:
            seen.add(k)
            uniq.append(e)

    return {
        "today": today.isoformat(),
        "next_opex": nxt_opex.isoformat(),
        "days_to_opex": (nxt_opex - today).days,
        "sessions_to_opex": trading_days_between(today, nxt_opex),
        "prev_opex": prv_opex.isoformat(),
        "days_since_opex": days_since_opex,
        "sessions_since_opex": sessions_since_opex,
        "post_opex_week": post_opex_week,
        "next_quarterly_opex": q_opex.isoformat(),
        "days_to_quarterly_opex": (q_opex - today).days,
        "is_triple_witching_next": nxt_opex == q_opex,
        "next_vix_expiry": vix_exp.isoformat(),
        "days_to_vix_expiry": (vix_exp - today).days,
        "next_spy_ex_div": ex_div.isoformat(),
        "days_to_spy_ex_div": (ex_div - today).days,
        "spy_ex_div_warning": 0 <= (ex_div - today).days <= EX_DIV_WARN_DAYS,
        "quarter_end": q_end.isoformat(),
        "days_to_quarter_end": (q_end - today).days,
        "jheqx_note": JHEQX_NOTE,
        "events": uniq,
        "window_days": 45,
    }


def refresh() -> dict:
    return compute()
