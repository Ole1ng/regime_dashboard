"""Tests for the regime classifier and confluence scorer.

Pure function, so every case is a hand-built payload set. The four regimes from
the RESEARCH.md table each get a fixture, plus the degradation behaviour when
inputs are missing.

    pytest panels/test_regime.py
"""

from __future__ import annotations

import copy

from panels import regime as rg


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def gamma(regime="positive", cushion_pct=0.024, spot=7700.0, zero=7520.0,
          charm=2.6e9, call_wall=8000.0, put_wall=7500.0, magnets=None):
    return {
        "spot": spot, "regime": regime, "zero_gamma": zero, "no_flip": False,
        "cushion": spot - zero, "cushion_pct": cushion_pct,
        "charm_drift": charm, "call_wall": call_wall, "put_wall": put_wall,
        "oi_magnets": magnets if magnets is not None
        else [{"strike": 7800.0, "oi": 50000}],
    }


def vix(structure="contango", inverted=False):
    return {"structure": structure,
            "flags": {"vix3m_inverted": inverted, "backwardation":
                      structure == "backwardation"}}


def corr(pctl=45.0, low=False, high=False, extreme_high=False, spiking=False,
         change_pct=1.0):
    return {"cor1m_pctl_2y": pctl, "cor1m_change_pct": change_pct,
            "flags": {"low": low, "high": high, "extreme_high": extreme_high,
                      "extreme_low": False, "spiking_from_lows": spiking}}


def cal(post_opex=False, days_to_opex=17, sessions_since=18):
    return {"post_opex_week": post_opex, "days_to_opex": days_to_opex,
            "sessions_since_opex": sessions_since}


def profile(poc=7600.0, vah=7750.0, val=7450.0, naked=None, lvn=None,
            ratio=10.02):
    return {"composite_poc_spx": poc, "composite_vah_spx": vah,
            "composite_val_spx": val, "ratio": ratio,
            "naked_pocs": naked if naked is not None else [],
            "lvn_zones": lvn if lvn is not None else []}


# --------------------------------------------------------------------------- #
# The four regimes
# --------------------------------------------------------------------------- #

def test_pin_and_grind():
    p = rg.compute({"gamma_spx": gamma(), "vix_structure": vix(),
                    "correlation": corr(), "calendar": cal()})
    assert p["regime"] == rg.PIN_GRIND
    assert p["label"] == "PIN & GRIND"
    assert p["confidence"] > 0.6
    assert "mechanically damped" in p["reason"]
    assert "Fade extremes" in p["posture"]


def test_unstable_pin_from_thin_cushion():
    p = rg.compute({"gamma_spx": gamma(cushion_pct=0.004), "vix_structure": vix(),
                    "correlation": corr(), "calendar": cal()})
    assert p["regime"] == rg.UNSTABLE_PIN
    assert "cushion to the flip is thin" in p["reason"]


def test_unstable_pin_from_correlation_spike():
    """The live 2026-08-04 configuration: positive gamma, contango, but COR1M
    jumping off record lows — the fragility RESEARCH.md §5 describes."""
    p = rg.compute({"gamma_spx": gamma(), "vix_structure": vix(),
                    "correlation": corr(pctl=5.0, low=True, spiking=True,
                                        change_pct=16.5),
                    "calendar": cal()})
    assert p["regime"] == rg.UNSTABLE_PIN
    assert "correlation is spiking off the lows" in p["reason"]
    assert "buy cheap wings" in p["posture"]


def test_unstable_pin_from_post_opex_reset():
    p = rg.compute({"gamma_spx": gamma(), "vix_structure": vix(),
                    "correlation": corr(), "calendar": cal(post_opex=True,
                                                           sessions_since=2)})
    assert p["regime"] == rg.UNSTABLE_PIN
    assert "post-OPEX gamma reset" in p["reason"]


def test_unstable_pin_from_flat_curve():
    p = rg.compute({"gamma_spx": gamma(), "vix_structure": vix("flat"),
                    "correlation": corr(), "calendar": cal()})
    assert p["regime"] == rg.UNSTABLE_PIN
    assert "gone flat" in p["reason"]


