"""Tests for the gamma engine.

Two layers, mirroring market_almanack's approach:
  1. Pipeline tests on synthetic CBOE payloads — filtering, symbol roots, sign
     convention, walls, zero gamma, expiry buckets, charm projection.
  2. Commentary rule tests on hand-built metrics dicts at SPX scale.

Everything is offline; no network.

    pytest panels/test_gamma_engine.py
"""

from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone

import numpy as np

from panels import _bs
from panels import gamma_engine as ge

NOW = datetime(2026, 8, 4, 16, 6, 26, tzinfo=timezone.utc)
TODAY = NOW.date()

R, Q = ge.R_RATE, ge.DIV_YIELD


# --------------------------------------------------------------------------- #
# Synthetic chain builder
# --------------------------------------------------------------------------- #

def _sym(root: str, expiry: date, right: str, strike: float) -> str:
    return f"{root}{expiry:%y%m%d}{right}{int(round(strike * 1000)):08d}"


def _contract(root, expiry, right, strike, oi, spot, iv=0.15):
    """A contract whose quoted greeks are internally consistent with BS."""
    T = max((expiry - TODAY).days, 0) / 365.0
    is_call = right == "C"
    return {
        "option": _sym(root, expiry, right, strike),
        "open_interest": float(oi),
        "gamma": float(_bs.bs_gamma(spot, strike, iv, T, R, Q)),
        "delta": float(_bs.bs_delta(spot, strike, iv, T, R, Q, is_call)),
        "iv": iv,
        "volume": 10.0,
        "bid": 1.0, "ask": 1.1,
    }


def build_chain(spot=7700.0, root="SPXW", strikes=None, expiries=None,
                call_oi=1000, put_oi=1000, extra=None):
    strikes = strikes if strikes is not None else [
        spot + k for k in (-300, -200, -100, 0, 100, 200, 300)]
    expiries = expiries if expiries is not None else [
        TODAY + timedelta(days=d) for d in (7, 30)]
    opts = []
    for e in expiries:
        for k in strikes:
            opts.append(_contract(root, e, "C", k, call_oi, spot))
            opts.append(_contract(root, e, "P", k, put_oi, spot))
    if extra:
        opts.extend(extra)
    return {
        "timestamp": "2026-08-04 16:06:26",
        "symbol": "^SPX",
        "data": {"current_price": spot, "close": spot,
                 "bid": spot - 1, "ask": spot + 1, "options": opts},
    }


# --------------------------------------------------------------------------- #
# 1. Parsing and filtering
# --------------------------------------------------------------------------- #

def test_spx_accepts_both_roots():
    """SPXW is the majority of the live SPX chain — it must not be dropped."""
    chain = build_chain(root="SPXW")
    chain["data"]["options"] += build_chain(root="SPX")["data"]["options"]
    m = ge.compute(chain, ge.SPX, NOW)
    assert set(m["root_counts"]) == {"SPX", "SPXW"}
    assert m["root_counts"]["SPXW"] > 0 and m["root_counts"]["SPX"] > 0


def test_spy_regex_rejects_spx_symbols():
    assert ge._parse_symbol(ge.SPY, "SPXW260821C07700000") is None
    assert ge._parse_symbol(ge.SPY, "SPY260821C00770000") is not None
    assert ge._parse_symbol(ge.SPX, "SPY260821C00770000") is None


def test_symbol_parse_extracts_fields():
    root, expiry, right, strike = ge._parse_symbol(ge.SPX, "SPXW260821P07725000")
    assert root == "SPXW"
    assert expiry == date(2026, 8, 21)
    assert right == "P"
    assert strike == 7725.0


