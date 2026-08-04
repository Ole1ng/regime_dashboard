"""Tests for the implied correlation panel.

The load-bearing behaviour is that the interpretation is *inverted*: a low
percentile must read as fragility, not calm (RESEARCH.md §5). Several tests
exist purely to stop someone "fixing" that later.

    pytest panels/test_correlation.py
"""

from __future__ import annotations

from datetime import date, timedelta

from panels import correlation as co


def hist(values, start=date(2020, 1, 1)):
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


# A synthetic history spanning 5..40. It repeats a 100-point sweep rather than
# ramping once, so that *every* trailing window (2y, 5y, all) sees the same
# roughly-uniform distribution and percentiles are easy to reason about. A
# single monotonic ramp would make the 2y window cover only the top of the
# range, which is a property of the fixture rather than of the code.
_SWEEP = [5.0 + 35.0 * j / 99 for j in range(100)]
WIDE = hist(_SWEEP * 10)


def q(value, prev=None):
    return {"symbol": "^COR1M", "value": value, "prev_close": prev,
            "open": None, "high": None, "low": None}


# --------------------------------------------------------------------------- #
# Percentile mechanics
# --------------------------------------------------------------------------- #

def test_percentile_rank_basics():
    vals = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert co.percentile_rank(vals, 0) == 0.0
    assert co.percentile_rank(vals, 11) == 100.0
    assert co.percentile_rank(vals, 5.5) == 50.0
    assert co.percentile_rank([], 5) is None


def test_percentile_windows_differ():
    """A value cheap against 2 years can be mid-range against the full series."""
    recent_low = hist([30.0] * 500 + [8.0] * 500)
    p = co.compute(q(9.0), q(12.0), recent_low)
    assert p["cor1m_pctl_2y"] is not None
    assert p["cor1m_pctl_all"] is not None
    # The last 2y window is all 8.0, so 9.0 ranks at the top of it...
    assert p["cor1m_pctl_2y"] > 90
    # ...but only halfway through the whole series.
    assert 40 < p["cor1m_pctl_all"] < 60


def test_history_window_sizes():
    p = co.compute(q(20.0), q(22.0), WIDE)
    assert p["history_days"] == 1000
    assert p["history_start"] == "2020-01-01"
    assert p["low_2y"] is not None and p["high_2y"] is not None
    assert p["low_2y"] <= 20.0 <= p["high_2y"]


# --------------------------------------------------------------------------- #
# The inverted interpretation
# --------------------------------------------------------------------------- #

def test_low_percentile_reads_as_fragility_not_calm():
    p = co.compute(q(6.0), q(9.0), WIDE)
    assert p["cor1m_pctl_2y"] < co.PCTL_EXTREME_LOW
    assert p["regime"] == "crowded dispersion"
    assert p["flags"]["extreme_low"] is True
    text = " ".join(p["commentary"]["sentences"]).lower()
    assert "cheap" in text
    assert "not because risk is low" in text
    # Must never describe a low reading as calm/benign.
    assert "calm because correlation is suppressing it" in text


def test_high_percentile_reads_as_macro_driven():
    p = co.compute(q(39.0), q(35.0), WIDE)
    assert p["regime"] == "macro shock"
    assert p["flags"]["extreme_high"] is True
    text = " ".join(p["commentary"]["sentences"]).lower()
    assert "moving together" in text


def test_midrange_is_neutral():
    p = co.compute(q(22.5), q(24.0), WIDE)
    assert p["regime"] == "neutral"
    assert p["flags"]["low"] is False and p["flags"]["high"] is False


# --------------------------------------------------------------------------- #
# The unwind trigger
# --------------------------------------------------------------------------- #

def test_spike_from_the_lows_is_flagged():
    """The live 2026-08-04 configuration: 6.62 from 5.64, a 17% jump off a
    record-low base — the dispersion unwind starting."""
    p = co.compute(q(6.62, prev=5.64), q(9.28), WIDE)
    assert p["cor1m_change_pct"] > co.SPIKE_PCT
    assert p["flags"]["low"] is True
    assert p["flags"]["spiking_from_lows"] is True
    text = " ".join(p["commentary"]["sentences"])
    assert "dispersion unwind starting" in text
    assert "buy index vol back" in text


