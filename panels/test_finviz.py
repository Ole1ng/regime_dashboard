"""Tests for the Finviz quote scrape and its parse helpers.

This is the most breakage-prone module in Tab 2 — it parses HTML from a site
with no API contract, and `finvizfinance` already broke against the same page.
Two classes of regression are guarded here:

  * **The glued-cell problem.** Finviz packs two values into one `<td>` as
    sibling `<span>`s. Without a separator they concatenate into unparseable
    strings ('164.0737.23%'), so every value below is checked in its real,
    separator-joined form.
  * **The six-table problem.** The snapshot is split across SIX
    `table.snapshot-table2` elements. `select_one` returns 14 pairs and drops
    every short-interest and ownership field *without raising*, which is
    exactly the kind of silent failure a test has to catch.

    pytest panels/test_finviz.py
"""

from __future__ import annotations

from datetime import date

from panels import _finviz as fv

SEP = fv.SEP


def close(a, b, tol=1e-9):
    """Percent-to-fraction division is not exact (37.23/100 != 0.3723)."""
    return a is not None and abs(a - b) < tol


# --------------------------------------------------------------------------- #
# Scalar parsing
# --------------------------------------------------------------------------- #

def test_clean_treats_finviz_missing_markers_as_none():
    for raw in ("-", "", "--", "N/A", None):
        assert fv.clean(raw) is None
    assert fv.clean("  33.93%  ") == "33.93%"


def test_num_handles_suffixes_commas_and_symbols():
    assert fv.num("1.97") == 1.97
    assert fv.num("33.93%") == 33.93
    assert fv.num("292.67M") == 292_670_000
    assert fv.num("1.65B") == 1_650_000_000
    assert fv.num("5448.87B") == 5_448_870_000_000
    assert fv.num("17.14K") == 17_140
    assert fv.num("7,164,622") == 7_164_622
    assert fv.num("-1.27%") == -1.27
    assert fv.num("-") is None
    assert fv.num("Yes / Yes") is None


def test_pct_returns_a_fraction_not_a_percentage():
    # Every downstream consumer formats with pct()/signed(), which expect
    # fractions. Returning 33.93 here would render as 3393%.
    assert close(fv.pct("33.93%"), 0.3393)
    assert close(fv.pct("-1.27%"), -0.0127)
    assert fv.pct("-") is None


def test_glued_cells_split_on_the_separator():
    # These are the exact strings the live page produces, captured from NVDA
    # and WEN. '164.07' + '37.23%' has NO sign or punctuation between the two
    # halves, so a regex over the un-separated text is ambiguous — the
    # separator is what makes this parseable at all.
    high = f"236.54{SEP}-4.81%"
    low = f"164.07{SEP}37.23%"

    assert fv.first(high) == "236.54"
    assert fv.num(high) == 236.54
    assert close(fv.pct2(high), -0.0481)

    assert fv.first(low) == "164.07"
    assert fv.num(low) == 164.07
    assert close(fv.pct2(low), 0.3723)


def test_space_separated_cells_take_the_leading_value():
    # 'Volatility' is two space-separated percentages (week, month) rather than
    # sibling spans, so num() must not choke on the trailing token.
    assert fv.num("6.71% 5.22%") == 6.71
    assert fv.second("6.71% 5.22%") == "5.22%"
    # 'Dividend TTM' -> '0.56 (6.48%)'
    assert fv.num("0.56 (6.48%)") == 0.56


def test_double_negative_glued_cell_does_not_crash():
    # 'EPS past 3/5Y' comes back as '-' + '-0.96%': a missing first half.
    raw = f"-{SEP}-0.96%"
    assert fv.first(raw) is None
    assert fv.num(raw) is None
    assert close(fv.pct2(raw), -0.0096)


# --------------------------------------------------------------------------- #
# Page parsing
# --------------------------------------------------------------------------- #