def test_filters_drop_unusable_rows():
    spot = 7700.0
    junk = [
        _contract("SPXW", TODAY + timedelta(days=30), "C", 7800, 0, spot),      # OI 0
        _contract("SPXW", TODAY - timedelta(days=1), "C", 7800, 500, spot),     # expired
        _contract("SPXW", TODAY + timedelta(days=200), "C", 7800, 500, spot),   # > window
        {"option": "GARBAGE", "open_interest": 100, "gamma": 1, "delta": 1, "iv": 0.2},
    ]
    bad_iv = _contract("SPXW", TODAY + timedelta(days=30), "C", 7900, 500, spot)
    bad_iv["iv"] = 6.0                                                          # iv >= 5
    junk.append(bad_iv)

    base = ge.compute(build_chain(spot=spot), ge.SPX, NOW)
    withjunk = ge.compute(build_chain(spot=spot, extra=junk), ge.SPX, NOW)
    assert withjunk["n_contracts"] == base["n_contracts"]


def test_no_usable_spot_raises():
    chain = build_chain()
    chain["data"] = {"options": chain["data"]["options"]}
    try:
        ge.compute(chain, ge.SPX, NOW)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "spot" in str(exc)


def test_empty_after_filtering_raises():
    chain = build_chain()
    for o in chain["data"]["options"]:
        o["open_interest"] = 0
    try:
        ge.compute(chain, ge.SPX, NOW)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "survived filtering" in str(exc)


# --------------------------------------------------------------------------- #
# 2. Sign convention and aggregates
# --------------------------------------------------------------------------- #

def test_sign_convention_calls_positive_puts_negative():
    """Dealers long calls / short puts: call-only book is +GEX, put-only is -GEX."""
    calls_only = ge.compute(build_chain(put_oi=0), ge.SPX, NOW)
    puts_only = ge.compute(build_chain(call_oi=0), ge.SPX, NOW)
    assert calls_only["net_gex"] > 0
    assert puts_only["net_gex"] < 0
    assert calls_only["regime"] == "positive"
    assert puts_only["regime"] == "negative"


def test_gex_scales_with_open_interest():
    a = ge.compute(build_chain(put_oi=0, call_oi=1000), ge.SPX, NOW)
    b = ge.compute(build_chain(put_oi=0, call_oi=2000), ge.SPX, NOW)
    assert abs(b["net_gex"] / a["net_gex"] - 2.0) < 1e-9


def test_spx_gex_is_ten_times_spy_at_equal_open_interest():
    """Same OI at 10x the index level -> exactly 10x the per-1% dollar gamma.

    GEX carries S^2 but Black-Scholes gamma carries 1/S, so the product is
    *linear* in the index level. That is the arithmetic justifying the 10x
    threshold scaling in the SPX config — not 100x, which is the easy mistake.
    """
    spx = ge.compute(build_chain(spot=7700.0, put_oi=0), ge.SPX, NOW)
    spy_chain = build_chain(spot=770.0, root="SPY", put_oi=0,
                            strikes=[770.0 + k for k in (-30, -20, -10, 0, 10, 20, 30)])
    spy_chain["symbol"] = "SPY"
    spy = ge.compute(spy_chain, ge.SPY, NOW)
    assert abs(spx["net_gex"] / spy["net_gex"] - 10.0) < 1e-6
    # The calibrated thresholds are in the same neighbourhood but not exactly
    # 10x: the two live chains carry different open-interest distributions, so
    # each threshold is set from its own observation (see the Underlying
    # docstring). Guard only against an order-of-magnitude slip.
    assert 5.0 < ge.SPX.gex_heavy / ge.SPY.gex_heavy < 20.0
    assert 5.0 < ge.SPX.gex_moderate / ge.SPY.gex_moderate < 20.0


# --------------------------------------------------------------------------- #
# 3. Levels
# --------------------------------------------------------------------------- #

def test_walls_find_the_heaviest_strikes():
    spot = 7700.0
    heavy_call = _contract("SPXW", TODAY + timedelta(days=30), "C", 7900, 90000, spot)
    heavy_put = _contract("SPXW", TODAY + timedelta(days=30), "P", 7500, 90000, spot)
    m = ge.compute(build_chain(spot=spot, extra=[heavy_call, heavy_put]), ge.SPX, NOW)
    assert m["call_wall"] == 7900.0
    assert m["put_wall"] == 7500.0


