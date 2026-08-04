"""Tests for the VIX term structure panel.

The put-call parity extraction is the piece that has to be right — every other
number derives from it. Fixtures are shaped like the live CBOE VIX chain and
carry the term structure actually observed on 2026-08-04.

    pytest panels/test_vix_structure.py
"""

from __future__ import annotations

from datetime import date

import numpy as np

from panels import vix_structure as vs

TODAY = date(2026, 8, 4)

# The live curve on 2026-08-04 (see DATA_SOURCES.md): spot VIX 16.30, monthly
# expiries 19 Aug and 16 Sep, weeklies in between.
LIVE_CURVE = [
    ("260805", "VIXW", 16.0, 0.42, 0.17),    # 1d  -> ~16.25
    ("260812", "VIXW", 17.0, 0.77, 0.86),    # 8d  -> ~16.91
    ("260819", "VIX", 18.0, 1.08, 1.23),     # 15d -> ~17.86  VX1
    ("260826", "VIXW", 18.0, 1.48, 1.16),    # 22d -> ~18.32
    ("260916", "VIX", 19.0, 2.04, 1.95),     # 43d -> ~19.09  VX2
    ("261021", "VIX", 20.0, 2.72, 2.55),     # 78d -> ~20.17
]

INDEX_QUOTES = {
    "vix1d": {"symbol": "^VIX1D", "value": 9.99, "prev_close": 9.43, "tenor_days": 1},
    "vix9d": {"symbol": "^VIX9D", "value": 14.54, "prev_close": 13.28, "tenor_days": 9},
    "vix": {"symbol": "^VIX", "value": 16.30, "prev_close": 15.86, "tenor_days": 30},
    "vix3m": {"symbol": "^VIX3M", "value": 19.06, "prev_close": 18.93, "tenor_days": 93},
    "vix6m": {"symbol": "^VIX6M", "value": 21.24, "prev_close": 21.20, "tenor_days": 186},
    "vvix": {"symbol": "^VVIX", "value": 89.31, "prev_close": 90.81, "tenor_days": None},
}


def _sym(root, exp, right, strike):
    return f"{root}{exp}{right}{int(round(strike * 1000)):08d}"


def build_chain(curve=None, spot=16.30):
    curve = curve if curve is not None else LIVE_CURVE
    opts = []
    for exp, root, k, c, p in curve:
        # A few surrounding strikes so the "most ATM" selection has real work.
        for off in (-2.0, -1.0, 0.0, 1.0, 2.0):
            strike = k + off
            # Intrinsic-ish skew away from the ATM pair keeps |C-P| minimal at
            # the intended strike.
            cc, pp = c - off * 0.55, p + off * 0.55
            if cc <= 0 or pp <= 0:
                continue
            opts.append({"option": _sym(root, exp, "C", strike),
                         "bid": cc - 0.02, "ask": cc + 0.02})
            opts.append({"option": _sym(root, exp, "P", strike),
                         "bid": pp - 0.02, "ask": pp + 0.02})
    return {"timestamp": "2026-08-04 16:07:21",
            "data": {"current_price": spot, "options": opts}}


# --------------------------------------------------------------------------- #
# Put-call parity extraction
# --------------------------------------------------------------------------- #

def test_forwards_match_the_live_curve():
    fwd = vs.forwards_from_chain(build_chain(), TODAY)
    got = {f["expiry"]: f["forward"] for f in fwd}
    expected = {
        "2026-08-05": 16.25, "2026-08-12": 16.91, "2026-08-19": 17.86,
        "2026-08-26": 18.32, "2026-09-16": 19.09, "2026-10-21": 20.17,
    }
    for exp, want in expected.items():
        # Parity plus the e^(rT) discount; tolerance covers the discount factor.
        assert abs(got[exp] - want) < 0.06, f"{exp}: {got[exp]} vs {want}"


def test_forward_is_monotonic_and_sorted_by_days():
    fwd = vs.forwards_from_chain(build_chain(), TODAY)
    days = [f["days"] for f in fwd]
    assert days == sorted(days)
    fs = [f["forward"] for f in fwd]
    assert fs == sorted(fs)          # this particular curve is in contango


def test_monthly_roots_are_flagged():
    fwd = vs.forwards_from_chain(build_chain(), TODAY)
    monthly = [f["expiry"] for f in fwd if f["is_monthly"]]
    assert monthly == ["2026-08-19", "2026-09-16", "2026-10-21"]
    weekly = [f["expiry"] for f in fwd if not f["is_monthly"]]
    assert "2026-08-12" in weekly