def test_acceleration():
    p = rg.compute({"gamma_spx": gamma(regime="negative", cushion_pct=-0.026,
                                       zero=7900.0, charm=-2e9),
                    "vix_structure": vix("backwardation"),
                    "correlation": corr(pctl=88.0, high=True),
                    "calendar": cal()})
    assert p["regime"] == rg.ACCELERATION
    assert "Do not fade" in p["posture"]
    assert p["confidence"] > 0.6


def test_acceleration_on_negative_gamma_alone():
    """Negative gamma with no corroboration still means do not fade, but the
    confidence should reflect the lack of agreement."""
    p = rg.compute({"gamma_spx": gamma(regime="negative", cushion_pct=-0.02,
                                       zero=7900.0),
                    "vix_structure": vix("contango"),
                    "correlation": corr(pctl=60.0),
                    "calendar": cal()})
    assert p["regime"] in (rg.ACCELERATION, rg.REFLEXIVE_REPAIR)


def test_reflexive_repair():
    p = rg.compute({"gamma_spx": gamma(regime="negative", cushion_pct=-0.01,
                                       zero=7800.0),
                    "vix_structure": vix("contango"),
                    "correlation": corr(pctl=30.0),
                    "calendar": cal()})
    assert p["regime"] == rg.REFLEXIVE_REPAIR
    assert "vanna" in p["posture"]
    assert "re-steepened" in p["reason"]


def test_extreme_correlation_forces_acceleration():
    p = rg.compute({"gamma_spx": gamma(regime="negative", cushion_pct=-0.02,
                                       zero=7900.0),
                    "vix_structure": vix("contango"),
                    "correlation": corr(pctl=97.0, high=True, extreme_high=True),
                    "calendar": cal()})
    assert p["regime"] == rg.ACCELERATION


# --------------------------------------------------------------------------- #
# Votes and degradation
# --------------------------------------------------------------------------- #

def test_confidence_is_measured_on_the_family_not_the_exact_regime():
    """UNSTABLE PIN is PIN & GRIND plus a destabiliser.

    Live on 2026-08-04 three of four votes read PIN_GRIND and one read
    UNSTABLE_PIN, which scored 25% and raised a spurious "signals are not
    lining up" warning — when in fact every input agreed the tape was pinned.
    """
    p = rg.compute({"gamma_spx": gamma(), "vix_structure": vix(),
                    "correlation": corr(pctl=5.0, low=True, spiking=True,
                                        change_pct=16.5),
                    "calendar": cal()})
    assert p["regime"] == rg.UNSTABLE_PIN
    assert p["family"] == "pin"
    assert p["confidence"] == 1.0
    assert p["destabilisers"] == ["correlation is spiking off the lows"]
    assert not any("not lining up" in w for w in p["commentary"]["warnings"])


def test_destabilisers_are_listed_and_empty_when_clean():
    clean = rg.compute({"gamma_spx": gamma(), "vix_structure": vix(),
                        "correlation": corr(), "calendar": cal()})
    assert clean["regime"] == rg.PIN_GRIND
    assert clean["destabilisers"] == []

    messy = rg.compute({"gamma_spx": gamma(cushion_pct=0.004),
                        "vix_structure": vix("flat"),
                        "correlation": corr(pctl=5.0, low=True, spiking=True),
                        "calendar": cal(post_opex=True, sessions_since=2)})
    assert len(messy["destabilisers"]) == 4


def test_genuine_disagreement_still_scores_low():
    """Family scoring must not paper over inputs that actually conflict."""
    p = rg.compute({"gamma_spx": gamma(regime="negative", cushion_pct=-0.01,
                                       zero=7800.0),
                    "vix_structure": vix("contango"),
                    "correlation": corr(pctl=30.0), "calendar": cal()})
    assert p["regime"] == rg.REFLEXIVE_REPAIR
    assert p["confidence"] < 0.5
    assert any("not lining up" in w for w in p["commentary"]["warnings"])


def test_votes_name_every_input():
    p = rg.compute({"gamma_spx": gamma(), "vix_structure": vix(),
                    "correlation": corr(), "calendar": cal()})
    signals = {v["signal"] for v in p["votes"]}
    assert signals == {"Dealer gamma", "VIX term structure",
                       "Implied correlation", "OPEX cycle"}


