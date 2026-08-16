"""Tests for the Tab 3 screener: routing, windowing and payload shape.

Everything here is offline — `compute()` is pure, so the whole engine is
testable without touching a feed.

Two properties are load-bearing and easy to break silently:

  * **The recency window is the only guarantee this tab has.** Google ignores
    its own `when:` operator on keyword queries and cheerfully returns
    eleven-year-old articles, and two publisher feeds were found serving
    year-old headlines behind fresh-looking titles. If the window ever softens
    — admitting undated items, say — the panel keeps rendering and quietly
    stops being news.
  * **Routing must drop what it cannot place.** Filing an unmatched headline
    somewhere arbitrary is worse than dropping it, because a panel with wrong
    items still produces a confident-looking score.

    pytest panels/test_news_screener.py
"""

from __future__ import annotations

import time

import pytest

from panels import news_screener as ns
from panels._feeds import PANEL_KEYS

NOW = 1_760_000_000.0
HOUR = 3600.0


def item(title, published_h_ago=1.0, link=None, source="Reuters",
         panels=None, fallback=(), feed="Google News"):
    """One fetched item, aged in hours rather than epoch seconds."""
    return {
        "title": title,
        "link": link if link is not None else f"https://x.test/{abs(hash(title))}",
        "source": source,
        "publisher": source,
        "published": NOW - published_h_ago * HOUR,
        "feed": feed,
        "panels": panels,
        "fallback_panels": fallback,
    }


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("title,expected", [
    ("Fed holds rates steady, signals one cut this year", "t3_us_macro"),
    ("S&P 500 tumbles in broad AI rout", "t3_us_equities"),
    ("10-year Treasury yield inches higher", "t3_us_rates"),
    ("Euro zone inflation eases, ECB seen cutting", "t3_eu_macro"),
    ("DAX climbs to record high as autos rally", "t3_eu_markets"),
    ("OPEC+ agrees deeper output cuts", "t3_energy"),
    ("Gold rises on weaker dollar", "t3_precious"),
    ("Copper surges as Chilean mine strike halts output", "t3_metals"),
])
def test_headlines_route_to_their_panel(title, expected):
    assert expected in ns.classify(title)


def test_european_index_names_are_european_markers_on_their_own():
    """"DAX climbs to record high" carries no other European word.

    An earlier gate required an explicit Europe marker before allowing a
    Europe panel, which dropped exactly the headlines the panel exists for.
    """
    for title in ("DAX climbs to record high as autos rally",
                  "FTSE 100 logs first weekly drop in five",
                  "Bund yields rise after strong data"):
        assert ns.classify(title), f"{title!r} was dropped entirely"


def test_european_story_does_not_vote_in_the_us_panels():
    panels = ns.classify("Euro zone inflation eases, ECB seen cutting")
    assert "t3_eu_macro" in panels
    assert "t3_us_macro" not in panels


def test_us_story_with_a_european_mention_keeps_its_us_panels():
    # A US angle present alongside the European one: both may be relevant.
    panels = ns.classify("Fed and ECB diverge as US inflation cools")
    assert "t3_us_macro" in panels


def test_one_story_can_belong_to_several_panels():
    """An FOMC decision is genuinely US Macro and US Fixed Income."""
    panels = ns.classify(
        "Fed holds rates steady as Treasury yields slide on the decision")
    assert "t3_us_macro" in panels and "t3_us_rates" in panels


def test_unclassifiable_headline_is_dropped():
    assert ns.classify("Lamborghini unveils its most powerful production car") == ()


def test_fallback_only_applies_when_nothing_matched():
    off_topic = "Quarterly outlook published"
    assert ns.classify(off_topic, ("t3_energy",)) == ("t3_energy",)
    # A headline that classifies on its own ignores the fallback.
    assert ns.classify("Gold rises on haven demand", ("t3_energy",)) == ("t3_precious",)


def test_bare_bond_and_yield_route_to_fixed_income():
    """Without these the panel routed zero publisher stories."""
    assert "t3_us_rates" in ns.classify("Investors pile into long-dated bonds")
    assert "t3_us_rates" in ns.classify("Yields steady before the auction")


# --------------------------------------------------------------------------- #
# The recency window
# --------------------------------------------------------------------------- #

def test_items_outside_the_window_are_discarded():
    fresh = item("Crude surges on supply outage", 2, panels=("t3_energy",))
    stale = item("Oil plunges on a glut", ns.WINDOW_HOURS + 5,
                 panels=("t3_energy",))
    out = ns.compute([fresh, stale], now=NOW)
    titles = [i["title"] for i in out["t3_energy"]["items"]]
    assert fresh["title"] in titles
    assert stale["title"] not in titles