def test_expired_contracts_are_dropped():
    curve = LIVE_CURVE + [("260801", "VIXW", 16.0, 0.4, 0.2)]   # 3 days ago
    fwd = vs.forwards_from_chain(build_chain(curve), TODAY)
    assert all(f["days"] >= 0 for f in fwd)
    assert "2026-08-01" not in {f["expiry"] for f in fwd}


def test_one_sided_quotes_are_ignored():
    chain = build_chain()
    for o in chain["data"]["options"]:
        if o["option"].startswith("VIX260819"):
            o["bid"] = 0.0            # kill the whole 19 Aug expiry
    fwd = vs.forwards_from_chain(chain, TODAY)
    assert "2026-08-19" not in {f["expiry"] for f in fwd}


def test_parity_uses_the_discount_factor():
    """F = K + e^(rT)(C-P), not K + (C-P) — matters at the long end."""
    curve = [("270120", "VIX", 22.0, 3.40, 3.67)]
    fwd = vs.forwards_from_chain(build_chain(curve), TODAY)[0]
    T = fwd["days"] / 365.0
    undiscounted = 22.0 + (3.40 - 3.67)
    expected = 22.0 + float(np.exp(vs.R_RATE * T)) * (3.40 - 3.67)
    assert abs(fwd["forward"] - expected) < 1e-4      # payload rounds to 4dp
    assert abs(fwd["forward"] - undiscounted) > 1e-3


# --------------------------------------------------------------------------- #
# Interpolation
# --------------------------------------------------------------------------- #

def test_interpolate_linear_in_days():
    curve = [{"days": 15, "forward": 17.86}, {"days": 43, "forward": 19.09}]
    got = vs.interpolate(curve, 30.0)
    want = 17.86 + (30 - 15) / (43 - 15) * (19.09 - 17.86)
    assert abs(got - want) < 1e-9


def test_interpolate_clamps_outside_the_curve():
    curve = [{"days": 15, "forward": 17.0}, {"days": 43, "forward": 19.0}]
    assert vs.interpolate(curve, 1.0) == 17.0
    assert vs.interpolate(curve, 400.0) == 19.0
    assert vs.interpolate([], 30.0) is None
    assert vs.interpolate([{"days": 20, "forward": 18.0}], 30.0) == 18.0


def test_constant_maturity_avoids_the_roll_sawtooth():
    """CM30 must move smoothly as the front contract ages toward expiry.

    Stepping the valuation date forward past a monthly roll should leave CM30
    nearly unchanged while VX1 jumps to the next contract — the whole reason
    RESEARCH.md insists on constant maturity.
    """
    monthlies = [{"days": 1, "forward": 17.80}, {"days": 29, "forward": 19.05},
                 {"days": 64, "forward": 20.10}]
    before = vs.interpolate(monthlies, 30.0)
    # One session later the front contract has expired and rolled off.
    rolled = [{"days": 28, "forward": 19.05}, {"days": 63, "forward": 20.10}]
    after = vs.interpolate(rolled, 30.0)
    assert abs(after - before) < 0.15          # CM30 barely moves
    assert abs(rolled[0]["forward"] - monthlies[0]["forward"]) > 1.0   # VX1 jumped


# --------------------------------------------------------------------------- #
# Compute / classification
# --------------------------------------------------------------------------- #

def test_compute_contango():
    p = vs.compute(INDEX_QUOTES, build_chain(), {}, TODAY)
    assert p["structure"] == "contango"
    assert p["vx1"]["expiry"] == "2026-08-19"
    assert p["vx2"]["expiry"] == "2026-09-16"
    assert p["basis"] < 0                       # spot below front future
    assert p["days_to_vix_expiry"] == 15
    assert p["expiry_rule_ok"] is True          # matches calendar_context
    assert 17.8 < p["cm30"] < 19.2
    assert p["flags"]["backwardation"] is False
    assert p["flags"]["vix3m_inverted"] is False
    assert "Contango" in p["commentary"]["headline"]


