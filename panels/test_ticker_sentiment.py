"""Tests for the composite sentiment panel.

Several of these exist specifically to stop someone "simplifying" a deliberate
design choice later:

  * gamma regime must NOT become a directional input (test_gamma_regime_*),
  * net DEX must NOT become one either — under this project's sign convention
    it is identically equal to gross DEX (test_dex_*),
  * short interest must be signed by momentum rather than assumed bullish
    (test_squeeze_*),
  * a missing panel must renormalise the weights rather than score 50
    (test_weights_*).

    pytest panels/test_ticker_sentiment.py
"""

from __future__ import annotations

from panels import ticker_sentiment as cs


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def positioning(**over):
    base = {"symbol": "XYZ", "spot": 100.0, "regime": "positive",
            "zero_gamma": 98.0, "no_flip": False, "cushion_pct": 0.02,
            "dex": 5e8, "gross_dex": 5e8, "net_gex": 1e7,
            "call_wall": 105.0, "put_wall": 95.0,
            "nearest_magnet": 100.0}
    base.update(over)
    return base


def vol(**over):
    base = {"symbol": "XYZ", "skew_25d_pct": 0.06, "skew_state": "balanced",
            "pcr_oi": 0.80, "pcr_vol": 0.75, "term_slope": 0.0,
            "ivrv_spread": 0.0, "ivrv_state": "fair", "thin_chain": False}
    base.update(over)
    return base


def news(mean=0.0, n=40, tone="Mixed", **over):
    base = {"symbol": "XYZ", "empty": False, "count": n,
            "sentiment": {"tone": tone, "mean": mean, "n": n,
                          "pos_pct": 33, "neg_pct": 33, "neu_pct": 34,
                          "pos": 13, "neg": 13, "neu": 14}}
    base.update(over)
    return base


def social(blended=0.0, n=90, bull_pct=0.5, tone="Mixed", **over):
    base = {"symbol": "XYZ", "empty": False, "n": n, "blended": blended,
            "bull_pct": bull_pct, "tone": tone}
    base.update(over)
    return base


def squeeze(**over):
    base = {"symbol": "XYZ", "short_float": 0.03, "days_to_cover": 1.5,
            "perf_month": 0.0, "perf_week": 0.0, "recom": 3.0,
            "recom_label": "Hold", "target_upside": 0.0, "insider_trans": 0.0,
            "market_cap": 5e9}
    base.update(over)
    return base


def events(**over):
    base = {"symbol": "XYZ", "move_state": "fair", "earnings_soon": False,
            "earnings_days_out": 40, "move_ratio": 1.0,
            "filings": {"available": True, "offering_flag": False}}
    base.update(over)
    return base


def panels(**over):
    base = {"t2_positioning": positioning(), "t2_vol": vol(),
            "t2_news": news(), "t2_social": social(),
            "t2_squeeze": squeeze(), "t2_events": events()}
    base.update(over)
    return base


def sub(p, key):
    return next(s for s in p["subscores"] if s["key"] == key)


# --------------------------------------------------------------------------- #
# Positioning: direction vs volatility
# --------------------------------------------------------------------------- #

def test_gamma_regime_alone_does_not_change_the_direction_score():
    # Positive gamma damps moves and negative gamma amplifies them; neither is
    # bullish. Flipping only the regime must leave the score untouched.
    pos = cs.compute(panels(t2_positioning=positioning(regime="positive")), "XYZ")
    neg = cs.compute(panels(t2_positioning=positioning(regime="negative")), "XYZ")
    assert sub(pos, "positioning")["score"] == sub(neg, "positioning")["score"]


def test_negative_gamma_sets_the_amplifying_flag_and_warns():
    p = cs.compute(panels(t2_positioning=positioning(regime="negative")), "XYZ")
    assert p["flags"]["amplifying"] is True
    assert any("short gamma" in w for w in p["commentary"]["warnings"])


def test_dex_is_not_a_directional_input():
    # Under the dealers-long-calls/short-puts convention every contract
    # contributes positive DEX, so net == gross for every ticker. Changing it
    # (while holding the real directional inputs fixed) must change nothing.
    small = cs.compute(panels(t2_positioning=positioning(dex=1e6, gross_dex=1e6)), "XYZ")
    huge = cs.compute(panels(t2_positioning=positioning(dex=9e11, gross_dex=9e11)), "XYZ")
    assert sub(small, "positioning")["score"] == sub(huge, "positioning")["score"]


def test_spot_above_the_flip_scores_higher_than_spot_below_it():
    above = cs.compute(panels(t2_positioning=positioning(cushion_pct=0.05)), "XYZ")
    below = cs.compute(panels(t2_positioning=positioning(cushion_pct=-0.05)), "XYZ")
    assert sub(above, "positioning")["score"] > sub(below, "positioning")["score"]


