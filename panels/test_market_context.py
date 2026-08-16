"""Tests for the Tab 3 quote strips.

Two things matter here and neither is the happy path:

  * **Yields move in basis points, everything else in percent.** A 10-year going
    4.641 -> 4.696 is "+5.5bp", not "+1.18%". Both render without error, and only
    one is right — so the formatting is asserted, not eyeballed.
  * **This module must never fail the tab.** Missing symbols, a dead download and
    a zero denominator all have to degrade to fewer rows rather than an
    exception, because the panels' actual content is the headlines.

    pytest panels/test_market_context.py
"""

from __future__ import annotations

import pytest

from panels import market_context as mc
from panels._feeds import PANEL_KEYS


# Two closes per symbol: (last, previous).
CLOSES = {
    "^IRX": (3.697, 3.705),
    "^FVX": (4.362, 4.313),
    "^TNX": (4.696, 4.641),
    "^TYX": (5.265, 5.213),
    "HYG": (79.71, 79.79),
    "CL=F": (82.40, 81.25),
    "BZ=F": (88.52, 87.07),
    "NG=F": (2.733, 2.727),
    "RB=F": (3.1841, 3.128),
    "GC=F": (4380.40, 4363.60),
    "SI=F": (64.988, 64.873),
    "HG=F": (6.5995, 6.5925),
    "^GSPC": (7785.76, 7798.99),
    "DX-Y.NYB": (99.67, 99.96),
    "^VIX": (14.25, 14.63),
}


def rows(panel, closes=None):
    return {r["label"]: r for r in mc.build(closes or CLOSES)[panel]}


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #

def test_yields_are_quoted_in_percent_and_move_in_basis_points():
    row = rows("t3_us_rates")["UST 10Y"]
    assert row["display"] == "4.696%"
    assert row["delta"] == "+5.5bp"
    assert row["dir"] == 1


def test_a_falling_yield_reads_negative_basis_points():
    row = rows("t3_us_rates")["UST 3M"]
    assert row["delta"] == "-0.8bp"
    assert row["dir"] == -1


def test_tnx_is_a_direct_percentage_not_the_old_times_ten_form():
    """4.696 means 4.696%. Treating it as x10 gives a 47% ten-year."""
    row = rows("t3_us_rates")["UST 10Y"]
    assert 0 < row["value"] < 25


def test_prices_move_in_percent():
    row = rows("t3_energy")["WTI"]
    assert row["display"] == "$82.40"
    assert row["delta"] == "+1.42%"


def test_index_levels_keep_cents_below_a_thousand_and_drop_them_above():
    # A $79 ETF needs its cents; a 7,785 index level does not.
    assert rows("t3_us_rates")["HY credit (HYG)"]["display"] == "79.71"
    assert rows("t3_us_equities")["S&P 500"]["display"] == "7,786"


def test_unchanged_reads_as_zero_direction():
    flat = dict(CLOSES, **{"^TNX": (4.696, 4.696)})
    assert rows("t3_us_rates", flat)["UST 10Y"]["dir"] == 0


# --------------------------------------------------------------------------- #
# Derived rows
# --------------------------------------------------------------------------- #

def test_curve_is_a_yield_difference_in_basis_points():
    row = rows("t3_us_rates")["Curve 3M-10Y"]
    # 4.696 - 3.697 = 0.999 -> +100bp
    assert row["display"] == "+100bp"
    # Steepened: 10y +5.5bp against 3m -0.8bp.
    assert row["delta"] == "+6.3bp"
    assert row["dir"] == 1


def test_curve_is_labelled_3m_10y_because_yahoo_has_no_two_year():
    """Calling it 2s10s would be quietly wrong on the number rates readers
    check first."""
    labels = [r["label"] for r in mc.build(CLOSES)["t3_us_rates"]]
    assert "Curve 3M-10Y" in labels
    assert not any("2s10s" in lbl for lbl in labels)


def test_spread_is_a_dollar_difference():
    row = rows("t3_energy")["Brent-WTI"]
    assert row["display"] == "$6.12"


def test_ratio_delta_is_a_percentage_not_an_absolute_difference():
    """Copper/gold sits near 0.0015, where a real move rounds to '+0.0000'."""
    row = rows("t3_metals")["Copper/Gold"]
    assert row["delta"].endswith("%")
    assert "0.0000" not in row["delta"]


def test_gold_silver_ratio_is_readable():
    row = rows("t3_precious")["Gold/Silver"]
    assert row["display"] == "67.4"


def test_a_derived_row_needs_both_legs():
    without_gold = {k: v for k, v in CLOSES.items() if k != "GC=F"}
    labels = [r["label"] for r in mc.build(without_gold)["t3_metals"]]
    assert "Copper/Gold" not in labels
    # The panel still renders its direct quotes.
    assert "Copper" in labels


def test_zero_denominator_drops_the_row_rather_than_raising():
    broken = dict(CLOSES, **{"SI=F": (0.0, 0.0)})
    labels = [r["label"] for r in mc.build(broken)["t3_precious"]]
    assert "Gold/Silver" not in labels
    assert "Gold" in labels


# --------------------------------------------------------------------------- #
# Degradation
# --------------------------------------------------------------------------- #

def test_every_panel_gets_a_list_even_with_no_data():
    built = mc.build({})
    assert set(built) == set(PANEL_KEYS)
    assert all(v == [] for v in built.values())


def test_missing_symbols_shrink_the_strip_rather_than_breaking_it():
    partial = {"^TNX": CLOSES["^TNX"]}
    built = mc.build(partial)["t3_us_rates"]
    assert [r["label"] for r in built] == ["UST 10Y"]


def test_a_failed_download_yields_empty_quotes(monkeypatch):
    monkeypatch.setattr(mc, "fetch", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("network down")))
    built = mc.build()
    assert all(v == [] for v in built.values())


def test_fetch_returns_empty_when_yfinance_raises(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "yfinance":
            raise ImportError("no yfinance")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert mc.fetch(["^TNX"]) == {}


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

def test_specs_cover_exactly_the_screener_panels():
    assert set(mc.SPECS) == set(PANEL_KEYS)


def test_derived_rows_name_known_panels():
    for panel, _label, _a, _b, mode in mc.DERIVED:
        assert panel in mc.SPECS
        assert mode in ("curve", "spread", "ratio")


def test_all_symbols_includes_derived_only_symbols():
    """The metals panel needs gold for copper/gold but never quotes it."""
    symbols = mc._all_symbols()
    assert "GC=F" in symbols
    assert len(symbols) == len(set(symbols)), "duplicate symbols in the download"


@pytest.mark.parametrize("panel", list(PANEL_KEYS))
def test_every_panel_has_at_least_two_quote_rows(panel):
    assert len(mc.SPECS[panel]) >= 2