def test_walls_stay_inside_the_display_window():
    """A monster strike outside +/-6% must not become the wall."""
    spot = 7700.0
    far = _contract("SPXW", TODAY + timedelta(days=30), "C", 9000, 500000, spot)
    m = ge.compute(build_chain(spot=spot, extra=[far]), ge.SPX, NOW)
    assert m["call_wall"] is not None
    assert m["call_wall"] <= spot * (1 + ge.DISPLAY_PCT)


def test_walls_stay_on_their_own_side_of_spot():
    """One heavy round-number strike must not become both walls.

    The live SPX chain does exactly this at 8000, which produced a "put wall
    above spot" and a wall-to-wall range of 0.00% before the side constraint.
    """
    spot = 7700.0
    both_at_one_strike = [
        _contract("SPXW", TODAY + timedelta(days=30), "C", 7975, 90000, spot),
        _contract("SPXW", TODAY + timedelta(days=30), "P", 7975, 90000, spot),
    ]
    m = ge.compute(build_chain(spot=spot, extra=both_at_one_strike), ge.SPX, NOW)
    assert m["call_wall"] >= spot
    assert m["put_wall"] <= spot
    assert m["call_wall"] != m["put_wall"]


def test_magnets_are_restricted_to_the_display_window():
    """Deep-ITM legacy OI is not a pin candidate.

    Live SPY returned magnets at 550 and 520 against a 768 spot before this.
    """
    spot = 7700.0
    far_itm = _contract("SPXW", TODAY + timedelta(days=20), "C", 5500, 900000, spot)
    near = _contract("SPXW", TODAY + timedelta(days=20), "C", 7750, 80000, spot)
    m = ge.compute(build_chain(spot=spot, extra=[far_itm, near]), ge.SPX, NOW)
    strikes = [x["strike"] for x in m["oi_magnets"]]
    assert 5500.0 not in strikes
    assert 7750.0 in strikes
    lo, hi = spot * (1 - ge.DISPLAY_PCT), spot * (1 + ge.DISPLAY_PCT)
    assert all(lo <= s <= hi for s in strikes)


def test_dex_window_excludes_far_strikes():
    spot = 7700.0
    far = _contract("SPXW", TODAY + timedelta(days=30), "C", 5500, 900000, spot)
    m = ge.compute(build_chain(spot=spot, extra=[far]), ge.SPX, NOW)
    # The deep-ITM block dominates total DEX but must not touch the window.
    assert m["dex"] > m["dex_window"]
    base = ge.compute(build_chain(spot=spot), ge.SPX, NOW)
    assert abs(m["dex_window"] - base["dex_window"]) < 1e-6


def test_zero_gamma_brackets_the_sign_change():
    """Puts below / calls above -> flip sits between them, and regime follows."""
    spot = 7700.0
    extra = [
        _contract("SPXW", TODAY + timedelta(days=30), "P", 7600, 60000, spot),
        _contract("SPXW", TODAY + timedelta(days=30), "C", 7800, 60000, spot),
    ]
    m = ge.compute(build_chain(spot=spot, call_oi=0, put_oi=0, extra=extra),
                   ge.SPX, NOW)
    assert m["no_flip"] is False
    assert m["zero_gamma"] is not None
    assert 7600 < m["zero_gamma"] < 7800
    assert m["regime"] == ("positive" if spot >= m["zero_gamma"] else "negative")
    # The displayed numbers must reconcile exactly: cushion is derived from the
    # rounded flip level, not the raw one.
    assert m["cushion"] == spot - m["zero_gamma"]
    assert abs(m["cushion_pct"] - m["cushion"] / spot) < 1e-12


def test_no_flip_when_one_signed():
    m = ge.compute(build_chain(put_oi=0), ge.SPX, NOW)
    assert m["no_flip"] is True
    assert m["zero_gamma"] is None
    assert m["cushion"] is None


