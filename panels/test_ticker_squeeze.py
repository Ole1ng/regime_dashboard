"""Tests for the squeeze / ownership / analyst panel.

Mostly a check that the Finviz snapshot is mapped onto the payload with the
right units — every percentage must come out as a FRACTION, because the
frontend formats with pct()/signed(). A stray 33.93 where 0.3393 belongs
renders as 3393%.

    pytest panels/test_ticker_squeeze.py
"""

from __future__ import annotations

from panels import _finviz as fv
from panels import ticker_squeeze as sq

SEP = fv.SEP


def quote(**over):
    """A Finviz-shaped quote dict using the site's real value formats."""
    snapshot = {
        "Price": "8.64", "Short Float": "33.93%", "Short Ratio": "3.43",
        "Short Interest": "58.82M", "Shs Float": "173.36M",
        "Inst Own": "98.71%", "Inst Trans": "-1.27%",
        "Insider Own": "9.08%", "Insider Trans": "0.00%",
        "Recom": "2.89", "Target Price": "7.79",
        "RSI (14)": "62.40", "Rel Volume": "0.42", "Avg Volume": "17.14M",
        "Beta": "0.42", "ATR (14)": "0.46", "Volatility": "6.71% 5.22%",
        "SMA20": "12.59%", "SMA50": "15.58%", "SMA200": "12.20%",
        "52W High": f"10.84{SEP}-20.26%", "52W Low": f"6.07{SEP}42.34%",
        "Perf Week": "12.35%", "Perf Month": "10.34%",
        "Perf Quarter": "6.54%", "Perf YTD": "3.72%",
        "Market Cap": "1.65B", "Earnings": "Aug 07 BMO",
        "Option/Short": "Yes / Yes", "Index": "RUT",
        "Dividend TTM": "0.56 (6.48%)",
    }
    snapshot.update(over.pop("snapshot", {}))
    base = {"symbol": "WEN", "company": "Wendy's Co", "snapshot": snapshot,
            "news": []}
    base.update(over)
    return base


def close(a, b, tol=1e-9):
    return a is not None and abs(a - b) < tol


# --------------------------------------------------------------------------- #
# Units
# --------------------------------------------------------------------------- #

def test_percentages_are_stored_as_fractions():
    p = sq.compute(quote(), "WEN")
    assert close(p["short_float"], 0.3393)
    assert close(p["inst_own"], 0.9871)
    assert close(p["inst_trans"], -0.0127)
    assert close(p["sma200"], 0.1220)
    assert close(p["perf_month"], 0.1034)
    assert close(p["from_high"], -0.2026)
    assert close(p["from_low"], 0.4234)


def test_counts_and_prices_are_absolute_numbers():
    p = sq.compute(quote(), "WEN")
    assert p["spot"] == 8.64
    assert p["short_interest"] == 58_820_000
    assert p["float_shares"] == 173_360_000
    assert p["market_cap"] == 1_650_000_000
    assert p["days_to_cover"] == 3.43
    assert p["high_52w"] == 10.84 and p["low_52w"] == 6.07


def test_volatility_splits_week_and_month():
    p = sq.compute(quote(), "WEN")
    assert close(p["volatility_week"], 0.0671)
    assert close(p["volatility_month"], 0.0522)


def test_dividend_yield_is_extracted_for_the_pricing_model():
    # The generic positioning module assumes a zero yield; this is the value
    # that can be threaded in for a payer.
    p = sq.compute(quote(), "WEN")
    assert close(p["dividend_yield"], 0.0648)


def test_option_short_field_is_split():
    assert sq.compute(quote(), "WEN")["shortable"] is True
    q = quote(snapshot={"Option/Short": "Yes / No"})
    assert sq.compute(q, "WEN")["shortable"] is False


# --------------------------------------------------------------------------- #
# Derived readings
# --------------------------------------------------------------------------- #

def test_analyst_upside_is_negative_when_the_target_sits_below_spot():
    p = sq.compute(quote(), "WEN")
    assert p["target_upside"] < 0
    assert close(p["target_upside"], 7.79 / 8.64 - 1)
    assert p["recom_label"] == "Hold"


def test_recom_scale_is_inverted():
    # Finviz runs 1 = Strong Buy .. 5 = Strong Sell.
    for value, label in [("1.10", "Strong Buy"), ("2.10", "Buy"),
                         ("3.00", "Hold"), ("4.00", "Sell"),
                         ("4.80", "Strong Sell")]:
        p = sq.compute(quote(snapshot={"Recom": value}), "WEN")
        assert p["recom_label"] == label, (value, p["recom_label"])


def test_squeeze_bands():
    for short_float, band in [("33.93%", "extreme"), ("12.0%", "high"),
                              ("7.0%", "moderate"), ("3.0%", "low"),
                              ("0.5%", "negligible")]:
        p = sq.compute(quote(snapshot={"Short Float": short_float}), "WEN")
        assert p["squeeze_band"] == band, (short_float, p["squeeze_band"])


def test_squeeze_score_rises_with_short_float_and_days_to_cover():
    low = sq.compute(quote(snapshot={"Short Float": "1.0%",
                                     "Short Ratio": "0.5"}), "WEN")
    high = sq.compute(quote(snapshot={"Short Float": "35.0%",
                                      "Short Ratio": "8.0"}), "WEN")
    assert high["squeeze_score"] > low["squeeze_score"]
    assert 0 <= low["squeeze_score"] <= 100
    assert 0 <= high["squeeze_score"] <= 100


def test_trend_state_from_the_three_moving_averages():
    up = sq.compute(quote(), "WEN")                      # all three positive
    assert up["trend"]["state"] == "up"

    down = sq.compute(quote(snapshot={"SMA20": "-5.0%", "SMA50": "-8.0%",
                                      "SMA200": "-12.0%"}), "WEN")
    assert down["trend"]["state"] == "down"

    mixed = sq.compute(quote(snapshot={"SMA20": "5.0%", "SMA50": "-8.0%",
                                       "SMA200": "-12.0%"}), "WEN")
    assert mixed["trend"]["state"] == "mixed"


# --------------------------------------------------------------------------- #
# Missing data
# --------------------------------------------------------------------------- #

def test_missing_fields_become_none_not_zero():
    # Finviz writes "-" for unavailable values; treating that as 0 would show
    # a real reading where there is none.
    p = sq.compute(quote(snapshot={"Short Float": "-", "Target Price": "-",
                                   "Recom": "-", "RSI (14)": "-"}), "WEN")
    assert p["short_float"] is None
    assert p["target_price"] is None
    assert p["target_upside"] is None
    assert p["recom"] is None and p["recom_label"] is None
    assert p["rsi"] is None
    assert p["commentary"]["headline"]


def test_commentary_uses_the_correct_article_for_vowel_bands():
    p = sq.compute(quote(), "WEN")
    joined = " ".join(p["commentary"]["sentences"])
    assert "an extreme short base" in joined
    assert "a extreme" not in joined


def test_commentary_frames_short_interest_as_fuel_not_direction():
    joined = " ".join(sq.compute(quote(), "WEN")["commentary"]["sentences"])
    assert "either direction" in joined