def test_compute_backwardation():
    inverted = [
        ("260819", "VIX", 18.0, 1.00, 2.60),     # forward ~16.4
        ("260916", "VIX", 17.0, 1.00, 2.10),     # forward ~15.9
    ]
    quotes = {k: dict(v) for k, v in INDEX_QUOTES.items()}
    quotes["vix"]["value"] = 28.0
    quotes["vix3m"]["value"] = 24.0
    quotes["vix9d"]["value"] = 33.0
    quotes["vvix"]["value"] = 130.0
    p = vs.compute(quotes, build_chain(inverted, spot=28.0), {}, TODAY)
    assert p["structure"] == "backwardation"
    assert p["basis"] > 0
    assert p["flags"]["vix3m_inverted"] is True
    assert p["flags"]["near_term_event"] is True
    assert p["flags"]["vvix_elevated"] is True
    assert "Backwardation" in p["commentary"]["headline"]
    assert any("outrunning supply" in s for s in p["commentary"]["sentences"])


def test_flat_curve_band():
    flat = [("260819", "VIX", 16.0, 1.20, 0.95)]     # forward ~16.25
    quotes = {k: dict(v) for k, v in INDEX_QUOTES.items()}
    quotes["vix"]["value"] = 16.20
    p = vs.compute(quotes, build_chain(flat, spot=16.20), {}, TODAY)
    assert p["structure"] == "flat"
    assert "Flat curve" in p["commentary"]["headline"]


def test_settlement_cross_check():
    settle = {"2026-09-16": 19.1787, "2026-10-21": 20.2239}
    p = vs.compute(INDEX_QUOTES, build_chain(), settle, TODAY)
    assert len(p["cross_check"]) == 2
    assert p["cross_check_max_diff"] < 0.15         # matches live agreement
    assert p["flags"]["curve_disagrees_with_settlement"] is False
    assert p["settlement_available"] is True


def test_settlement_disagreement_is_flagged():
    p = vs.compute(INDEX_QUOTES, build_chain(), {"2026-09-16": 25.0}, TODAY)
    assert p["flags"]["curve_disagrees_with_settlement"] is True
    assert any("settlement" in w for w in p["commentary"]["warnings"])


def test_untraded_weeklies_do_not_raise_a_false_disagreement():
    """The real 2026-08-04 artifact: six weeklies all settled at 17.9466.

    Those are reference prices for contracts that did not trade, so they must
    not be treated as the market disagreeing with the parity curve.
    """
    settle = {
        "2026-08-05": 17.9466, "2026-08-12": 17.9466, "2026-08-19": 17.9466,
        "2026-08-26": 17.9466, "2026-09-16": 19.1787, "2026-10-21": 20.2239,
    }
    p = vs.compute(INDEX_QUOTES, build_chain(), settle, TODAY)
    # The weekly rows are still shown...
    weeklies = [x for x in p["cross_check"] if not x["is_monthly"]]
    assert weeklies and max(abs(x["diff"]) for x in weeklies) > 1.0
    # ...but only monthlies decide agreement, and those match.
    assert p["cross_check_max_diff"] < 0.35
    assert p["flags"]["curve_disagrees_with_settlement"] is False
    assert not any("settlement" in w for w in p["commentary"]["warnings"])


def test_missing_settlement_is_warned_not_fatal():
    p = vs.compute(INDEX_QUOTES, build_chain(), {}, TODAY)
    assert p["settlement_available"] is False
    assert any("unverified" in w for w in p["commentary"]["warnings"])


def test_expiry_rule_mismatch_is_flagged():
    """If the chain's front monthly disagrees with the calendar rule, say so."""
    odd = [("260818", "VIX", 18.0, 1.08, 1.23)]     # a Tuesday, not the rule date
    p = vs.compute(INDEX_QUOTES, build_chain(odd), {}, TODAY)
    assert p["expiry_rule_ok"] is False
    assert p["flags"]["expiry_rule_mismatch"] is True
    assert any("calendar rule" in w for w in p["commentary"]["warnings"])


def test_no_forwards_raises():
    empty = {"timestamp": "x", "data": {"current_price": 16.3, "options": []}}
    try:
        vs.compute(INDEX_QUOTES, empty, {}, TODAY)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "forwards" in str(exc)


def test_index_curve_is_sorted_by_tenor():
    p = vs.compute(INDEX_QUOTES, build_chain(), {}, TODAY)
    days = [x["days"] for x in p["index_curve"]]
    assert days == sorted(days)
    assert [x["label"] for x in p["index_curve"]] == [
        "VIX1D", "VIX9D", "VIX", "VIX3M", "VIX6M"]
    assert "VVIX" not in {x["label"] for x in p["index_curve"]}   # not a tenor