def test_undated_items_are_dropped_and_counted():
    """Admitting them would quietly break the one promise this tab makes."""
    dated = item("Crude surges on supply outage", 2, panels=("t3_energy",))
    undated = dict(item("Oil plunges on a glut", panels=("t3_energy",)),
                   published=None)
    out = ns.compute([dated, undated], now=NOW)
    assert out["t3_energy"]["count"] == 1
    assert out["t3_energy"]["undated_dropped"] == 1


def test_items_from_the_future_are_discarded():
    """A feed with a fast clock must not jump the queue."""
    future = item("Crude surges", -48, panels=("t3_energy",))  # 48h ahead
    out = ns.compute([future], now=NOW)
    assert out["t3_energy"]["empty"] is True


def test_only_the_newest_max_items_survive():
    items = [item(f"Crude surges on outage number {i}", i * 0.5,
                  panels=("t3_energy",))
             for i in range(ns.MAX_ITEMS * 2)]
    out = ns.compute(items, now=NOW)
    panel = out["t3_energy"]
    assert panel["count"] == ns.MAX_ITEMS
    # Newest first, and the tail is the oldest that fits.
    published = [i["published"] for i in panel["items"]]
    assert published == sorted(published, reverse=True)


# --------------------------------------------------------------------------- #
# Dedup
# --------------------------------------------------------------------------- #

def test_the_same_story_from_two_feeds_is_counted_once():
    a = item("Crude surges on supply outage", 1, link="https://a.test/1",
             source="Reuters", panels=("t3_energy",))
    b = item("Crude surges on supply outage!", 1, link="https://b.test/2",
             source="Bloomberg", panels=("t3_energy",))
    out = ns.compute([a, b], now=NOW)
    assert out["t3_energy"]["count"] == 1


def test_dedup_is_per_panel_not_global():
    """A cross-panel story must survive in both panels, not just the first."""
    story = item("Fed holds rates steady as Treasury yields slide", 1)
    out = ns.compute([story], now=NOW)
    assert out["t3_us_macro"]["count"] == 1
    assert out["t3_us_rates"]["count"] == 1


# --------------------------------------------------------------------------- #
# Payload shape
# --------------------------------------------------------------------------- #

def test_every_panel_is_present_even_with_no_items():
    out = ns.compute([], now=NOW)
    assert set(out) == set(PANEL_KEYS)
    for panel in PANEL_KEYS:
        payload = out[panel]
        assert payload["empty"] is True
        assert payload["count"] == 0
        assert payload["asset"]["score"] is None
        assert payload["commentary"]["headline"]
        assert payload["commentary"]["warnings"]


def test_payload_carries_both_readings_and_the_evidence():
    items = [item("Crude surges as OPEC+ agrees deeper output cuts", 1,
                  panels=("t3_energy",)),
             item("Oil plunges on a glut and inventory build", 2,
                  panels=("t3_energy",))]
    p = ns.compute(items, now=NOW)["t3_energy"]

    assert p["panel"] == "t3_energy" and p["title"] == "Oil & Energy"
    assert p["sentiment"]["n"] == 2
    assert p["asset"]["n"] == 2 and p["asset"]["n_fired"] == 2
    assert p["window_hours"] == ns.WINDOW_HOURS
    # Every item carries both scores so the table can show them side by side.
    for row in p["items"]:
        assert "compound" in row and "asset" in row and "drivers" in row


def test_salient_leads_with_the_directional_headlines():
    """Ranking by tone would bury the headline that actually says something."""
    items = [item("Analysts remain wonderful and delighted about the sector", 1,
                  panels=("t3_energy",)),
             item("Crude soars as OPEC+ agrees deeper output cuts", 2,
                  panels=("t3_energy",))]
    p = ns.compute(items, now=NOW)["t3_energy"]
    assert p["salient"][0]["title"].startswith("Crude soars")


def test_quotes_are_attached_per_panel():
    quotes = {"t3_energy": [{"label": "WTI", "display": "$82.40",
                             "delta": "+1.42%", "dir": 1, "value": 82.4,
                             "kind": "futures"}]}
    out = ns.compute([item("Crude surges", 1, panels=("t3_energy",))],
                     quotes=quotes, now=NOW)
    assert out["t3_energy"]["quotes"] == quotes["t3_energy"]
    assert out["t3_precious"]["quotes"] == []