def test_no_spike_flag_without_a_low_base():
    """The same percentage jump from mid-range is not the unwind signal."""
    p = co.compute(q(26.0, prev=22.0), q(24.0), WIDE)
    assert p["cor1m_change_pct"] > co.SPIKE_PCT
    assert p["flags"]["spiking_from_lows"] is False


def test_no_spike_flag_without_a_move():
    p = co.compute(q(6.0, prev=5.98), q(9.0), WIDE)
    assert p["flags"]["low"] is True
    assert p["flags"]["spiking_from_lows"] is False
    # Instead it should advise watching for the first uptick.
    assert any("first sharp uptick" in s for s in p["commentary"]["sentences"])


def test_missing_prev_close_does_not_crash():
    p = co.compute(q(6.0, prev=None), q(9.0), WIDE)
    assert p["cor1m_change"] is None
    assert p["cor1m_change_pct"] is None
    assert p["flags"]["spiking_from_lows"] is False


# --------------------------------------------------------------------------- #
# Term structure of correlation
# --------------------------------------------------------------------------- #

def test_term_inversion_flag():
    inverted = co.compute(q(30.0), q(25.0), WIDE)
    assert inverted["flags"]["term_inverted"] is True
    assert inverted["spread"] == 5.0
    assert any("stress is being priced now" in s
               for s in inverted["commentary"]["sentences"])

    normal = co.compute(q(20.0), q(25.0), WIDE)
    assert normal["flags"]["term_inverted"] is False
    assert normal["spread"] == -5.0
    assert any("normal ordering" in s for s in normal["commentary"]["sentences"])


# --------------------------------------------------------------------------- #
# Payload shape
# --------------------------------------------------------------------------- #

def test_spark_series_is_capped_and_ordered():
    p = co.compute(q(20.0), q(22.0), WIDE)
    assert len(p["spark"]) == co.SPARK_DAYS
    dates = [s["date"] for s in p["spark"]]
    assert dates == sorted(dates)
    assert p["spark"][-1]["date"] == p["history_end"]


def test_cor3m_percentile_is_optional():
    with_3m = co.compute(q(20.0), q(22.0), WIDE, WIDE)
    assert with_3m["cor3m_pctl_2y"] is not None
    without = co.compute(q(20.0), q(22.0), WIDE, None)
    assert without["cor3m_pctl_2y"] is None


def test_headline_carries_level_and_percentile():
    p = co.compute(q(6.62, prev=5.64), q(9.28), WIDE)
    assert "COR1M 6.62" in p["commentary"]["headline"]
    assert "pctl" in p["commentary"]["headline"]


def test_ordinal_suffixes():
    """Live COR1M sat in the 1st percentile, which was rendering as '1th'."""
    assert co.ordinal(1) == "1st"
    assert co.ordinal(2) == "2nd"
    assert co.ordinal(3) == "3rd"
    assert co.ordinal(4) == "4th"
    assert co.ordinal(0.7) == "1st"
    assert co.ordinal(11) == "11th"
    assert co.ordinal(12) == "12th"
    assert co.ordinal(13) == "13th"
    assert co.ordinal(21) == "21st"
    assert co.ordinal(100) == "100th"


def test_history_parser_handles_cboe_csv():
    import io
    csv_text = ("DATE,OPEN,HIGH,LOW,CLOSE\n"
                "01/03/2006,23.500000,23.500000,23.500000,23.500000\n"
                "01/04/2006,24.330000,24.330000,24.330000,24.330000\n"
                "bad,row,,,\n"
                "01/05/2006,,,,\n")

    class FakeResp:
        text = csv_text
        status_code = 200

        def raise_for_status(self):
            pass

    import panels.correlation as mod
    real = mod.requests.get
    mod.requests.get = lambda *a, **k: FakeResp()
    try:
        rows = mod.fetch_history("COR1M")
    finally:
        mod.requests.get = real
    assert rows == [(date(2006, 1, 3), 23.5), (date(2006, 1, 4), 24.33)]
    assert io  # silence lint