def test_walls_on_the_same_side_of_spot_are_skipped_not_saturated():
    # A thin single-name chain often puts both walls on one strike. The
    # difference of the two distances is then an artefact, and using it
    # saturated the sub-score at 100 before this guard.
    both_above = positioning(call_wall=105.0, put_wall=105.0,
                             cushion_pct=None, no_flip=True,
                             nearest_magnet=None)
    p = cs.compute(panels(t2_positioning=both_above), "XYZ")
    # No flip, no usable walls, no magnet -> genuinely no directional read.
    assert sub(p, "positioning")["available"] is False


def test_positioning_shrinks_toward_neutral_on_a_single_input():
    # One saturated term must not read as confidently as three agreeing ones.
    one = positioning(cushion_pct=None, no_flip=True, call_wall=105.0,
                      put_wall=105.0, nearest_magnet=130.0)
    three = positioning(cushion_pct=0.10, call_wall=130.0, put_wall=99.0,
                        nearest_magnet=130.0)
    s_one = sub(cs.compute(panels(t2_positioning=one), "XYZ"), "positioning")["score"]
    s_three = sub(cs.compute(panels(t2_positioning=three), "XYZ"), "positioning")["score"]
    assert 50 < s_one < s_three


# --------------------------------------------------------------------------- #
# Squeeze: fuel, not direction
# --------------------------------------------------------------------------- #

def test_squeeze_is_bullish_only_when_the_stock_is_rising():
    up = squeeze(short_float=0.34, days_to_cover=5.0, perf_month=0.15)
    down = squeeze(short_float=0.34, days_to_cover=5.0, perf_month=-0.15)
    s_up = sub(cs.compute(panels(t2_squeeze=up), "XYZ"), "squeeze")["score"]
    s_down = sub(cs.compute(panels(t2_squeeze=down), "XYZ"), "squeeze")["score"]
    # Identical short interest, opposite readings: shorts covering vs winning.
    assert s_up > 60 and s_down < 40


def test_low_short_interest_is_neutral_regardless_of_momentum():
    for perf in (0.20, -0.20):
        s = sub(cs.compute(panels(t2_squeeze=squeeze(short_float=0.01,
                                                     days_to_cover=0.5,
                                                     perf_month=perf)), "XYZ"),
                "squeeze")["score"]
        assert abs(s - 50) < 1.0


def test_squeeze_is_neutral_when_momentum_is_unknown():
    s = sub(cs.compute(panels(t2_squeeze=squeeze(short_float=0.34,
                                                 perf_month=None)), "XYZ"),
            "squeeze")["score"]
    assert s == 50.0


# --------------------------------------------------------------------------- #
# Weighting
# --------------------------------------------------------------------------- #

def test_missing_panels_renormalise_rather_than_scoring_fifty():
    # A dropped input must not drag the composite toward neutral.
    full = cs.compute(panels(t2_news=news(mean=0.5), t2_social=social(blended=0.8)),
                      "XYZ")
    without_squeeze = cs.compute(
        panels(t2_news=news(mean=0.5), t2_social=social(blended=0.8),
               t2_squeeze=None), "XYZ")
    assert sub(without_squeeze, "squeeze")["available"] is False
    assert sub(without_squeeze, "analyst")["available"] is False
    assert "Short interest" in without_squeeze["missing"]
    # Both bullish reads survive; the composite does not collapse to 50.
    assert without_squeeze["composite"] > 55
    assert full["composite"] > 55


def test_thin_samples_reduce_weight_rather_than_score():
    thin = cs.compute(panels(t2_news=news(mean=0.9, n=3)), "XYZ")
    thick = cs.compute(panels(t2_news=news(mean=0.9, n=40)), "XYZ")
    # Same score, smaller effective weight.
    assert sub(thin, "news")["score"] == sub(thick, "news")["score"]
    assert sub(thin, "news")["weight_eff"] < sub(thick, "news")["weight_eff"]


def test_all_panels_missing_gives_no_reading_rather_than_a_number():
    p = cs.compute({}, "XYZ")
    assert p["composite"] is None
    assert p["band"] == "unknown"
    assert p["commentary"]["headline"]


def test_weights_sum_to_one_hundred():
    assert sum(cs.WEIGHTS.values()) == 100


# --------------------------------------------------------------------------- #
# Bands and confidence
# --------------------------------------------------------------------------- #

def test_bands_map_scores_to_labels():
    assert cs._band(10)[1] == "STRONGLY BEARISH"
    assert cs._band(30)[1] == "BEARISH"
    assert cs._band(50)[1] == "MIXED"
    assert cs._band(70)[1] == "BULLISH"
    assert cs._band(90)[1] == "STRONGLY BULLISH"


def test_disagreeing_inputs_reduce_confidence():
    agree = cs.compute(panels(t2_news=news(mean=0.4), t2_social=social(blended=0.4)),
                       "XYZ")
    clash = cs.compute(panels(t2_news=news(mean=0.9),
                              t2_social=social(blended=-0.9, bull_pct=0.05)),
                       "XYZ")
    assert clash["confidence"] < agree["confidence"]


def test_confidence_never_falls_below_the_floor():
    p = cs.compute(panels(t2_news=news(mean=0.9, n=1),
                          t2_social=social(blended=-0.9, n=1, bull_pct=0.0),
                          t2_vol=vol(thin_chain=True),
                          t2_squeeze=None), "XYZ")
    assert p["confidence"] >= cs.CONFIDENCE_FLOOR


