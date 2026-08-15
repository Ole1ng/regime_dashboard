"""Tests for the options-derived volatility sentiment panel.

Built on synthetic chains with known answers rather than a captured payload, so
each metric is checked against a construction whose result is arguable from
first principles: a symmetric smile must give zero skew, a hand-built open
interest ladder has a max pain that can be computed by hand, and so on.

    pytest panels/test_vol_sentiment.py
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from panels import vol_sentiment as vs

TODAY = date(2026, 8, 15)
NOW = datetime(2026, 8, 15, 15, 0, tzinfo=timezone.utc)
SPOT = 100.0


def occ(symbol: str, expiry: date, right: str, strike: float) -> str:
    return f"{symbol}{expiry:%y%m%d}{right}{int(round(strike * 1000)):08d}"


def contract(expiry, right, strike, iv=0.30, delta=None, oi=100, volume=10,
             bid=1.0, ask=1.2):
    if delta is None:
        # Crude but monotonic stand-in: ATM ~0.5, deep OTM -> 0.
        moneyness = (strike - SPOT) / SPOT
        delta = max(0.01, min(0.99, 0.5 - moneyness * 4))
        if right == "P":
            delta = -(1.0 - delta)
    return {"option": occ("XYZ", expiry, right, strike), "iv": iv,
            "delta": delta, "open_interest": oi, "volume": volume,
            "bid": bid, "ask": ask}


def chain(options, iv30=32.0, spot=SPOT):
    return {"data": {"current_price": spot, "iv30": iv30, "options": options,
                     "security_type": "stock"}}


# --------------------------------------------------------------------------- #
# Units
# --------------------------------------------------------------------------- #

def test_iv30_is_normalised_from_percent_to_decimal():
    # CBOE quotes iv30 as 52.421 while per-contract iv is 0.524. Failing to
    # normalise makes every IV-vs-realised comparison wrong by 100x.
    exp = TODAY + timedelta(days=30)
    p = vs.compute(chain([contract(exp, "C", 100.0)], iv30=52.421), "XYZ", now=NOW)
    assert abs(p["iv30"] - 0.52421) < 1e-9


# --------------------------------------------------------------------------- #
# Skew
# --------------------------------------------------------------------------- #

def _wings(put_iv, call_iv, expiry):
    """A chain whose put and call wings sit at flat, distinct IVs."""
    out = []
    for strike in (70.0, 80.0, 90.0, 100.0):
        out.append(contract(expiry, "P", strike, iv=put_iv,
                            delta=-(strike - 60.0) / 80.0))
    for strike in (100.0, 110.0, 120.0, 130.0):
        out.append(contract(expiry, "C", strike, iv=call_iv,
                            delta=(140.0 - strike) / 80.0))
    return out


def test_symmetric_wings_give_zero_skew():
    exp = TODAY + timedelta(days=30)
    p = vs.compute(chain(_wings(0.30, 0.30, exp)), "XYZ", now=NOW)
    assert abs(p["skew_25d"]) < 1e-6
    assert p["skew_state"] == "balanced"


def test_rich_puts_give_positive_skew_and_a_put_bid_state():
    exp = TODAY + timedelta(days=30)
    p = vs.compute(chain(_wings(0.45, 0.30, exp)), "XYZ", now=NOW)
    assert p["skew_25d"] > 0
    # Normalised by ATM IV so the state is comparable across vol regimes.
    assert p["skew_25d_pct"] > vs.SKEW_PUT_BID
    assert p["skew_state"] == "put-bid"


def test_rich_calls_give_an_inverted_skew():
    exp = TODAY + timedelta(days=30)
    p = vs.compute(chain(_wings(0.25, 0.40, exp)), "XYZ", now=NOW)
    assert p["skew_25d"] < 0
    assert p["skew_state"] == "call-bid"


def test_skew_is_none_when_a_wing_is_too_thin_to_interpolate():
    exp = TODAY + timedelta(days=30)
    # Calls only: there is no put wing at all.
    only_calls = [contract(exp, "C", k, iv=0.3) for k in (100.0, 110.0, 120.0)]
    p = vs.compute(chain(only_calls), "XYZ", now=NOW)
    assert p["skew_25d"] is None
    assert p["skew_state"] == "unknown"


def test_zero_iv_contracts_are_excluded_from_the_wings():
    # CBOE writes iv: 0 for untraded contracts; treating those as real would
    # drag every interpolated wing toward zero.
    exp = TODAY + timedelta(days=30)
    opts = _wings(0.40, 0.30, exp) + [
        contract(exp, "P", 65.0, iv=0.0, delta=-0.10),
        contract(exp, "C", 135.0, iv=0.0, delta=0.10)]
    p = vs.compute(chain(opts), "XYZ", now=NOW)
    assert p["skew_put_iv"] == 0.40
    assert p["skew_call_iv"] == 0.30


# --------------------------------------------------------------------------- #
# Put/call ratios
# --------------------------------------------------------------------------- #

def test_put_call_ratios_are_computed_separately_for_oi_and_volume():
    exp = TODAY + timedelta(days=30)
    opts = [
        contract(exp, "C", 100.0, oi=1000, volume=10),
        contract(exp, "P", 100.0, oi=500, volume=90),
    ]
    p = vs.compute(chain(opts), "XYZ", now=NOW)
    assert p["pcr_oi"] == 0.5           # 500 / 1000
    assert p["pcr_vol"] == 9.0          # 90 / 10
    # The book is call-heavy while the flow is put-heavy — the disagreement is
    # the whole reason both are reported.
    assert p["pcr_oi_state"] == "call-heavy"
    assert p["pcr_vol_state"] == "put-heavy"


# --------------------------------------------------------------------------- #
# Term structure
# --------------------------------------------------------------------------- #

def test_term_structure_slope_and_states():
    front, back = TODAY + timedelta(days=7), TODAY + timedelta(days=60)
    rising = [contract(front, "C", 100.0, iv=0.25),
              contract(back, "C", 100.0, iv=0.35)]
    p = vs.compute(chain(rising), "XYZ", now=NOW)
    assert abs(p["term_slope"] - 0.10) < 1e-9
    assert p["term_state"] == "contango"

    falling = [contract(front, "C", 100.0, iv=0.50),
               contract(back, "C", 100.0, iv=0.30)]
    q = vs.compute(chain(falling), "XYZ", now=NOW)
    assert q["term_slope"] < 0
    assert q["term_state"] == "inverted"


# --------------------------------------------------------------------------- #
# Max pain
# --------------------------------------------------------------------------- #

def test_max_pain_matches_a_hand_computed_ladder():
    # All open interest sits on the 100 calls and 100 puts, so settlement at
    # 100 leaves everything worthless — max pain is unambiguously 100.
    exp = TODAY + timedelta(days=10)
    opts = [contract(exp, "C", 100.0, oi=1000),
            contract(exp, "P", 100.0, oi=1000),
            contract(exp, "C", 90.0, oi=1), contract(exp, "P", 110.0, oi=1),
            contract(exp, "C", 110.0, oi=1), contract(exp, "P", 90.0, oi=1)]
    p = vs.compute(chain(opts), "XYZ", now=NOW)
    assert p["max_pain"] == 100.0
    assert abs(p["max_pain_dist_pct"]) < 1e-9


def test_max_pain_is_pulled_toward_the_heaviest_open_interest():
    exp = TODAY + timedelta(days=10)
    opts = [contract(exp, "C", 90.0, oi=5000), contract(exp, "P", 90.0, oi=5000),
            contract(exp, "C", 100.0, oi=10), contract(exp, "P", 100.0, oi=10),
            contract(exp, "C", 110.0, oi=10), contract(exp, "P", 110.0, oi=10)]
    p = vs.compute(chain(opts), "XYZ", now=NOW)
    assert p["max_pain"] == 90.0
    assert p["max_pain_dist_pct"] < 0   # below a 100 spot


# --------------------------------------------------------------------------- #
# IV rank honesty
# --------------------------------------------------------------------------- #

def test_iv_rank_is_withheld_until_enough_history_exists():
    exp = TODAY + timedelta(days=30)
    short_history = [{"iv30": 0.3 + i * 0.001} for i in range(5)]
    p = vs.compute(chain([contract(exp, "C", 100.0)]), "XYZ",
                   history=short_history, now=NOW)
    # A percentile over five observations would be worse than no number.
    assert p["iv_rank"] is None
    assert p["history_days"] == 5
    assert p["history_needed"] == vs.MIN_HISTORY_DAYS


def test_iv_rank_is_reported_once_history_is_long_enough():
    exp = TODAY + timedelta(days=30)
    history = [{"iv30": v / 100.0} for v in range(10, 10 + vs.MIN_HISTORY_DAYS)]
    p = vs.compute(chain([contract(exp, "C", 100.0)], iv30=100.0), "XYZ",
                   history=history, now=NOW)
    # iv30 of 1.00 sits above every stored value, so both readings max out.
    assert p["iv_rank"] == 100.0
    assert p["iv_pctl"] == 100.0


def test_iv_rank_stays_within_0_and_100_at_a_fresh_extreme():
    # Regression: today's value used to be excluded from the min/max, so a new
    # high scored above 100 and a new low below 0.
    exp = TODAY + timedelta(days=30)
    history = [{"iv30": 0.30 + i * 0.001} for i in range(vs.MIN_HISTORY_DAYS)]
    for iv30_pct in (500.0, 1.0):
        p = vs.compute(chain([contract(exp, "C", 100.0)], iv30=iv30_pct), "XYZ",
                       history=history, now=NOW)
        assert 0.0 <= p["iv_rank"] <= 100.0, p["iv_rank"]


def test_iv_rank_sits_mid_range_for_a_mid_range_reading():
    exp = TODAY + timedelta(days=30)
    history = [{"iv30": v / 100.0} for v in range(20, 20 + vs.MIN_HISTORY_DAYS)]
    lo, hi = 0.20, (19 + vs.MIN_HISTORY_DAYS) / 100.0
    mid = (lo + hi) / 2
    p = vs.compute(chain([contract(exp, "C", 100.0)], iv30=mid * 100), "XYZ",
                   history=history, now=NOW)
    assert abs(p["iv_rank"] - 50.0) < 2.0


# --------------------------------------------------------------------------- #
# Robustness
# --------------------------------------------------------------------------- #

def test_foreign_roots_in_the_payload_are_rejected():
    # A CBOE payload for a short symbol can carry contracts belonging to a
    # different, longer-rooted name.
    exp = TODAY + timedelta(days=30)
    mine = contract(exp, "C", 100.0, oi=100)
    theirs = dict(mine, option=occ("XYZW", exp, "C", 100.0))
    p = vs.compute(chain([mine, theirs]), "XYZ", now=NOW)
    assert p["n_contracts"] == 1


def test_expired_contracts_are_dropped():
    past, future = TODAY - timedelta(days=5), TODAY + timedelta(days=30)
    p = vs.compute(chain([contract(past, "C", 100.0),
                          contract(future, "C", 100.0)]), "XYZ", now=NOW)
    assert p["n_contracts"] == 1


def test_empty_chain_degrades_without_raising():
    p = vs.compute(chain([]), "XYZ", now=NOW)
    assert p["n_contracts"] == 0
    assert p["skew_25d"] is None and p["max_pain"] is None
    assert p["thin_chain"] is True
    assert p["commentary"]["headline"]


def test_missing_price_history_leaves_realised_vol_none():
    exp = TODAY + timedelta(days=30)
    p = vs.compute(chain([contract(exp, "C", 100.0)]), "XYZ", prices=None, now=NOW)
    assert p["rv20"] is None
    assert p["ivrv_spread"] is None
    assert p["ivrv_state"] == "unknown"