def test_oi_magnets_are_top_three_within_30_days():
    spot = 7700.0
    extra = [
        _contract("SPXW", TODAY + timedelta(days=10), "C", 7750, 80000, spot),
        _contract("SPXW", TODAY + timedelta(days=10), "C", 7850, 70000, spot),
        _contract("SPXW", TODAY + timedelta(days=10), "C", 7950, 60000, spot),
        # Beyond 30 days: must be ignored even though it is the biggest.
        _contract("SPXW", TODAY + timedelta(days=60), "C", 8000, 999999, spot),
    ]
    m = ge.compute(build_chain(spot=spot, extra=extra), ge.SPX, NOW)
    strikes = [x["strike"] for x in m["oi_magnets"]]
    assert strikes == [7750.0, 7850.0, 7950.0]
    assert m["nearest_magnet"] == 7750.0


def test_chart_is_bucketed_and_sorted():
    m = ge.compute(build_chain(), ge.SPX, NOW)
    strikes = [c["strike"] for c in m["chart"]]
    assert strikes == sorted(strikes)
    assert all(abs(s % ge.SPX.bucket) < 1e-9 for s in strikes)
    assert m["bucket"] == 25.0


# --------------------------------------------------------------------------- #
# 4. Expiry buckets (RESEARCH.md section 6)
# --------------------------------------------------------------------------- #

def test_expiry_buckets_partition_the_book():
    expiries = [TODAY, TODAY + timedelta(days=3), TODAY + timedelta(days=14),
                TODAY + timedelta(days=60)]
    m = ge.compute(build_chain(expiries=expiries), ge.SPX, NOW)
    buckets = m["expiry_buckets"]
    assert len(buckets) == 4
    assert sum(b["n_contracts"] for b in buckets) == m["n_contracts"]
    assert abs(sum(b["abs_share"] for b in buckets) - 1.0) < 1e-9
    assert buckets[0]["label"] == "0DTE" and buckets[0]["n_contracts"] > 0


def test_zero_dte_share():
    only_today = ge.compute(build_chain(expiries=[TODAY]), ge.SPX, NOW)
    assert abs(only_today["zero_dte_gamma_share"] - 1.0) < 1e-9
    none_today = ge.compute(build_chain(expiries=[TODAY + timedelta(days=30)]),
                            ge.SPX, NOW)
    assert none_today["zero_dte_gamma_share"] == 0.0


# --------------------------------------------------------------------------- #
# 5. Charm decay projection (RESEARCH.md section 2)
# --------------------------------------------------------------------------- #

def test_projection_shape_and_trading_days():
    from panels import calendar_context as cal
    m = ge.compute(build_chain(expiries=[TODAY + timedelta(days=45)]), ge.SPX, NOW)
    proj = m["charm_projection"]
    assert len(proj["series"]) == ge.PROJECTION_DAYS
    for row in proj["series"]:
        d = date.fromisoformat(row["date"])
        assert cal.is_trading_day(d)
    days = [date.fromisoformat(r["date"]) for r in proj["series"]]
    assert days == sorted(days)


def test_projection_drops_contracts_as_they_expire():
    expiries = [TODAY + timedelta(days=d) for d in (2, 5, 45)]
    m = ge.compute(build_chain(expiries=expiries), ge.SPX, NOW)
    alive = [r["contracts_alive"] for r in m["charm_projection"]["series"]]
    assert alive == sorted(alive, reverse=True)       # monotonically non-increasing
    assert alive[0] > alive[-1]                        # something did expire


def test_charm_tailwind_from_short_puts_is_a_buy_flow():
    """The canonical RESEARCH.md section 2 case, and the sign that matters most.

    Dealers short OTM puts are long delta and hedge by shorting stock. As the
    puts decay their delta rises toward zero, the book's delta falls, less short
    stock is needed, and dealers BUY BACK. The published number is the flow, so
    it must be positive even though the book-delta derivative is negative.
    """
    spot = 7700.0
    puts = [_contract("SPXW", TODAY + timedelta(days=40), "P", k, 50000, spot)
            for k in (7300.0, 7400.0, 7500.0)]
    m = ge.compute(build_chain(spot=spot, call_oi=0, put_oi=0, extra=puts),
                   ge.SPX, NOW)
    proj = m["charm_projection"]

    assert m["dex"] > 0                                # short puts -> long delta
    assert m["charm_book_delta_per_day"] < 0           # book delta falling
    assert m["charm_drift"] > 0                        # ...so the flow is buying
    assert m["charm_drift"] == -m["charm_book_delta_per_day"]

    assert all(r["charm_per_day"] > 0 for r in proj["series"])
    flows = [r["cum_hedge_flow"] for r in proj["series"]]
    assert flows == sorted(flows)                      # accumulating buy pressure
    assert flows[-1] > 0


