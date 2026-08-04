"""Print live gamma-engine magnitudes so the commentary thresholds can be
checked against reality rather than guessed.

    python tools/calibrate.py

The SPX thresholds were seeded at 10x market_almanack's SPY values (see
test_gamma_engine.test_spx_gex_is_ten_times_spy_at_equal_open_interest for why
10x and not 100x), then confirmed against the output of this script.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from panels import gamma_engine as ge   # noqa: E402


def band(name: str, value: float, moderate: float, heavy: float) -> str:
    a = abs(value)
    where = "LIGHT" if a < moderate else ("MODERATE" if a <= heavy else "HEAVY")
    return (f"    {name:<22} {ge.fmt_usd(value):>12}   "
            f"[{where}]  bands {ge.fmt_usd(moderate)} / {ge.fmt_usd(heavy)}")


def run(cfg: ge.Underlying) -> None:
    print(f"\n{'=' * 68}\n{cfg.label}\n{'=' * 68}")
    m = ge.compute(ge.fetch_chain(cfg), cfg)
    print(f"  snapshot {m['snapshot_ts']}   spot {m['spot']:,.2f} "
          f"({m['spot_source']})")
    print(f"  contracts {m['n_contracts']:,}   total OI {m['total_oi']:,}   "
          f"roots {m['root_counts']}")
    print(f"  regime {m['regime']}   flip "
          f"{m['zero_gamma'] if m['zero_gamma'] is not None else 'none'}   "
          f"cushion {'n/a' if m['cushion_pct'] is None else f'{m['cushion_pct']*100:.2f}%'}")
    print()
    print(band("net GEX /1%", m["net_gex"], cfg.gex_moderate, cfg.gex_heavy))
    print(f"    {'DEX (total)':<22} {ge.fmt_usd(m['dex']):>12}")
    print(f"    {'DEX (+/-6% window)':<22} {ge.fmt_usd(m['dex_window']):>12}   "
          f"band {ge.fmt_usd(cfg.dex_band)}")
    print(f"    {'vanna /vol pt':<22} {ge.fmt_usd(m['vanna_pressure']):>12}   "
          f"band {ge.fmt_usd(cfg.vanna_band)}")
    print(f"    {'charm flow /day':<22} {ge.fmt_usd(m['charm_drift']):>12}   "
          f"band {ge.fmt_usd(cfg.charm_band)}")
    print(f"    {'(book delta /day)':<22} "
          f"{ge.fmt_usd(m['charm_book_delta_per_day']):>12}")
    print()
    print(f"  call wall {m['call_wall']}   put wall {m['put_wall']}   "
          f"magnets {[x['strike'] for x in m['oi_magnets']]}")
    print(f"  0DTE gamma share {m['zero_dte_gamma_share'] * 100:.1f}%")
    print("  expiry buckets:")
    for b in m["expiry_buckets"]:
        print(f"    {b['label']:<18} {ge.fmt_usd(b['net_gex']):>12}  "
              f"{b['abs_share'] * 100:5.1f}% of |gamma|  ({b['n_contracts']:,} contracts)")
    print("  charm projection (hedging flow, + = dealers buy):")
    for r in m["charm_projection"]["series"]:
        mark = "  <- OPEX" if r["is_opex"] else ""
        print(f"    d{r['day']:<2} {r['date']}  {ge.fmt_usd(r['charm_per_day']):>10}/day  "
              f"cum {ge.fmt_usd(r['cum_hedge_flow']):>10}  "
              f"expiring {ge.fmt_usd(r['expiry_delta_released']):>10}  "
              f"({r['contracts_alive']:,} alive){mark}")
    assert abs(m["charm_drift"]
               - m["charm_projection"]["series"][0]["charm_per_day"]) < 1e-6, \
        "headline charm must equal the first projection bar"
    if m["commentary"]:
        print(f"\n  HEADLINE: {m['commentary']['headline']}")
        for w in m["commentary"]["warnings"]:
            print(f"    {w}")
        for s in m["commentary"]["sentences"]:
            print(f"    - {s}")


if __name__ == "__main__":
    for c in (ge.SPX, ge.SPY):
        run(c)
