"""Tests for the CFTC trader-positioning panel — pure math only, no network.

Covers the normalisation (net, percentile, z-score, WoW), the state mapping
INCLUDING the VIX inversion (net-short -> fragile), the plain-English sentence,
the as-of price alignment, and an end-to-end compute_contract / build_payload
over synthetic in-memory data. The fetch_* helpers (the only networked code)
are not exercised here.

Run standalone:  python -m panels.test_cftc_positioning
Or with pytest:  pytest panels/test_cftc_positioning.py
"""

from __future__ import annotations

from datetime import date

from panels import cftc_positioning as c


def _rows(net_long_short, long_key, short_key, start=date(2024, 1, 2)):
    """Build raw TFF-style rows from a list of (long, short) tuples, weekly."""
    from datetime import timedelta
    out = []
    d = start
    for lo, sh in net_long_short:
        out.append({c.F_DATE: d.isoformat() + "T00:00:00.000",
                    long_key: str(lo), short_key: str(sh)})
        d += timedelta(days=7)
    return out


# --------------------------------------------------------------------------- #
# percentile / z-score
# --------------------------------------------------------------------------- #

def test_percentile_rank_extremes():
    win = [float(x) for x in range(100)]  # 0..99
    assert c.percentile_rank(win, 99) == 100.0      # all <= max
    assert c.percentile_rank(win, -10) == 0.0       # none <= below-min
    assert 45 <= c.percentile_rank(win, 49) <= 55   # middle ~50


def test_percentile_rank_empty_is_mid():
    assert c.percentile_rank([], 5.0) == 50.0


def test_zscore_basic():
    assert c.zscore([10, 10, 10], 10) == 0.0        # zero variance
    assert c.zscore([0, 2], 4) > 0                  # above mean -> positive
    assert c.zscore([5], 5) is None                 # too few obs


# --------------------------------------------------------------------------- #
# state mapping incl. VIX inversion
# --------------------------------------------------------------------------- #

def test_state_equity_index():
    assert c.classify_state(90, invert=False) == "crowded"
    assert c.classify_state(10, invert=False) == "squeeze"
    assert c.classify_state(50, invert=False) == "neutral"


def test_state_vix_inverted():
    # low percentile = heavily net-short vol = fragile (the crucial inversion)
    assert c.classify_state(10, invert=True) == "fragile"
    # high / mid percentile for VIX is NOT a crowding-long warning -> neutral
    assert c.classify_state(90, invert=True) == "neutral"
    assert c.classify_state(50, invert=True) == "neutral"


def test_vix_low_pctl_not_crowded():
    # a net-short VIX must never read as the equity "crowded" (long) state
    assert c.classify_state(5, invert=True) != "crowded"


# --------------------------------------------------------------------------- #
# sentence / wow / ordinal
# --------------------------------------------------------------------------- #

def test_wow_phrase_directions():
    assert c._wow_phrase(100, 10) == "funds added to longs"
    assert c._wow_phrase(100, -10) == "funds trimmed longs"
    assert c._wow_phrase(-100, 10) == "funds covered shorts"
    assert c._wow_phrase(-100, -10) == "funds added to shorts"


def test_ordinal():
    assert c._ordinal(1) == "1st"
    assert c._ordinal(2) == "2nd"
    assert c._ordinal(3) == "3rd"
    assert c._ordinal(11) == "11th"
    assert c._ordinal(42) == "42nd"
    assert c._ordinal(52) == "52nd"


def test_sentence_mentions_short_vol_for_vix_extreme():
    s = c.build_sentence("VIX Futures", net=-40000, wow=-2000, pctl=8.0,
                         invert=True)
    assert "short-vol" in s and "net-short" in s


# --------------------------------------------------------------------------- #
# price alignment (as-of)
# --------------------------------------------------------------------------- #

def test_align_price_asof():
    prices = [(date(2024, 1, 1), 100.0), (date(2024, 1, 8), 110.0)]
    rep = [date(2024, 1, 2), date(2024, 1, 9), date(2023, 12, 1)]
    out = c.align_price(rep, prices)
    assert out[0] == 100.0      # latest on/before Jan 2 -> Jan 1
    assert out[1] == 110.0      # latest on/before Jan 9 -> Jan 8
    assert out[2] == 100.0      # before all -> falls back to first


def test_align_price_empty():
    assert c.align_price([date(2024, 1, 1)], []) == [None]


# --------------------------------------------------------------------------- #
# end-to-end compute_contract / build_payload
# --------------------------------------------------------------------------- #

def test_compute_contract_full():
    # Leveraged funds net rises from -100 to +200 over the series; latest is the
    # max so it should land at the top percentile and "crowded".
    pairs = [(100, 200)] * 5 + [(300, 100)]   # nets: -100*5, then +200
    rows = _rows(pairs, c.F_LEV_LONG, c.F_LEV_SHORT)
    # add asset-manager fields onto the same rows
    for r in rows:
        r[c.F_AM_LONG] = "500"
        r[c.F_AM_SHORT] = "100"
    cfg = {"key": "sp500", "label": "E-mini S&P 500", "code": "X",
           "yf": "^GSPC", "invert": False}
    prices = [(date(2024, 1, 2), 4000.0)]
    out = c.compute_contract(cfg, rows, prices)

    assert out["key"] == "sp500"
    assert out["lev"]["net"] == 200
    assert out["lev"]["wow"] == 300            # -100 -> +200
    assert out["lev"]["state"] == "crowded"
    assert out["lev"]["verdict"]
    assert out["lev"]["sentence"]
    assert out["am"] is not None and out["am"]["net"] == 400
    assert "verdict" not in out["am"]          # secondary group has no verdict
    assert len(out["series"]) == len(rows)
    assert out["flags"]["price_missing"] is False


def test_build_payload_shape():
    cfg = {"key": "vix", "label": "VIX Futures", "code": "X",
           "yf": "^VIX", "invert": True}
    rows = _rows([(50, 100)] * 4, c.F_LEV_LONG, c.F_LEV_SHORT)
    for r in rows:
        r[c.F_AM_LONG] = "10"
        r[c.F_AM_SHORT] = "20"
    block = c.compute_contract(cfg, rows, [])
    payload = c.build_payload([block])
    assert payload["lookback_weeks"] == 156
    assert payload["as_of"] == block["series"][-1]["date"]
    assert payload["contracts"][0]["invert"] is True
    assert payload["contracts"][0]["flags"]["price_missing"] is True
    assert "Commitments of Traders" in payload["caveat"]


def test_determinism():
    cfg = {"key": "ndx", "label": "E-mini Nasdaq-100", "code": "X",
           "yf": "^NDX", "invert": False}
    rows = _rows([(100, 50), (120, 40)], c.F_LEV_LONG, c.F_LEV_SHORT)
    for r in rows:
        r[c.F_AM_LONG] = "5"; r[c.F_AM_SHORT] = "5"
    a = c.compute_contract(cfg, rows, [])
    b = c.compute_contract(cfg, rows, [])
    assert a == b


# --------------------------------------------------------------------------- #
# Standalone runner
# --------------------------------------------------------------------------- #

def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
        except Exception as exc:
            print(f"  FAIL {fn.__name__}: {exc}")
            continue
        passed += 1
        print(f"  ok  {fn.__name__}")
    print(f"\n{passed}/{len(fns)} cftc-positioning tests passed.")


if __name__ == "__main__":
    _run_all()