def test_missing_gamma_gives_mixed():
    p = rg.compute({"vix_structure": vix(), "correlation": corr(),
                    "calendar": cal()})
    assert p["regime"] == rg.MIXED
    assert "gamma" in p["missing"]
    assert any("Classified without" in w for w in p["commentary"]["warnings"])


def test_missing_inputs_reduce_confidence():
    full = rg.compute({"gamma_spx": gamma(), "vix_structure": vix(),
                       "correlation": corr(), "calendar": cal()})
    partial = rg.compute({"gamma_spx": gamma(), "calendar": cal()})
    assert partial["confidence"] < full["confidence"]
    assert set(partial["missing"]) == {"vix_structure", "correlation"}
    # It still classifies rather than refusing.
    assert partial["regime"] == rg.PIN_GRIND


def test_store_records_are_unwrapped():
    """compute() accepts raw payloads or {payload, status, ...} store records."""
    wrapped = {
        "gamma_spx": {"payload": gamma(), "status": "ok", "updated_at": 1.0},
        "vix_structure": {"payload": vix(), "status": "ok", "updated_at": 1.0},
        "correlation": {"payload": corr(), "status": "ok", "updated_at": 1.0},
        "calendar": {"payload": cal(), "status": "ok", "updated_at": 1.0},
    }
    assert rg.compute(wrapped)["regime"] == rg.PIN_GRIND


def test_errored_panel_with_no_payload_counts_as_missing():
    p = rg.compute({
        "gamma_spx": {"payload": gamma(), "status": "ok", "updated_at": 1.0},
        "correlation": {"payload": None, "status": "error", "updated_at": 1.0},
    })
    assert "correlation" in p["missing"]


def test_stale_payload_behind_an_error_is_still_used():
    """A cached payload behind an error badge is better than nothing."""
    p = rg.compute({
        "gamma_spx": {"payload": gamma(), "status": "error", "updated_at": 1.0},
        "vix_structure": {"payload": vix(), "status": "ok", "updated_at": 1.0},
        "correlation": {"payload": corr(), "status": "ok", "updated_at": 1.0},
        "calendar": {"payload": cal(), "status": "ok", "updated_at": 1.0},
    })
    assert p["missing"] == []
    assert p["regime"] == rg.PIN_GRIND


# --------------------------------------------------------------------------- #
# Invalidation
# --------------------------------------------------------------------------- #

def test_invalidation_is_the_flip_for_pin_regimes():
    p = rg.compute({"gamma_spx": gamma(), "vix_structure": vix(),
                    "correlation": corr(), "calendar": cal()})
    assert p["invalidation"]["price"] == 7520.0
    assert "Losing" in p["invalidation"]["description"]
    assert "2.34% away" in p["invalidation"]["description"]


def test_invalidation_is_a_reclaim_for_acceleration():
    p = rg.compute({"gamma_spx": gamma(regime="negative", cushion_pct=-0.026,
                                       zero=7900.0),
                    "vix_structure": vix("backwardation"),
                    "correlation": corr(pctl=88.0, high=True), "calendar": cal()})
    assert "Reclaiming" in p["invalidation"]["description"]


def test_non_price_triggers_are_listed():
    p = rg.compute({"gamma_spx": gamma(), "vix_structure": vix(),
                    "correlation": corr(pctl=5.0, low=True), "calendar": cal()})
    trig = " ".join(p["invalidation"]["triggers"])
    assert "VIX/VIX3M" in trig
    assert "COR1M" in trig


def test_no_flip_is_reported_honestly():
    g = gamma()
    g.update({"zero_gamma": None, "no_flip": True, "cushion": None,
              "cushion_pct": None})
    p = rg.compute({"gamma_spx": g, "vix_structure": vix(),
                    "correlation": corr(), "calendar": cal()})
    assert p["invalidation"]["price"] is None
    assert "No gamma flip" in p["invalidation"]["description"]


# --------------------------------------------------------------------------- #
# Confluence scorer
# --------------------------------------------------------------------------- #

