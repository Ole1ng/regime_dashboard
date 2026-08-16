"""Tests for the Tab 3 directional layer.

The load-bearing property is the one the whole module exists for: **a headline's
direction for an asset is not its tone**, and where the two disagree the
directional read must follow the asset, not the language.

Every case in `test_tone_and_direction_disagree_as_designed` is a real headline
shape that plain VADER gets backwards. If any of them starts agreeing with tone,
the directional layer has been disconnected — which would be invisible in the
UI, because the panel would still render two plausible-looking numbers.

    pytest panels/test_asset_read.py
"""

from __future__ import annotations

import pytest

from panels import _asset_read as ar
from panels import _sentiment_util as su
from panels._feeds import PANEL_KEYS


# --------------------------------------------------------------------------- #
# The point of the module
# --------------------------------------------------------------------------- #

# (panel, headline, expected direction sign for the panel's asset)
DISAGREEMENTS = [
    # Rising yields are bearish bonds; "higher" is not a negative word.
    ("t3_us_rates", "10-year Treasury yield inches higher as traders digest data", -1),
    ("t3_us_rates", "Treasury yields tumble on haven bid after weak data", +1),
    ("t3_us_rates", "Credit spreads widen as risk appetite fades", -1),
    # "cools", "fall" and "cut" are negative words describing good news.
    ("t3_us_macro", "Inflation cools more than expected in July", +1),
    ("t3_us_macro", "Jobless claims fall to lowest level since mid-May", +1),
    ("t3_precious", "Gold rises on weaker dollar as rate-cut bets firm", +1),
    # "cuts" is negative; an OPEC output cut is bullish crude.
    ("t3_energy", "OPEC+ agrees deeper output cuts, crude surges", +1),
    # "halts" and "strike" are negative; a supply outage is bullish the metal.
    ("t3_metals", "Copper surges as Chilean mine strike halts output", +1),
]


@pytest.mark.parametrize("panel,headline,expected", DISAGREEMENTS)
def test_direction_follows_the_asset_not_the_language(panel, headline, expected):
    score = ar.asset_read(headline, panel)
    assert score is not None, f"no rule fired on {headline!r}"
    assert score * expected > 0, (
        f"{headline!r} read {score:+.2f} in {panel}, expected sign {expected:+d}")


def test_tone_and_direction_disagree_as_designed():
    """At least half these cases must have tone pointing the wrong way.

    This is the assertion that proves the two readings are independent. If tone
    agreed with direction everywhere, the second reading would be decoration.
    """
    wrong_way = 0
    for panel, headline, expected in DISAGREEMENTS:
        tone = su.score(headline, su.MACRO)
        if tone * expected <= 0:
            wrong_way += 1
    assert wrong_way >= len(DISAGREEMENTS) // 2, (
        f"only {wrong_way}/{len(DISAGREEMENTS)} headlines have tone pointing "
        f"against the asset — these cases no longer test anything")


def test_same_verb_means_opposite_things_in_different_panels():
    """One rise, two panels, two signs. The core of the per-panel design."""
    rates = ar.asset_read("Yields surge after hot inflation print", "t3_us_rates")
    energy = ar.asset_read("Crude surges after supply outage", "t3_energy")
    assert rates is not None and energy is not None
    assert rates < 0 < energy


# --------------------------------------------------------------------------- #
# None is not zero
# --------------------------------------------------------------------------- #

def test_undirected_headline_returns_none_not_zero():
    """A headline saying nothing about the asset must not vote as neutral.

    Counting these as 0.0 would drag every panel toward the middle in
    proportion to how much off-topic news happened to be routed to it, making a
    strong reading arithmetically impossible.
    """
    for headline in ("Berkshire adds $17 billion to Alphabet stake",
                     "Lamborghini unveils its most powerful production car",
                     "Bonds Become Really Difficult to Trade as Correlations Unwind"):
        assert ar.asset_read(headline, "t3_us_equities") is None