def _snapshot_html(n_tables: int = 6, rows_per_table: int = 7) -> str:
    """Build a page whose snapshot is split across `n_tables` tables."""
    tables = []
    field = 0
    for _ in range(n_tables):
        cells = []
        for _ in range(rows_per_table):
            cells.append(f"<td>Field{field}</td><td>{field}.00</td>")
            field += 1
        tables.append(
            '<table class="snapshot-table2"><tr>' + "".join(cells) + "</tr></table>")
    return ("<html><head><title>WEN - Wendy's Co Stock Price and Quote</title>"
            "</head><body>" + "".join(tables) + "</body></html>")


def test_parse_reads_every_snapshot_table_not_just_the_first():
    # The regression guard: six tables x 7 pairs = 42 fields. A `select_one`
    # implementation would return 7 and raise nothing.
    parsed = fv.parse_quote(_snapshot_html(), "WEN")
    assert len(parsed["snapshot"]) == 42
    assert "Field41" in parsed["snapshot"]


def test_parse_raises_parse_changed_when_the_layout_shrinks():
    # One table's worth of fields means Finviz moved things; that must be a
    # loud, distinguishable failure rather than a panel full of Nones.
    try:
        fv.parse_quote(_snapshot_html(n_tables=1), "WEN")
    except fv.ParseChanged as exc:
        assert "expected" in str(exc)
    else:
        raise AssertionError("expected ParseChanged for a one-table page")


def test_parse_raises_not_found_when_there_is_no_snapshot_at_all():
    try:
        fv.parse_quote("<html><body><p>nothing here</p></body></html>", "ZZZZ")
    except fv.NotFound:
        pass
    else:
        raise AssertionError("expected NotFound when no snapshot table exists")


def test_company_name_comes_from_the_title():
    parsed = fv.parse_quote(_snapshot_html(), "WEN")
    assert parsed["company"] == "Wendy's Co"


def test_company_name_is_none_rather_than_guessed():
    html = ('<html><head><title>Finviz</title></head><body>'
            + _snapshot_html().split("<body>")[1])
    assert fv.parse_quote(html, "WEN")["company"] is None


# --------------------------------------------------------------------------- #
# News table
# --------------------------------------------------------------------------- #

_NEWS_HTML = """
<table id="news-table">
  <tr><td align="right">Aug-14-26 03:15PM</td>
      <td><a class="tab-link-news" href="https://x.test/1">First story</a>
          <span>(Moneywise)</span></td></tr>
  <tr><td align="right">09:30AM</td>
      <td><a class="tab-link-news" href="https://x.test/2">Same day, time only</a>
          <span>(FastCompany)</span></td></tr>
  <tr><td align="right">Aug-13-26 12:59PM</td>
      <td><a class="tab-link-news" href="https://x.test/3">Previous day</a>
          <span>(CRE Daily)</span></td></tr>
</table>
"""


def test_news_rows_carry_the_date_forward():
    # Finviz prints the full stamp only on the first row of each day; later
    # rows carry a bare time and inherit the date above them.
    from bs4 import BeautifulSoup
    rows = fv._news_rows(BeautifulSoup(_NEWS_HTML, "html.parser"),
                         today=date(2026, 8, 15))
    assert len(rows) == 3
    assert [r["source"] for r in rows] == ["Moneywise", "FastCompany", "CRE Daily"]

    first, second, third = rows
    from datetime import datetime
    assert datetime.fromtimestamp(first["published"]).date() == date(2026, 8, 14)
    # Inherits 14 Aug from the row above rather than defaulting to today.
    assert datetime.fromtimestamp(second["published"]).date() == date(2026, 8, 14)
    assert datetime.fromtimestamp(third["published"]).date() == date(2026, 8, 13)
    # ...and the bare-time row is EARLIER in the day than the stamped one.
    assert second["published"] < first["published"]


def test_news_rows_skip_entries_without_a_link():
    from bs4 import BeautifulSoup
    html = ('<table id="news-table"><tr><td align="right">Aug-14-26 03:15PM</td>'
            '<td>no anchor here</td></tr></table>')
    rows = fv._news_rows(BeautifulSoup(html, "html.parser"), today=date(2026, 8, 15))
    assert rows == []
