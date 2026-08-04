"""Live smoke test: run each panel's refresh() and print the payload summary.

    python tools/smoke.py [panel ...]

Panels: vix, correlation, profile, calendar, gamma, regime. No arguments runs
everything. Used to check a panel against real data before wiring it into the
app.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def show(title: str, payload: dict, keys: list[str] | None = None) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")
    if keys:
        for k in keys:
            v = payload.get(k)
            if isinstance(v, (list, dict)):
                v = json.dumps(v)[:300]
            print(f"  {k:28} {v}")
    c = payload.get("commentary")
    if c:
        print(f"\n  HEADLINE: {c['headline']}")
        for w in c.get("warnings", []):
            print(f"    {w}")
        for s in c.get("sentences", []):
            print(f"    - {s}")


def run_vix() -> None:
    from panels import vix_structure as vs
    p = vs.refresh()
    show("VIX TERM STRUCTURE", p, [
        "spot_vix", "structure", "basis", "cm30", "cm30_all_expiries",
        "roll_vx1_vx2", "vix1d", "vix9d", "vix3m", "vix6m", "vvix",
        "vix_vix3m", "vix9d_vix", "days_to_vix_expiry", "expiry_rule_ok",
        "n_monthlies", "cross_check_max_diff", "settlement_available", "flags"])
    print("\n  futures curve (put-call parity):")
    for f in p["futures_curve"][:12]:
        tag = "MONTHLY" if f["is_monthly"] else "weekly "
        print(f"    {f['expiry']}  {f['days']:4}d  {tag}  F={f['forward']:7.3f}  "
              f"K={f['atm_strike']:6.1f}  C={f['call']:5.2f} P={f['put']:5.2f}  "
              f"pairs={f['n_pairs']:3}  spread={f['spread']:.3f}")
    if p["cross_check"]:
        print("\n  cross-check vs CFE settlement:")
        for x in p["cross_check"][:8]:
            print(f"    {x['expiry']}  parity {x['parity']:7.3f}  "
                  f"settle {x['settlement']:7.3f}  diff {x['diff']:+.3f}")


def run_correlation() -> None:
    from panels import correlation as co
    p = co.refresh()
    show("IMPLIED CORRELATION", p, [
        "cor1m", "cor3m", "spread", "cor1m_pctl_2y", "cor1m_pctl_5y",
        "cor1m_pctl_all", "history_start", "history_days", "regime", "flags"])


def run_profile() -> None:
    from panels import volume_profile as vp
    p = vp.refresh()
    show("VOLUME PROFILE", p, [
        "spy_spot", "spx_spot", "ratio", "sessions", "interval",
        "composite_poc", "composite_vah", "composite_val",
        "naked_pocs", "lvn_zones", "flags"])


def run_calendar() -> None:
    from panels import calendar_context as cc
    p = cc.refresh()
    show("CALENDAR", p, [
        "today", "next_opex", "days_to_opex", "post_opex_week",
        "next_vix_expiry", "days_to_vix_expiry", "next_quarterly_opex",
        "is_triple_witching_next", "quarter_end", "events"])


def run_gamma() -> None:
    from panels import gamma_engine as ge
    for cfg in (ge.SPX, ge.SPY):
        p = ge.refresh(cfg)
        show(f"GAMMA {cfg.label}", p, [
            "spot", "regime", "zero_gamma", "cushion_pct", "net_gex", "dex",
            "dex_window", "vanna_pressure", "charm_drift", "call_wall",
            "put_wall", "zero_dte_gamma_share", "n_contracts"])


def run_regime() -> None:
    from panels import regime as rg
    from panels import (calendar_context as cc, correlation as co,
                        gamma_engine as ge, vix_structure as vs,
                        volume_profile as vp)
    spx = ge.refresh(ge.SPX)
    p = rg.compute({"gamma_spx": spx, "gamma_spy": ge.refresh(ge.SPY),
                    "vix_structure": vs.refresh(), "correlation": co.refresh(),
                    "volume_profile": vp.refresh(spx_spot=spx["spot"]),
                    "calendar": cc.refresh()})
    show("REGIME", p, ["regime", "label", "confidence", "invalidation",
                       "votes", "missing", "posture"])
    print("\n  confluence:")
    for lv in p["confluence"][:12]:
        print(f"    {lv['level']:>10.2f}  score {lv['score']}  "
              f"{lv['distance_pct']*100:+6.2f}%  {', '.join(lv['sources'])}")


RUNNERS = {"vix": run_vix, "correlation": run_correlation, "profile": run_profile,
           "calendar": run_calendar, "gamma": run_gamma, "regime": run_regime}

if __name__ == "__main__":
    names = sys.argv[1:] or list(RUNNERS)
    for n in names:
        if n not in RUNNERS:
            print(f"unknown panel {n!r}; choose from {list(RUNNERS)}")
            continue
        try:
            RUNNERS[n]()
        except Exception as exc:
            print(f"\n{n}: FAILED — {type(exc).__name__}: {exc}")
            raise