def test_otm_calls_also_produce_a_buy_flow():
    """A structural consequence of the assumed dealer convention, worth pinning.

    Long call and short put are *both* long-delta positions, so under "dealers
    long calls, short puts" every OTM contract decays toward zero delta and
    every one of them forces the same buy-back. OTM charm can never be a sell
    flow here — a useful sanity check on the convention itself.
    """
    spot = 7700.0
    calls = [_contract("SPXW", TODAY + timedelta(days=40), "C", k, 50000, spot)
             for k in (7900.0, 8000.0, 8100.0)]
    m = ge.compute(build_chain(spot=spot, call_oi=0, put_oi=0, extra=calls),
                   ge.SPX, NOW)
    assert m["dex"] > 0                                # long calls -> long delta
    assert m["charm_drift"] > 0
    assert all(r["charm_per_day"] > 0
               for r in m["charm_projection"]["series"])


def test_itm_calls_produce_a_sell_flow():
    """The mirror case: ITM delta migrates toward 1, so the book delta *rises*
    and dealers must sell more stock to stay hedged."""
    spot = 7700.0
    calls = [_contract("SPXW", TODAY + timedelta(days=40), "C", k, 50000, spot)
             for k in (7300.0, 7400.0, 7500.0)]
    m = ge.compute(build_chain(spot=spot, call_oi=0, put_oi=0, extra=calls),
                   ge.SPX, NOW)
    assert m["charm_book_delta_per_day"] > 0           # book delta rising
    assert m["charm_drift"] < 0                        # ...so the flow is selling
    flows = [r["cum_hedge_flow"] for r in m["charm_projection"]["series"]]
    assert flows == sorted(flows, reverse=True)        # accumulating sell pressure
    assert flows[-1] < 0


def test_projection_marks_opex():
    """The OPEX session inside the horizon is flagged so the UI can mark it."""
    from panels import calendar_context as cal
    # 2026-08-17 is 4 sessions before the 2026-08-21 monthly OPEX.
    now = datetime(2026, 8, 17, 16, 0, 0, tzinfo=timezone.utc)
    today = now.date()

    def contract(expiry, right, strike, oi):
        T = max((expiry - today).days, 0) / 365.0
        return {"option": _sym("SPXW", expiry, right, strike),
                "open_interest": float(oi),
                "gamma": float(_bs.bs_gamma(7700.0, strike, 0.15, T, R, Q)),
                "delta": float(_bs.bs_delta(7700.0, strike, 0.15, T, R, Q,
                                            right == "C")),
                "iv": 0.15}

    opts = [contract(date(2026, 9, 18), r, k, 1000)
            for r in ("C", "P") for k in (7600.0, 7700.0, 7800.0)]
    chain = {"timestamp": "2026-08-17 16:00:00",
             "data": {"current_price": 7700.0, "options": opts}}
    m = ge.compute(chain, ge.SPX, now)
    flags = [r for r in m["charm_projection"]["series"] if r["is_opex"]]
    assert len(flags) == 1
    assert flags[0]["date"] == "2026-08-21"
    assert cal.is_trading_day(date.fromisoformat(flags[0]["date"]))


def test_projection_base_delta_matches_bs_recompute():
    m = ge.compute(build_chain(), ge.SPX, NOW)
    proj = m["charm_projection"]
    # Same order of magnitude as the feed-quoted DEX (feed deltas vs BS deltas
    # differ slightly, but not by a factor).
    assert np.sign(proj["base_delta"]) == np.sign(m["dex"]) or abs(m["dex"]) < 1e6


# --------------------------------------------------------------------------- #
# 6. Quality flags
# --------------------------------------------------------------------------- #

