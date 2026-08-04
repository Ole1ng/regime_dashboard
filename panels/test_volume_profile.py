"""Tests for the volume profile / auction structure panel.

Profiles are built from synthetic bars whose answers are known by construction,
so POC / value area / naked POC / LVN can be asserted exactly.

    pytest panels/test_volume_profile.py
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from panels import volume_profile as vp


def bars(rows, start=datetime(2026, 8, 3, 9, 30)):
    """rows = [(open, high, low, close, volume), ...] at 5-minute spacing."""
    idx = [start + timedelta(minutes=5 * i) for i in range(len(rows))]
    return pd.DataFrame(
        [{"Open": o, "High": h, "Low": lo, "Close": c, "Volume": v}
         for o, h, lo, c, v in rows],
        index=pd.DatetimeIndex(idx))


def session(day, rows):
    return bars(rows, start=datetime(day.year, day.month, day.day, 9, 30))


# --------------------------------------------------------------------------- #
# Profile construction
# --------------------------------------------------------------------------- #

def test_volume_is_spread_across_each_bar_range():
    """A bar spanning several bins contributes evenly to each."""
    p = vp.build_profile(bars([(100.0, 100.3, 100.0, 100.2, 1200.0)]),
                         bin_size=0.10)
    # 100.00-100.30 covers bins 100.0x, 100.1x, 100.2x, 100.3x -> 4 bins
    assert len(p["bins"]) == 4
    assert all(abs(v - 300.0) < 1e-6 for v in p["bins"].values())
    assert abs(p["total"] - 1200.0) < 1e-6
    assert p["low"] == 100.0 and p["high"] == 100.3


def test_bin_index_is_robust_at_edges():
    """99.8/0.1 is 997.9999999999999 in IEEE, which naively floors to 997.

    A one-bin shift moves a whole session's profile and can relocate the POC,
    so edges must be deterministic.
    """
    assert vp._bin_index(99.8, 0.10) == 998
    assert vp._bin_index(100.0, 0.10) == 1000
    assert vp._bin_index(100.3, 0.10) == 1003
    assert vp._bin_index(768.45, 0.05) == 15369
    # ...and the same for the vectorised path used on real bar arrays.
    import numpy as np
    assert list(vp._bin_index(np.array([99.8, 100.0, 100.3]), 0.10)) == [
        998, 1000, 1003]


def test_poc_is_the_heaviest_price():
    p = vp.build_profile(bars([
        (100.0, 100.0, 100.0, 100.0, 100.0),
        (101.0, 101.0, 101.0, 101.0, 900.0),     # clear winner
        (102.0, 102.0, 102.0, 102.0, 100.0),
    ]), bin_size=0.10)
    poc, vah, val = vp.value_area(p["bins"])
    assert abs(poc - 101.05) < 0.06


def test_value_area_covers_about_seventy_percent():
    rows = []
    # A tent-shaped distribution centred at 100.50.
    for i in range(21):
        price = 100.0 + i * 0.05
        weight = 100.0 - abs(i - 10) * 8
        rows.append((price, price, price, price, float(max(weight, 5))))
    p = vp.build_profile(bars(rows), bin_size=0.05)
    poc, vah, val = vp.value_area(p["bins"], coverage=0.70)
    inside = sum(v for k, v in p["bins"].items() if val <= k <= vah)
    share = inside / p["total"]
    assert 0.68 <= share <= 0.90       # expands in pairs, so it overshoots a little
    assert val <= poc <= vah


def test_value_area_of_empty_profile():
    assert vp.value_area({}) == (None, None, None)


def test_single_price_profile():
    p = vp.build_profile(bars([(100.0, 100.0, 100.0, 100.0, 500.0)]))
    poc, vah, val = vp.value_area(p["bins"])
    assert poc is not None and vah == poc and val == poc


# --------------------------------------------------------------------------- #
# Low-volume nodes
# --------------------------------------------------------------------------- #

def test_lvn_finds_the_thin_corridor():
    """Two heavy shelves with a thin gap between them."""
    bins = {}
    for i in range(10):                       # heavy shelf 100.0-100.9
        bins[round(100.0 + i * 0.1, 2)] = 1000.0
    for i in range(5):                        # thin corridor 101.0-101.4
        bins[round(101.0 + i * 0.1, 2)] = 20.0
    for i in range(10):                       # heavy shelf 101.5-102.4
        bins[round(101.5 + i * 0.1, 2)] = 1000.0
    zones = vp.find_lvn(bins, bin_size=0.10)
    assert len(zones) == 1
    z = zones[0]
    assert abs(z["lo"] - 100.95) < 0.02
    assert abs(z["hi"] - 101.45) < 0.02
    assert z["bins"] == 5
    assert z["mean_volume_share"] < vp.LVN_THRESHOLD


def test_lvn_ignores_runs_that_are_too_short():
    bins = {round(100.0 + i * 0.1, 2): 1000.0 for i in range(20)}
    bins[100.5] = 10.0                        # a single thin bin
    assert vp.find_lvn(bins, bin_size=0.10) == []


def test_lvn_of_uniform_profile_is_empty():
    bins = {round(100.0 + i * 0.1, 2): 500.0 for i in range(30)}
    assert vp.find_lvn(bins, bin_size=0.10) == []


def test_lvn_ignores_the_thin_tails_of_the_profile():
    """Volume is always light at the extremes — that is an edge, not a corridor.

    Live SPY reported 770.30-771.30 as its thinnest corridor purely because
    that was the session high.
    """
    bins = {}
    for i in range(5):                        # thin tail at the bottom
        bins[round(99.5 + i * 0.1, 2)] = 20.0
    for i in range(20):                       # the only real shelf
        bins[round(100.0 + i * 0.1, 2)] = 1000.0
    for i in range(5):                        # thin tail at the top
        bins[round(102.0 + i * 0.1, 2)] = 20.0
    assert vp.find_lvn(bins, bin_size=0.10) == []


def test_lvn_between_two_shelves_survives_alongside_tails():
    bins = {}
    for i in range(4):                        # thin bottom tail (ignored)
        bins[round(99.6 + i * 0.1, 2)] = 20.0
    for i in range(10):                       # shelf
        bins[round(100.0 + i * 0.1, 2)] = 1000.0
    for i in range(4):                        # genuine corridor
        bins[round(101.0 + i * 0.1, 2)] = 20.0
    for i in range(10):                       # shelf
        bins[round(101.4 + i * 0.1, 2)] = 1000.0
    for i in range(4):                        # thin top tail (ignored)
        bins[round(102.4 + i * 0.1, 2)] = 20.0
    zones = vp.find_lvn(bins, bin_size=0.10)
    assert len(zones) == 1
    assert abs(zones[0]["lo"] - 100.95) < 0.02


# --------------------------------------------------------------------------- #
# Sessions, naked POCs, SPX conversion
# --------------------------------------------------------------------------- #

def _peaked(day, centre, low, high):
    """A session with a genuine volume peak at `centre`.

    Flat sessions (identical bars) leave the POC as an arbitrary tie-break, so
    every fixture here concentrates volume at one price and uses a single wide
    bar to set the session range.
    """
    rows = [(centre, centre, centre, centre, 5000.0)] * 6
    rows.append((centre, high, low, centre, 300.0))
    return session(day, rows)


def _three_sessions():
    """Day 1 auctions at 100, day 2 at 105, day 3 at 106.

    Day 1's POC at 100 is never traded through again -> naked.
    Day 2's POC at 105 IS inside day 3's 104.9-106.2 range -> not naked.
    """
    return pd.concat([
        _peaked(datetime(2026, 7, 29), 100.0, 99.8, 100.2),
        _peaked(datetime(2026, 7, 30), 105.0, 104.8, 105.2),
        _peaked(datetime(2026, 7, 31), 106.0, 104.9, 106.2),
    ])


def test_sessions_are_profiled_individually():
    p = vp.compute(_three_sessions(), spx_spot=1060.0, spy_spot=106.0)
    assert p["n_sessions"] == 3
    assert [s["date"] for s in p["sessions"]] == [
        "2026-07-29", "2026-07-30", "2026-07-31"]
    assert abs(p["sessions"][0]["poc"] - 100.0) < 0.2
    assert abs(p["sessions"][1]["poc"] - 105.0) < 0.2


def test_naked_poc_detection():
    p = vp.compute(_three_sessions(), spx_spot=1060.0, spy_spot=106.0)
    naked_dates = {n["date"] for n in p["naked_pocs"]}
    assert "2026-07-29" in naked_dates       # never revisited
    assert "2026-07-30" not in naked_dates   # day 3 traded down through it
    # The final session is never counted — nothing has come after it yet.
    assert "2026-07-31" not in naked_dates


def test_naked_pocs_sorted_by_distance_and_sided():
    p = vp.compute(_three_sessions(), spx_spot=1060.0, spy_spot=106.0)
    dists = [abs(n["spy"] - p["spy_spot"]) for n in p["naked_pocs"]]
    assert dists == sorted(dists)
    assert p["naked_pocs"][0]["above_spot"] is False    # 100 is below 106


def test_spx_conversion_uses_the_live_ratio():
    p = vp.compute(_three_sessions(), spx_spot=1060.0, spy_spot=106.0)
    assert p["ratio"] == 10.0
    for s in p["sessions"]:
        assert abs(s["poc_spx"] - s["poc"] * 10.0) < 0.01
    for n in p["naked_pocs"]:
        assert abs(n["spx"] - n["spy"] * 10.0) < 0.01
    assert abs(p["composite_poc_spx"] - p["composite_poc"] * 10.0) < 0.01


def test_value_area_position_flags():
    data = _three_sessions()
    inside = vp.compute(data, spx_spot=1055.0, spy_spot=105.5)
    above = vp.compute(data, spx_spot=2000.0, spy_spot=200.0)
    below = vp.compute(data, spx_spot=500.0, spy_spot=50.0)
    assert above["flags"]["above_value"] is True
    assert above["flags"]["in_value"] is False
    assert below["flags"]["below_value"] is True
    assert inside["flags"]["in_value"] or inside["flags"]["above_value"]


def test_commentary_mentions_the_rth_limitation_in_payload():
    p = vp.compute(_three_sessions(), spx_spot=1060.0, spy_spot=106.0)
    assert p["rth_only"] is True
    assert "overnight auction is not captured" in p["limitation"]
    assert p["commentary"]["headline"]


def test_chart_is_sorted_and_carries_spx_prices():
    p = vp.compute(_three_sessions(), spx_spot=1060.0, spy_spot=106.0)
    prices = [c["price"] for c in p["chart"]]
    assert prices == sorted(prices)
    assert all(abs(c["price_spx"] - c["price"] * 10.0) < 0.01 for c in p["chart"])
    assert abs(sum(c["share"] for c in p["chart"]) - 1.0) < 1e-6


def test_empty_bars_raise():
    try:
        vp.compute(pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"]),
                   spx_spot=7700.0, spy_spot=770.0)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "session" in str(exc)


def test_candles_are_aggregated_to_30_minutes():
    """Six 5m bars collapse into one 30m candle with OHLC taken correctly."""
    rows = [(100.0, 100.5, 99.5, 100.2, 1000.0),
            (100.2, 101.0, 100.1, 100.8, 1000.0),
            (100.8, 100.9, 100.3, 100.4, 1000.0),
            (100.4, 100.6, 100.0, 100.5, 1000.0),
            (100.5, 100.7, 100.2, 100.6, 1000.0),
            (100.6, 102.0, 100.4, 101.5, 1000.0)]     # 6 x 5m = one 30m bucket
    day = datetime(2026, 8, 3)
    p = vp.compute(session(day, rows), spx_spot=1015.0, spy_spot=101.5)
    cs = p["candles"]
    assert len(cs) == 1
    c = cs[0]
    assert c["o"] == 100.0            # first open
    assert c["c"] == 101.5            # last close
    assert c["h"] == 102.0            # max high
    assert c["l"] == 99.5             # min low
    assert c["v"] == 6000.0           # summed volume
    assert c["t"] == "2026-08-03 09:30"
    assert c["d"] == "2026-08-03"
    assert p["candle_interval"] == "30m"


def test_candles_split_across_buckets_and_sessions():
    rows = [(100.0, 100.1, 99.9, 100.0, 500.0)] * 9   # 45 min -> 2 buckets
    data = pd.concat([session(datetime(2026, 8, 3), rows),
                      session(datetime(2026, 8, 4), rows)])
    p = vp.compute(data, spx_spot=1000.0, spy_spot=100.0)
    cs = p["candles"]
    assert len(cs) == 4                                # 2 buckets x 2 sessions
    assert [c["d"] for c in cs] == ["2026-08-03"] * 2 + ["2026-08-04"] * 2
    assert [c["t"] for c in cs] == [
        "2026-08-03 09:30", "2026-08-03 10:00",
        "2026-08-04 09:30", "2026-08-04 10:00"]
    # Buckets never straddle a session boundary.
    for c in cs:
        assert c["t"].startswith(c["d"])


def test_candles_flag_the_composite_window():
    """Candles cover all profiled sessions; only the composite ones are flagged
    so the chart can shade the region the profile actually summarises."""
    frames = []
    for i in range(vp.SESSIONS):
        d = datetime(2026, 7, 6) + timedelta(days=i)
        frames.append(session(d, [(100.0 + i, 100.2 + i, 99.8 + i,
                                   100.0 + i, 1000.0)] * 6))
    p = vp.compute(pd.concat(frames), spx_spot=1090.0, spy_spot=109.0)
    days = sorted({c["d"] for c in p["candles"]})
    assert len(days) == vp.SESSIONS
    flagged = sorted({c["d"] for c in p["candles"] if c["in_composite"]})
    assert len(flagged) == vp.COMPOSITE_SESSIONS
    assert flagged == days[-vp.COMPOSITE_SESSIONS:]
    assert flagged[0] == p["composite_from"]
    assert flagged[-1] == p["composite_to"]


def test_candles_cover_the_same_price_range_as_the_sessions():
    p = vp.compute(_three_sessions(), spx_spot=1060.0, spy_spot=106.0)
    lo = min(c["l"] for c in p["candles"])
    hi = max(c["h"] for c in p["candles"])
    assert abs(lo - min(s["low"] for s in p["sessions"])) < 0.01
    assert abs(hi - max(s["high"] for s in p["sessions"])) < 0.01


def test_only_the_last_n_sessions_are_kept():
    frames = []
    for i in range(20):
        d = datetime(2026, 7, 1) + timedelta(days=i)
        frames.append(session(d, [(100.0 + i, 100.2 + i, 99.8 + i,
                                   100.0 + i, 1000.0)] * 4))
    p = vp.compute(pd.concat(frames), spx_spot=1190.0, spy_spot=119.0)
    assert p["n_sessions"] == vp.SESSIONS
    assert p["composite_sessions"] == vp.COMPOSITE_SESSIONS