def test_confluence_merges_nearby_levels_from_different_lenses():
    """A gamma wall and a value-area edge at the same price score together."""
    g = gamma(call_wall=7750.0)
    pr = profile(vah=7752.0)                       # 2 points away -> same level
    out = rg.confluence(g, None, pr)
    top = [c for c in out if abs(c["level"] - 7751.0) < 5][0]
    assert "gamma wall" in top["sources"]
    assert "value area" in top["sources"]
    assert top["score"] >= 2


def test_confluence_keeps_distinct_levels_apart():
    g = gamma(call_wall=8000.0)
    pr = profile(vah=7750.0)                       # 250 points away
    out = rg.confluence(g, None, pr)
    levels = [c["level"] for c in out]
    assert any(abs(x - 8000.0) < 1 for x in levels)
    assert any(abs(x - 7750.0) < 1 for x in levels)


def test_charm_drift_counts_as_a_source_only_in_its_direction():
    up = rg.confluence(gamma(charm=2.6e9, call_wall=7900.0), None, profile())
    wall_up = [c for c in up if abs(c["level"] - 7900.0) < 5][0]
    assert "charm drift" in wall_up["sources"]

    down = rg.confluence(gamma(charm=-2.6e9, call_wall=7900.0), None, profile())
    wall_dn = [c for c in down if abs(c["level"] - 7900.0) < 5][0]
    assert "charm drift" not in wall_dn["sources"]


def test_spy_book_is_an_independent_source():
    g_spx = gamma(call_wall=8000.0)
    g_spy = {"spot": 768.0, "call_wall": 798.4, "put_wall": 750.0}   # ~8000 SPX
    out = rg.confluence(g_spx, g_spy, profile(ratio=10.02))
    top = [c for c in out if abs(c["level"] - 8000.0) < 20][0]
    assert "SPY book" in top["sources"]


def test_levels_inside_an_lvn_are_marked():
    """A gamma level in a low-volume corridor is weakened, not confirmed."""
    lvn = [{"lo_spx": 7890.0, "hi_spx": 7910.0, "lo": 787.0, "hi": 789.0}]
    out = rg.confluence(gamma(call_wall=7900.0), None, profile(lvn=lvn))
    wall = [c for c in out if abs(c["level"] - 7900.0) < 5][0]
    assert wall["in_lvn"] is True
    other = [c for c in out if abs(c["level"] - 7500.0) < 5]
    assert other and other[0]["in_lvn"] is False


def test_confluence_sorted_by_score_then_distance():
    out = rg.confluence(gamma(), None, profile(
        naked=[{"date": "2026-08-03", "spx": 7600.0}]))
    scores = [c["score"] for c in out]
    assert scores == sorted(scores, reverse=True)


def test_confluence_carries_side_and_distance():
    out = rg.confluence(gamma(spot=7700.0, call_wall=8000.0), None, profile())
    above = [c for c in out if c["level"] > 7700.0]
    below = [c for c in out if c["level"] < 7700.0]
    assert all(c["side"] == "above" for c in above)
    assert all(c["side"] == "below" for c in below)
    for c in out:
        assert abs(c["distance"] - (c["level"] - 7700.0)) < 0.01


def test_confluence_empty_without_gamma():
    assert rg.confluence(None, None, profile()) == []
    assert rg.confluence({"spot": None}, None, profile()) == []


def test_strong_levels_need_three_sources():
    p = rg.compute({"gamma_spx": gamma(), "vix_structure": vix(),
                    "correlation": corr(), "calendar": cal(),
                    "volume_profile": profile(), "gamma_spy": None})
    for lv in p["strong_levels"]:
        assert lv["score"] >= 3
    if not p["strong_levels"]:
        assert any("three or more independent sources" in s
                   for s in p["commentary"]["sentences"])


def test_commentary_is_deterministic():
    panels = {"gamma_spx": gamma(), "vix_structure": vix(),
              "correlation": corr(), "calendar": cal(),
              "volume_profile": profile()}
    a = rg.compute(copy.deepcopy(panels))
    b = rg.compute(copy.deepcopy(panels))
    assert a == b