def test_empty_and_missing_titles_are_safe():
    assert ar.asset_read("", "t3_us_macro") is None
    assert ar.asset_read(None, "t3_us_macro") is None


def test_panel_read_ignores_undirected_headlines_in_the_mean():
    directional = "Crude surges as OPEC+ agrees deeper output cuts"
    noise = ["Lamborghini unveils a new car"] * 9

    alone = ar.panel_read([directional], "t3_energy")
    diluted = ar.panel_read([directional] + noise, "t3_energy")

    # Same score — the nine undirected headlines do not average it down.
    assert alone["score"] == diluted["score"]
    # But coverage collapses, which is how the thinness is surfaced instead.
    assert alone["coverage"] == 1.0
    assert diluted["coverage"] == pytest.approx(0.1)
    assert diluted["thin"] is True


# --------------------------------------------------------------------------- #
# Subject masking
# --------------------------------------------------------------------------- #

def test_treasury_yields_is_one_subject_not_two_cancelling_ones():
    """"Treasury yields" must resolve as a yield, not a yield and a bond.

    Without span masking the yield pattern (-1) and the bond pattern (+1) both
    match the same words and cancel to exactly zero — a silent failure that
    reads on screen as "no view".
    """
    score = ar.asset_read("Treasury yields climb after strong data", "t3_us_rates")
    assert score is not None
    assert score < 0, "yields up must read bearish bonds, not neutral"


def test_bonds_without_the_word_yield_read_the_other_way():
    score = ar.asset_read("Treasuries rally as investors seek safety", "t3_us_rates")
    assert score is not None and score > 0


def test_widening_spreads_are_bearish_and_narrowing_bullish():
    wide = ar.asset_read("Credit spreads widen sharply", "t3_us_rates")
    tight = ar.asset_read("Credit spreads narrow as risk appetite returns",
                          "t3_us_rates")
    assert wide is not None and tight is not None
    assert wide < 0 < tight


# --------------------------------------------------------------------------- #
# Proximity pairing
# --------------------------------------------------------------------------- #

def test_verb_binds_to_the_nearer_subject():
    """"Gold rises as stocks tumble" must not read gold as falling."""
    assert ar.asset_read("Gold rises as stocks tumble", "t3_precious") > 0
    assert ar.asset_read("Gold slips as stocks rally", "t3_precious") < 0


def test_distant_verb_does_not_pair():
    # The mover sits well beyond PAIR_WINDOW of the subject, so nothing binds.
    far = ("Copper " + "x" * (ar.PAIR_WINDOW + 20) + " surges")
    assert ar.asset_read(far, "t3_metals") is None


# --------------------------------------------------------------------------- #
# Intensity
# --------------------------------------------------------------------------- #

def test_stronger_verbs_score_harder():
    mild = ar.asset_read("Crude edges higher", "t3_energy")
    normal = ar.asset_read("Crude rises", "t3_energy")
    strong = ar.asset_read("Crude soars", "t3_energy")
    assert 0 < mild < normal < strong


# --------------------------------------------------------------------------- #
# Two-sided prints
# --------------------------------------------------------------------------- #

def test_two_sided_print_is_damped_not_decided():
    """A hot jobs number is good growth and bad for cuts — so do not pick one."""
    two_sided = ar.asset_read(
        "Nonfarm payrolls beat expectations as hiring surges", "t3_us_macro")
    one_sided = ar.asset_read("Hiring surges across the economy", "t3_us_macro")
    assert two_sided is not None and one_sided is not None
    assert abs(two_sided) < abs(one_sided)


def test_panel_read_counts_two_sided_headlines():
    read = ar.panel_read(
        ["Nonfarm payrolls beat expectations as hiring surges"], "t3_us_macro")
    assert read["two_sided"] == 1


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #

