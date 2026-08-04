"""Tests for the expiration calendar.

The expiry rules are anchored to dates confirmed against live CBOE data on
2026-08-04 (see DATA_SOURCES.md):

  * the SPX chain carried contracts expiring 2026-08-21  -> monthly OPEX
  * the VIX chain's monthly (root `VIX`) expiries were 2026-08-19 and
    2026-09-16 -> VIX expiry

    pytest panels/test_calendar_context.py
"""

from __future__ import annotations

from datetime import date

from panels import calendar_context as cal


# --------------------------------------------------------------------------- #
# Expiry rules, anchored to observed live data
# --------------------------------------------------------------------------- #

def test_third_friday_matches_live_spx_chain():
    # Live SPX symbols on 2026-08-04 included SPX260821* -> 21 Aug 2026.
    assert cal.third_friday(2026, 8) == date(2026, 8, 21)
    assert cal.third_friday(2026, 9) == date(2026, 9, 18)
    assert cal.third_friday(2026, 1) == date(2026, 1, 16)
    for y in (2025, 2026, 2027):
        for m in range(1, 13):
            assert cal.third_friday(y, m).weekday() == 4


def test_vix_expiry_matches_live_vix_chain():
    # Live monthly VIX expiries on 2026-08-04 were 19 Aug and 16 Sep 2026.
    assert cal.vix_expiry_for_month(2026, 8) == date(2026, 8, 19)
    assert cal.vix_expiry_for_month(2026, 9) == date(2026, 9, 16)


def test_vix_expiry_is_30_days_before_following_third_friday():
    for y in (2025, 2026, 2027):
        for m in range(1, 13):
            exp = cal.vix_expiry_for_month(y, m)
            ny, nm = (y + (m == 12), 1 if m == 12 else m + 1)
            gap = (cal.third_friday(ny, nm) - exp).days
            # Exactly 30 unless a holiday rolled it back a business day.
            assert gap in (30, 31, 32, 33), f"{y}-{m}: gap {gap}"
            assert cal.is_trading_day(exp)


def test_vix_expiry_lands_on_wednesday_absent_holidays():
    hits = 0
    for y in (2025, 2026, 2027):
        for m in range(1, 13):
            if cal.vix_expiry_for_month(y, m).weekday() == 2:
                hits += 1
    assert hits >= 33  # 36 months, allowing a few holiday rolls


def test_vix_expiry_is_distinct_from_opex():
    """The two events must never be conflated — different weeks, different rules."""
    for y in (2025, 2026, 2027):
        for m in range(1, 13):
            assert cal.vix_expiry_for_month(y, m) != cal.third_friday(y, m)


# --------------------------------------------------------------------------- #
# Holidays
# --------------------------------------------------------------------------- #

def test_known_holidays():
    h2026 = cal.market_holidays(2026)
    assert date(2026, 4, 3) in h2026       # Good Friday (Easter 5 Apr 2026)
    assert date(2026, 11, 26) in h2026     # Thanksgiving
    assert date(2026, 7, 3) in h2026       # 4 Jul falls Saturday -> observed Fri
    assert date(2026, 1, 19) in h2026      # MLK Jr Day
    assert date(2026, 5, 25) in h2026      # Memorial Day
    assert date(2025, 4, 18) in cal.market_holidays(2025)   # Good Friday 2025


def test_is_trading_day():
    assert cal.is_trading_day(date(2026, 8, 4))        # Tuesday
    assert not cal.is_trading_day(date(2026, 8, 8))    # Saturday
    assert not cal.is_trading_day(date(2026, 7, 3))    # observed holiday


def test_trading_days_ahead_skips_weekends_and_holidays():
    days = cal.trading_days_ahead(date(2026, 7, 1), 5)
    assert date(2026, 7, 3) not in days                # observed 4 Jul
    assert date(2026, 7, 4) not in days
    assert all(cal.is_trading_day(d) for d in days)
    assert days == [date(2026, 7, 2), date(2026, 7, 6), date(2026, 7, 7),
                    date(2026, 7, 8), date(2026, 7, 9)]


def test_trading_days_between_is_signed():
    a, b = date(2026, 8, 3), date(2026, 8, 7)          # Mon -> Fri
    assert cal.trading_days_between(a, b) == 4
    assert cal.trading_days_between(b, a) == -4
    assert cal.trading_days_between(a, a) == 0


# --------------------------------------------------------------------------- #
# Payload
# --------------------------------------------------------------------------- #

def test_compute_midcycle():
    p = cal.compute(date(2026, 8, 4))
    assert p["next_opex"] == "2026-08-21"
    assert p["days_to_opex"] == 17
    assert p["prev_opex"] == "2026-07-17"
    assert p["next_vix_expiry"] == "2026-08-19"
    assert p["days_to_vix_expiry"] == 15
    assert p["next_quarterly_opex"] == "2026-09-18"
    assert p["is_triple_witching_next"] is False
    assert p["post_opex_week"] is False
    assert p["quarter_end"] == "2026-09-30"


def test_post_opex_week_flag():
    # 2026-07-17 was a monthly OPEX Friday; the next session is 2026-07-20.
    assert cal.compute(date(2026, 7, 20))["post_opex_week"] is True
    assert cal.compute(date(2026, 7, 24))["post_opex_week"] is True   # 5 sessions
    assert cal.compute(date(2026, 7, 27))["post_opex_week"] is False  # 6 sessions
    # On OPEX day itself it has not happened yet.
    assert cal.compute(date(2026, 7, 17))["post_opex_week"] is False


def test_triple_witching_and_ex_div_align():
    p = cal.compute(date(2026, 9, 15))
    assert p["is_triple_witching_next"] is True
    assert p["next_opex"] == "2026-09-18"
    # SPY's quarterly ex-div is the same day — the early-exercise caveat.
    assert p["next_spy_ex_div"] == "2026-09-18"
    assert p["spy_ex_div_warning"] is True
    # Far from it, no warning.
    assert cal.compute(date(2026, 8, 4))["spy_ex_div_warning"] is False


def test_events_are_sorted_deduped_and_in_window():
    p = cal.compute(date(2026, 8, 4))
    ev = p["events"]
    assert ev == sorted(ev, key=lambda e: e["days"])
    assert all(0 <= e["days"] <= 45 for e in ev)
    assert len({(e["date"], e["label"]) for e in ev}) == len(ev)
    labels = {e["label"] for e in ev}
    assert "VIX expiry" in labels and "Monthly OPEX" in labels


def test_quarter_end_rolls_forward():
    assert cal.compute(date(2026, 9, 30))["quarter_end"] == "2026-09-30"
    assert cal.compute(date(2026, 10, 1))["quarter_end"] == "2026-12-31"
    assert cal.compute(date(2026, 12, 31))["quarter_end"] == "2026-12-31"


def test_compute_runs_for_every_day_of_a_year():
    """No crashes, and every derived date stays consistent."""
    d = date(2026, 1, 1)
    while d < date(2027, 1, 1):
        p = cal.compute(d)
        assert p["days_to_opex"] >= 0
        assert p["days_to_vix_expiry"] >= 0
        assert p["days_since_opex"] >= 0
        assert p["days_to_quarter_end"] >= 0
        d = date.fromordinal(d.toordinal() + 1)