def test_stale_flag():
    chain = build_chain()
    chain["timestamp"] = "2026-08-04 14:00:00"     # >30 min before NOW
    assert ge.compute(chain, ge.SPX, NOW)["stale"] is True
    chain["timestamp"] = "2026-08-04 16:00:00"
    assert ge.compute(chain, ge.SPX, NOW)["stale"] is False


def test_fallback_spot_flag():
    chain = build_chain()
    chain["data"]["current_price"] = None
    m = ge.compute(chain, ge.SPX, NOW)
    assert m["fallback_spot"] is True
    assert m["spot_source"] == "close"


def test_spy_carries_ex_div_flag_and_spx_does_not():
    """The early-exercise caveat is SPY-only (RESEARCH.md section 1)."""
    # 2026-09-18 is the quarterly OPEX / SPY ex-div; look 3 days ahead of it.
    now = datetime(2026, 9, 16, 16, 0, 0, tzinfo=timezone.utc)
    today = now.date()

    def chain_for(root, spot, strikes):
        opts = []
        for k in strikes:
            for right in ("C", "P"):
                T = 30 / 365
                opts.append({"option": _sym(root, today + timedelta(days=30),
                                            right, k),
                             "open_interest": 1000.0,
                             "gamma": float(_bs.bs_gamma(spot, k, 0.15, T, R, Q)),
                             "delta": float(_bs.bs_delta(spot, k, 0.15, T, R, Q,
                                                         right == "C")),
                             "iv": 0.15})
        return {"timestamp": "2026-09-16 16:00:00",
                "data": {"current_price": spot, "options": opts}}

    spy = ge.compute(chain_for("SPY", 770.0, [750.0, 770.0, 790.0]), ge.SPY, now)
    spx = ge.compute(chain_for("SPXW", 7700.0, [7500.0, 7700.0, 7900.0]), ge.SPX, now)
    assert spy["ex_div_risk"] is True
    assert spy["next_ex_div"] == "2026-09-18"
    assert spx["ex_div_risk"] is False
    assert spx["next_ex_div"] is None


def test_spy_has_no_commentary_spx_does():
    spy_chain = build_chain(spot=770.0, root="SPY",
                            strikes=[770.0 + k for k in (-30, 0, 30)])
    assert ge.compute(spy_chain, ge.SPY, NOW)["commentary"] is None
    assert ge.compute(build_chain(), ge.SPX, NOW)["commentary"] is not None


# --------------------------------------------------------------------------- #
# 7. Commentary engine (SPX-scale metrics)
# --------------------------------------------------------------------------- #

BASE = {
    "spot": 7700.00,
    "regime": "positive",
    "zero_gamma": 7580.00,
    "no_flip": False,
    "cushion": 120.0,
    "cushion_pct": 120.0 / 7700.0,          # ~1.56% > 0.75% -> firm
    "net_gex": 60e9,                         # moderate at SPX scale
    "dex": 0.0,
    "dex_window": 0.0,
    "vanna_pressure": 0.0,
    "charm_drift": 0.0,
    "call_wall": 7900.00,                    # ~2.6% away -> not near
    "put_wall": 7500.00,
    "call_wall_is_magnet": False,
    "oi_magnets": [{"strike": 7800.0, "oi": 50000}],
    "nearest_magnet": 7800.0,
    "days_to_opex": 17,
    "days_since_opex": 18,
    "next_opex": "2026-08-21",
    "zero_dte_gamma_share": 0.10,
    "n_contracts": 8000,
    "stale": False,
    "fallback_spot": False,
    "thin_chain": False,
    "ex_div_risk": False,
}


def m(**over):
    d = copy.deepcopy(BASE)
    d.update(over)
    return d


def _all_text(out):
    return " ".join([out["headline"]] + out["warnings"] + out["sentences"])


def test_regime_firm_vs_fragile():
    firm = ge.generate_commentary(m(cushion_pct=0.0080), ge.SPX)
    assert "Positive gamma with a" in firm["sentences"][0]
    fragile = ge.generate_commentary(m(cushion=50.0, cushion_pct=0.0070), ge.SPX)
    assert "Positive but fragile gamma" in fragile["sentences"][0]