def test_panel_read_with_no_headlines():
    read = ar.panel_read([], "t3_us_macro")
    assert read["score"] is None
    assert read["n"] == 0 and read["n_fired"] == 0
    assert read["coverage"] == 0.0
    assert read["thin"] is True
    assert read["label"] == "No directional read"


def test_panel_read_with_nothing_directional():
    read = ar.panel_read(["Lamborghini unveils a new car"] * 5, "t3_metals")
    assert read["score"] is None
    assert read["n"] == 5 and read["n_fired"] == 0


def test_panel_read_score_stays_in_range():
    headlines = ["Crude soars as OPEC+ agrees deeper output cuts and "
                 "sanctions bite amid a supply outage"] * 3
    read = ar.panel_read(headlines, "t3_energy")
    assert -1.0 <= read["score"] <= 1.0


def test_bull_and_bear_counts_partition_the_called_headlines():
    headlines = ["Crude soars on supply outage",
                 "Oil plunges on a glut and inventory build",
                 "Lamborghini unveils a new car"]
    read = ar.panel_read(headlines, "t3_energy")
    assert read["bull"] == 1 and read["bear"] == 1
    assert read["n_fired"] == 2 and read["n"] == 3


# --------------------------------------------------------------------------- #
# Labels
# --------------------------------------------------------------------------- #

def test_macro_panels_read_policy_not_price():
    """"Bullish the outlook" is the wrong register for a growth backdrop."""
    assert ar.label_for(0.8, "t3_us_macro") == "Supportive"
    assert ar.label_for(-0.8, "t3_eu_macro") == "Restrictive"
    assert ar.label_for(0.0, "t3_us_macro") == "Balanced"


def test_asset_panels_name_their_asset():
    assert ar.label_for(0.8, "t3_us_rates") == "Bullish bonds"
    assert ar.label_for(-0.8, "t3_energy") == "Bearish crude"
    assert "precious metals" in ar.label_for(0.0, "t3_precious")


def test_label_is_neutral_inside_the_call_cut():
    inside = ar.CALL_CUT / 2
    assert ar.label_for(inside, "t3_energy").startswith("Neutral")
    assert ar.label_for(-inside, "t3_energy").startswith("Neutral")


# --------------------------------------------------------------------------- #
# Tables stay in sync
# --------------------------------------------------------------------------- #

def test_every_panel_has_subjects_and_a_noun():
    # The module asserts this at import; re-check so the failure names the file
    # rather than surfacing as an ImportError somewhere unrelated.
    assert set(ar.SUBJECTS) == set(PANEL_KEYS)
    assert set(ar.ASSET_NOUN) == set(PANEL_KEYS)
    assert set(ar.DRIVERS) <= set(PANEL_KEYS)
    assert set(ar.TWO_SIDED) <= set(PANEL_KEYS)


def test_every_panel_can_produce_a_read():
    """No panel may be structurally incapable of firing — a typo'd subject
    pattern would otherwise show as a permanently undirected panel."""
    samples = {
        "t3_us_macro": "Inflation cools sharply",
        "t3_us_equities": "S&P 500 climbs to a record high",
        "t3_us_rates": "Treasury yields climb",
        "t3_eu_macro": "Euro zone inflation eases",
        "t3_eu_markets": "DAX rallies to a record high",
        "t3_energy": "Crude surges",
        "t3_precious": "Gold rises",
        "t3_metals": "Copper jumps",
    }
    for panel in PANEL_KEYS:
        assert ar.asset_read(samples[panel], panel) is not None, panel


def test_drivers_fired_names_the_rule():
    fired = ar.drivers_fired("OPEC+ agrees deeper output cuts", "t3_energy")
    assert "OPEC restraint" in fired


def test_top_drivers_counts_and_orders():
    headlines = ["OPEC+ agrees deeper output cuts"] * 3 + ["Oil glut deepens"]
    rows = ar.top_drivers(headlines, "t3_energy")
    assert rows[0]["driver"] == "OPEC restraint" and rows[0]["count"] == 3