# --------------------------------------------------------------------------- #
# Divergences
# --------------------------------------------------------------------------- #

def keys_of(p):
    return {d["key"] for d in p["divergences"]}


def test_retail_bullish_into_a_downtrend_fires():
    p = cs.compute(panels(t2_social=social(blended=0.9, bull_pct=0.9),
                          t2_squeeze=squeeze(perf_month=-0.20)), "XYZ")
    assert "crowd_vs_price" in keys_of(p)


def test_retail_bullish_in_an_uptrend_does_not_fire():
    p = cs.compute(panels(t2_social=social(blended=0.9, bull_pct=0.9),
                          t2_squeeze=squeeze(perf_month=0.20)), "XYZ")
    assert "crowd_vs_price" not in keys_of(p)


def test_retail_long_against_a_bid_put_skew_fires():
    p = cs.compute(panels(t2_social=social(blended=0.8, bull_pct=0.85),
                          t2_vol=vol(skew_25d_pct=0.20, skew_state="put-bid")),
                   "XYZ")
    assert "crowd_vs_options" in keys_of(p)


def test_insiders_selling_into_retail_enthusiasm_fires():
    p = cs.compute(panels(t2_social=social(blended=0.9, bull_pct=0.9),
                          t2_squeeze=squeeze(insider_trans=-0.06)), "XYZ")
    assert "insider_vs_retail" in keys_of(p)


def test_squeeze_setup_needs_both_short_interest_and_negative_gamma():
    both = cs.compute(panels(t2_squeeze=squeeze(short_float=0.30),
                             t2_positioning=positioning(regime="negative")), "XYZ")
    assert "squeeze_setup" in keys_of(both)

    only_short = cs.compute(panels(t2_squeeze=squeeze(short_float=0.30),
                                   t2_positioning=positioning(regime="positive")),
                            "XYZ")
    assert "squeeze_setup" not in keys_of(only_short)


def test_price_above_target_fires_only_when_the_target_is_below_spot():
    below = cs.compute(panels(t2_squeeze=squeeze(target_upside=-0.10)), "XYZ")
    above = cs.compute(panels(t2_squeeze=squeeze(target_upside=0.10)), "XYZ")
    assert "price_thru_target" in keys_of(below)
    assert "price_thru_target" not in keys_of(above)


def test_dilution_divergence_is_gated_by_market_cap():
    # A 424B at a mega-cap is routine debt issuance, not equity dilution.
    filings = {"available": True, "offering_flag": True}
    small = cs.compute(panels(t2_social=social(blended=0.8, bull_pct=0.8),
                              t2_squeeze=squeeze(market_cap=5e8),
                              t2_events=events(filings=filings)), "XYZ")
    mega = cs.compute(panels(t2_social=social(blended=0.8, bull_pct=0.8),
                             t2_squeeze=squeeze(market_cap=5e12),
                             t2_events=events(filings=filings)), "XYZ")
    assert "dilution_overhang" in keys_of(small)
    assert "dilution_overhang" not in keys_of(mega)


def test_divergences_are_ordered_by_severity():
    p = cs.compute(panels(t2_social=social(blended=0.9, bull_pct=0.9),
                          t2_squeeze=squeeze(perf_month=-0.20, insider_trans=-0.06,
                                             target_upside=-0.10)), "XYZ")
    order = [d["severity"] for d in p["divergences"]]
    rank = {"alert": 0, "warn": 1, "note": 2}
    assert order == sorted(order, key=lambda s: rank[s])


def test_alerts_are_surfaced_as_warnings_in_the_commentary():
    p = cs.compute(panels(t2_social=social(blended=0.9, bull_pct=0.9),
                          t2_squeeze=squeeze(perf_month=-0.20)), "XYZ")
    assert any("Retail bullish into a downtrend" in w
               for w in p["commentary"]["warnings"])


# --------------------------------------------------------------------------- #
# Store-record handling
# --------------------------------------------------------------------------- #

def test_accepts_store_records_as_well_as_raw_payloads():
    wrapped = {k: {"payload": v, "status": "ok", "updated_at": 1.0}
               for k, v in panels().items()}
    assert cs.compute(wrapped, "XYZ")["composite"] is not None


def test_errored_panel_with_no_payload_counts_as_missing():
    recs = {k: {"payload": v, "status": "ok", "updated_at": 1.0}
            for k, v in panels().items()}
    recs["t2_vol"] = {"payload": None, "status": "error", "updated_at": 1.0}
    p = cs.compute(recs, "XYZ")
    assert sub(p, "vol")["available"] is False


def test_stale_payload_behind_an_error_badge_is_still_used():
    recs = {k: {"payload": v, "status": "ok", "updated_at": 1.0}
            for k, v in panels().items()}
    recs["t2_vol"] = {"payload": vol(), "status": "error", "updated_at": 1.0}
    p = cs.compute(recs, "XYZ")
    assert sub(p, "vol")["available"] is True