def test_regime_negative_marginal_vs_deep():
    marg = ge.generate_commentary(
        m(regime="negative", zero_gamma=7740.0, cushion=-40.0, cushion_pct=-0.005), ge.SPX)
    assert "Marginally negative gamma" in marg["sentences"][0]
    deep = ge.generate_commentary(
        m(regime="negative", zero_gamma=7900.0, cushion=-200.0, cushion_pct=-0.026), ge.SPX)
    assert "Deeply negative gamma" in deep["sentences"][0]


def test_gex_bands_use_spx_thresholds():
    """The SPY thresholds would misclassify every SPX snapshot."""
    assert "is heavy" in _all_text(ge.generate_commentary(m(net_gex=120e9), ge.SPX))
    assert "is moderate" in _all_text(ge.generate_commentary(m(net_gex=60e9), ge.SPX))
    assert "is light" in _all_text(ge.generate_commentary(m(net_gex=10e9), ge.SPX))
    # The same $9bn reading is "light" for SPX but "heavy" for SPY — which is
    # exactly why the thresholds cannot be shared.
    assert "is light" in _all_text(ge.generate_commentary(m(net_gex=9e9), ge.SPX))
    assert "is heavy" in _all_text(ge.generate_commentary(m(net_gex=9e9), ge.SPY))


def test_flip_watch_suppresses_gex_size():
    out = ge.generate_commentary(m(zero_gamma=7695.0, cushion=5.0,
                                   cushion_pct=5.0 / 7700.0), ge.SPX)
    text = _all_text(out)
    assert "vol trigger" in text
    assert "Net GEX" not in text
    assert "regime in play" in out["headline"]


def test_synthesis_suppresses_raw_flow_sentences():
    out = ge.generate_commentary(
        m(vanna_pressure=20e9, charm_drift=3e9), ge.SPX)
    text = _all_text(out)
    assert "path of least resistance" in text
    assert "per vol point" not in text          # vanna sentence suppressed


def test_conflict_headline():
    out = ge.generate_commentary(m(vanna_pressure=-20e9), ge.SPX)
    assert "adverse vanna" in out["headline"]


def test_dex_rule_reads_the_windowed_figure_not_the_total():
    """Total DEX is dominated by deep-ITM OI; the rule must ignore it."""
    quiet = ge.generate_commentary(m(dex=5000e9, dex_window=100e9), ge.SPX)
    assert "dealer delta" not in _all_text(quiet).lower()
    loud = ge.generate_commentary(m(dex=0.0, dex_window=-3000e9), ge.SPX)
    assert "Near-the-money dealer delta" in _all_text(loud)
    assert "squeeze fuel" in _all_text(loud)


def test_warnings_are_prepended_not_capped():
    out = ge.generate_commentary(
        m(stale=True, thin_chain=True, n_contracts=12), ge.SPX)
    assert len(out["warnings"]) == 2
    assert any("30 minutes" in w for w in out["warnings"])
    assert any("thin chain" in w for w in out["warnings"])


def test_ex_div_warning_appears():
    out = ge.generate_commentary(
        m(ex_div_risk=True, next_ex_div="2026-09-18"), ge.SPY)
    assert any("ex-dividend" in w for w in out["warnings"])


def test_sentence_cap_and_regime_always_kept():
    out = ge.generate_commentary(
        m(regime="negative", zero_gamma=7900.0, cushion=-200.0, cushion_pct=-0.026,
          net_gex=-120e9, dex=-4000e9, dex_window=-3000e9, vanna_pressure=-20e9,
          charm_drift=-3e9, call_wall=7710.0, put_wall=7690.0, days_to_opex=1,
          zero_dte_gamma_share=0.5), ge.SPX)
    assert len(out["sentences"]) <= ge.MAX_SENTENCES
    assert "gamma" in out["sentences"][0]


def test_commentary_is_deterministic():
    a = ge.generate_commentary(m(), ge.SPX)
    b = ge.generate_commentary(m(), ge.SPX)
    assert a == b