def test_missing_quotes_warn_but_do_not_fail_the_panel():
    out = ns.compute([item("Crude surges", 1, panels=("t3_energy",))], now=NOW)
    warnings = " ".join(out["t3_energy"]["commentary"]["warnings"])
    assert "Market levels unavailable" in warnings
    assert out["t3_energy"]["count"] == 1


# --------------------------------------------------------------------------- #
# Divergence
# --------------------------------------------------------------------------- #

def test_divergence_is_flagged_when_tone_and_direction_disagree():
    # Negative language describing a bullish setup, repeated so both readings
    # are stable.
    items = [item(f"Gold rises on weaker dollar as rate-cut bets firm ({i})",
                  1, panels=("t3_precious",)) for i in range(4)]
    p = ns.compute(items, now=NOW)["t3_precious"]
    assert p["sentiment"]["mean"] < 0 < p["asset"]["score"]
    assert p["divergence"] is not None
    assert p["divergence"]["severity"] in ("medium", "high")
    assert p["divergence"]["gap"] >= ns.DIVERGENCE_CUT


def test_a_wide_gap_in_the_same_direction_is_not_a_divergence():
    """Mildly bearish language plus a firmly bearish direction is agreement.

    Flagging on gap alone lit the badge on seven of eight live panels, including
    tone -0.09 against asset -0.54, which is the same story told twice.
    """
    tone = {"mean": -0.089, "tone": "Bearish", "n": 25}
    asset = {"score": -0.54, "noun": "the outlook"}
    assert abs(tone["mean"] - asset["score"]) > ns.DIVERGENCE_CUT
    assert ns._divergence(tone, asset, "t3_us_macro") is None


def test_divergence_needs_both_readings_to_clear_their_thresholds():
    # Tone inside VADER's neutral band: no claim to contradict.
    flat_tone = {"mean": 0.01, "tone": "Mixed", "n": 20}
    assert ns._divergence(flat_tone, {"score": -0.9, "noun": "crude"},
                          "t3_energy") is None
    # Direction inside the call cut: not a call, so not a disagreement.
    weak_asset = {"score": ns.ar.CALL_CUT / 2, "noun": "crude"}
    assert ns._divergence({"mean": -0.8, "tone": "Bearish", "n": 20},
                          weak_asset, "t3_energy") is None


def test_divergence_sentence_matches_the_direction_it_describes():
    bullish = ns._divergence({"mean": -0.5, "tone": "Bearish", "n": 20},
                             {"score": 0.8, "noun": "gold"}, "t3_precious")
    assert "constructive for gold" in bullish["sentence"]

    bearish = ns._divergence({"mean": 0.5, "tone": "Bullish", "n": 20},
                             {"score": -0.8, "noun": "bonds"}, "t3_us_rates")
    assert "unhelpful for bonds" in bearish["sentence"]


def test_no_divergence_without_a_directional_read():
    items = [item("Lamborghini unveils a new car", 1, panels=("t3_energy",))]
    p = ns.compute(items, now=NOW)["t3_energy"]
    assert p["asset"]["score"] is None
    assert p["divergence"] is None


# --------------------------------------------------------------------------- #
# Degradation
# --------------------------------------------------------------------------- #

def test_feed_errors_are_surfaced_without_emptying_the_panels():
    items = [item("Crude surges on supply outage", 1, panels=("t3_energy",))]
    out = ns.compute(items, errors=["OilPrice: Timeout"], now=NOW)
    assert out["t3_energy"]["count"] == 1
    assert "OilPrice: Timeout" in out["t3_energy"]["errors"]
    assert any("feed(s) failed" in w
               for w in out["t3_energy"]["commentary"]["warnings"])


def test_thin_directional_coverage_is_warned_about():
    items = [item("Crude surges on supply outage", 1, panels=("t3_energy",))]
    items += [item(f"Lamborghini unveils car number {i}", 1,
                   panels=("t3_energy",)) for i in range(19)]
    p = ns.compute(items, now=NOW)["t3_energy"]
    assert p["asset"]["thin"] is True
    assert any("Directional coverage is thin" in w
               for w in p["commentary"]["warnings"])


def test_compute_defaults_to_wall_clock_without_a_now():
    """The default path must not silently discard everything."""
    fresh = item("Crude surges on supply outage", 0)
    fresh["published"] = time.time() - 600
    fresh["panels"] = ("t3_energy",)
    out = ns.compute([fresh])
    assert out["t3_energy"]["count"] == 1
