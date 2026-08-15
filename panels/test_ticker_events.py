"""Tests for the earnings / catalysts panel.

Three things here are easy to get subtly wrong and hard to notice in the UI:

  * Finviz's earnings date carries **no year** ("Aug 26 AMC"), so the year has
    to be inferred — and naive year-stamping breaks across December/January.
  * EDGAR form codes are irregular. "424B5" starts with "4" but is an offering,
    not a Form 4, and 13D/G arrive as both "SC 13D" and "SCHEDULE 13D".
  * The counts and the post-earnings history must be computed over EVERY
    filing, not the twenty shown in the table — an active filer's most recent
    twenty rows are all Form 4s.

    pytest panels/test_ticker_events.py
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from panels import ticker_events as ev

TODAY = date(2026, 8, 15)
NOW = datetime(2026, 8, 15, 15, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Earnings date inference
# --------------------------------------------------------------------------- #

def test_earnings_parses_date_and_session():
    d, when, source = ev._earnings_date({"earnings_raw": "Aug 26 AMC"}, TODAY)
    assert d == date(2026, 8, 26)
    assert when == "AMC"
    assert source == "finviz"


def test_earnings_handles_before_market_open():
    d, when, _ = ev._earnings_date({"earnings_raw": "Aug 07 BMO"}, TODAY)
    assert d == date(2026, 8, 7)
    assert when == "BMO"


def test_earnings_year_inference_across_the_new_year():
    # In late December, a "Jan 28" print belongs to NEXT year. Stamping the
    # current year would put it eleven months in the past.
    december = date(2026, 12, 28)
    d, _, _ = ev._earnings_date({"earnings_raw": "Jan 28 AMC"}, december)
    assert d == date(2027, 1, 28)

    # And in early January, a "Dec 18" print belongs to LAST year.
    january = date(2027, 1, 5)
    d, _, _ = ev._earnings_date({"earnings_raw": "Dec 18 BMO"}, january)
    assert d == date(2026, 12, 18)


def test_earnings_missing_or_unparsable_degrades_quietly():
    assert ev._earnings_date({}, TODAY) == (None, None, "none")
    assert ev._earnings_date({"earnings_raw": "-"}, TODAY)[0] is None
    assert ev._earnings_date({"earnings_raw": "nonsense"}, TODAY)[2] == "unparsed"


# --------------------------------------------------------------------------- #
# EDGAR form classification
# --------------------------------------------------------------------------- #

def test_form_kinds():
    assert ev._form_kind("4", "") == "insider"
    assert ev._form_kind("3", "") == "insider"
    assert ev._form_kind("5", "") == "insider"
    assert ev._form_kind("4/A", "") == "insider"
    assert ev._form_kind("SCHEDULE 13D/A", "") == "activist"
    assert ev._form_kind("SC 13D", "") == "activist"
    assert ev._form_kind("SCHEDULE 13G", "") == "ownership"
    assert ev._form_kind("10-Q", "") == "periodic"
    assert ev._form_kind("DEF 14A", "") == "proxy"
    assert ev._form_kind("S-8", "") == "comp"
    assert ev._form_kind("8-K", "5.02") == "event"
    assert ev._form_kind("13F-HR", "") == "other"


def test_424b_is_an_offering_not_a_form_4():
    # The bug this guards: a prefix match on "4" swallows "424B5".
    assert ev._form_kind("424B5", "") == "offering"
    assert ev._form_kind("424B3", "") == "offering"


def test_shelf_and_offering_are_distinct_kinds():
    # An S-3ASR is a routine automatic shelf for a large issuer; a 424B is a
    # live prospectus supplement. Conflating them labels every mega-cap a
    # dilution risk.
    assert ev._form_kind("S-3ASR", "") == "shelf"
    assert ev._form_kind("S-3", "") == "shelf"
    assert ev._form_kind("S-1", "") == "shelf"
    assert ev._form_kind("424B5", "") == "offering"


def test_earnings_eight_k_is_detected_by_item_number():
    # Item 2.02 is "Results of Operations"; that is what makes an 8-K an
    # earnings release rather than any other material event.
    assert ev._form_kind("8-K", "2.02,9.01") == "earnings"
    assert ev._form_kind("8-K", "1.01,9.01") == "event"


# --------------------------------------------------------------------------- #
# Filing aggregation
# --------------------------------------------------------------------------- #

def submissions(rows):
    """Build an EDGAR-shaped payload from (date, form, items) triples."""
    return {"name": "Test Co", "sicDescription": "Testing",
            "exchanges": ["Nasdaq"],
            "filings": {"recent": {
                "form": [r[1] for r in rows],
                "filingDate": [r[0] for r in rows],
                "items": [r[2] if len(r) > 2 else "" for r in rows],
                "accessionNumber": [f"000-{i}" for i in range(len(rows))],
                "primaryDocument": ["doc.htm"] * len(rows),
                "primaryDocDescription": [""] * len(rows),
            }}}


def test_counts_and_earnings_dates_span_every_filing_not_just_the_display_slice():
    # 30 Form 4s crowd out the earnings 8-K from the first MAX_FILINGS rows.
    rows = [(f"2026-08-{(i % 28) + 1:02d}", "4") for i in range(30)]
    rows.append(("2026-05-20", "8-K", "2.02,9.01"))
    rows.append(("2026-02-25", "8-K", "2.02,9.01"))
    out = ev._classify_filings(submissions(rows), "0000000001", today=TODAY)

    assert len(out["recent"]) == ev.MAX_FILINGS      # display slice is capped
    assert out["total_scanned"] == 32                # ...but the scan is not
    assert out["earnings_dates"] == ["2026-05-20", "2026-02-25"]
    assert out["counts"]["insider_30d"] == 30
    assert out["insider_cluster"] is True


def test_dilution_flag_distinguishes_offerings_from_shelves():
    recent_offering = ev._classify_filings(
        submissions([("2026-07-01", "424B5")]), "1", today=TODAY)
    assert recent_offering["offering_flag"] is True
    assert recent_offering["dilution_flag"] is True

    old_shelf = ev._classify_filings(
        submissions([("2024-12-20", "S-3ASR")]), "1", today=TODAY)
    # Outside the 90-day window, so neither flag fires.
    assert old_shelf["offering_flag"] is False
    assert old_shelf["dilution_flag"] is False


def test_activist_flag_needs_a_13d_not_a_13g():
    d = ev._classify_filings(submissions([("2026-06-01", "SCHEDULE 13D/A")]),
                             "1", today=TODAY)
    g = ev._classify_filings(submissions([("2026-06-01", "SCHEDULE 13G")]),
                             "1", today=TODAY)
    assert d["activist_flag"] is True
    assert g["activist_flag"] is False


def test_filing_url_is_built_from_the_accession_number():
    out = ev._classify_filings(submissions([("2026-08-01", "8-K", "2.02")]),
                               "0000030697", today=TODAY)
    url = out["recent"][0]["url"]
    assert url.startswith("https://www.sec.gov/Archives/edgar/data/30697/")
    assert "-" not in url.rsplit("/", 2)[1]   # dashes stripped from accession


def test_etfs_skip_the_filings_lookup_entirely():
    out = ev._filings("SPY", security_type="etf")
    assert out["available"] is False
    assert "no issuer filings" in out["reason"]


# --------------------------------------------------------------------------- #
# Implied move
# --------------------------------------------------------------------------- #

def occ(sym, expiry, right, strike):
    return f"{sym}{expiry:%y%m%d}{right}{int(round(strike * 1000)):08d}"


def chain(options, spot=100.0):
    return {"data": {"current_price": spot, "options": options,
                     "security_type": "stock"}}


def straddle(expiry, strike=100.0, call=4.0, put=3.0):
    return [
        {"option": occ("XYZ", expiry, "C", strike), "bid": call - 0.1,
         "ask": call + 0.1, "open_interest": 100},
        {"option": occ("XYZ", expiry, "P", strike), "bid": put - 0.1,
         "ask": put + 0.1, "open_interest": 100},
    ]


def test_implied_move_is_the_atm_straddle_over_spot():
    expiry = TODAY + timedelta(days=10)
    out = ev._implied_move(chain(straddle(expiry)), "XYZ", 100.0,
                           TODAY + timedelta(days=5), TODAY)
    assert abs(out["implied_move"] - 0.07) < 1e-9      # (4 + 3) / 100
    assert out["covers_earnings"] is True
    assert out["atm_strike"] == 100.0


def test_implied_move_picks_the_first_expiry_after_earnings():
    early = TODAY + timedelta(days=3)
    late = TODAY + timedelta(days=20)
    options = straddle(early, call=1.0, put=1.0) + straddle(late, call=6.0, put=6.0)
    earnings = TODAY + timedelta(days=10)
    out = ev._implied_move(chain(options), "XYZ", 100.0, earnings, TODAY)
    # The near expiry is cheaper but expires BEFORE the print, so it prices
    # nothing about the event.
    assert out["implied_expiry"] == late.isoformat()
    assert abs(out["implied_move"] - 0.12) < 1e-9


def test_implied_move_flags_when_no_expiry_covers_the_event():
    expiry = TODAY + timedelta(days=3)
    earnings = TODAY + timedelta(days=40)
    out = ev._implied_move(chain(straddle(expiry)), "XYZ", 100.0, earnings, TODAY)
    assert out["covers_earnings"] is False


# --------------------------------------------------------------------------- #
# Historical comparison
# --------------------------------------------------------------------------- #

def test_move_ratio_needs_enough_prior_prints():
    expiry = TODAY + timedelta(days=10)
    filings = {"available": True, "earnings_dates": ["2026-05-20"],
               "recent": [], "counts": {}}
    p = ev.compute("XYZ", chain_json=chain(straddle(expiry)),
                   snapshot={"earnings_raw": "Aug 20 AMC", "market_cap": 5e9},
                   prices=None, filings=filings, now=NOW)
    # One prior move is an anecdote, not an average.
    assert p["move_ratio"] is None
    assert p["move_state"] == "unknown"


def price_frame(pairs):
    """Minimal daily close series shaped like a yfinance history frame."""
    import pandas as pd
    index = pd.to_datetime([d for d, _ in pairs])
    return pd.DataFrame({"Close": [c for _, c in pairs]}, index=index)


def _series_around(report_days, before=100.0, after_moves=()):
    """Daily closes flat at `before`, stepping to a new level after each report."""
    pairs = []
    level = before
    day = date(2026, 1, 1)
    moves = dict(zip(report_days, after_moves))
    while day <= date(2026, 8, 15):
        if day in moves:
            pairs.append((day, level))          # close before the print
            level = level * (1 + moves[day])
        else:
            pairs.append((day, level))
        day += timedelta(days=1)
    return price_frame(pairs)


def test_historical_moves_measures_the_close_around_each_earnings_filing():
    reports = [date(2026, 3, 2), date(2026, 5, 4), date(2026, 7, 6)]
    prices = _series_around(reports, after_moves=(-0.10, -0.05, -0.08))
    filings = {"available": True,
               "earnings_dates": [d.isoformat() for d in reports]}
    out = ev._historical_moves(prices, filings, TODAY)

    assert out["n"] == 3
    assert out["down"] == 3 and out["up"] == 0
    # All three reports moved the stock lower, which a magnitude-only straddle
    # cannot express — hence the flag.
    assert out["lopsided"] is True
    assert 0.07 < out["mean_abs"] < 0.09


def test_historical_moves_is_not_lopsided_when_direction_is_mixed():
    reports = [date(2026, 3, 2), date(2026, 5, 4), date(2026, 7, 6)]
    prices = _series_around(reports, after_moves=(0.10, -0.05, 0.08))
    filings = {"available": True,
               "earnings_dates": [d.isoformat() for d in reports]}
    out = ev._historical_moves(prices, filings, TODAY)
    assert out["n"] == 3
    assert out["lopsided"] is False


def test_historical_moves_returns_empty_without_price_history():
    filings = {"available": True, "earnings_dates": ["2026-05-20"]}
    assert ev._historical_moves(None, filings, TODAY)["n"] == 0


def test_commentary_survives_a_partial_filings_block():
    # Regression: a payload whose counts dict is incomplete used to raise a
    # KeyError out of the commentary and blank the whole panel.
    p = ev.compute("XYZ", chain_json=None,
                   snapshot={"earnings_raw": "Aug 26 AMC"},
                   filings={"available": True, "counts": {}, "recent": []},
                   now=NOW)
    assert p["commentary"]["headline"]
