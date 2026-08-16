"""Live smoke test: run each panel's refresh() and print the payload summary.

    python tools/smoke.py [panel ...]        # Tab 1, fixed subject
    python tools/smoke.py t2 [SYMBOL ...]    # Tab 2, per ticker
    python tools/smoke.py t3                 # Tab 3, news screener

Tab 1 panels: vix, correlation, profile, calendar, gamma, spy, cftc, regime.
No arguments runs all of them.

Tab 2 runs every ticker panel plus the composite for each symbol given
(default NVDA). Worth running against three shapes before trusting a change:

    python tools/smoke.py t2 NVDA WEN QQQ

NVDA is a deep, dense chain; WEN is a low-priced small cap with a thin chain,
an extreme short float and an activist filing; QQQ is an ETF, which has no
issuer filings and exercises that suppression path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Commentary warnings are prefixed with "⚠", which a Windows console running
# the legacy cp1252 codepage cannot encode — printing one raises
# UnicodeEncodeError and kills the run partway through. Force UTF-8 and fall
# back to replacement characters rather than losing the output.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover - non-standard stdout
    pass


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


def run_spy() -> None:
    from panels import spy_positioning as sp
    p = sp.refresh()
    show("SPY DEALER POSITIONING", p, [
        "spot", "regime", "zero_gamma", "cushion_pct", "net_gex", "dex",
        "vanna_pressure", "charm_drift", "call_wall", "put_wall",
        "zero_dte_gamma_share", "n_contracts", "bucket"])
    nets = [c["net_gex"] for c in p["chart"]]
    pos = sum(1 for v in nets if v >= 0)
    print(f"\n  chart: {len(nets)} buckets, {pos} net-positive / "
          f"{len(nets) - pos} net-negative")


def run_cftc() -> None:
    from panels import cftc_positioning as cf
    p = cf.refresh()
    show("CFTC TRADER POSITIONING", p, ["as_of", "lookback_weeks"])
    for ct in p["contracts"]:
        lev = ct["lev"]
        print(f"\n  {ct['label']}: net {lev['net']:+,} "
              f"({lev['pctl']:.0f} %ile, z={lev['z']}, WoW {lev['wow']:+,}) "
              f"-> {lev['state']}")
        print(f"    {lev['sentence']}")


def run_regime() -> None:
    from panels import regime as rg
    from panels import (calendar_context as cc, correlation as co,
                        gamma_engine as ge, spy_positioning as sp,
                        vix_structure as vs, volume_profile as vp)
    spx = ge.refresh(ge.SPX)
    # gamma_spy comes from spy_positioning, matching what app.py stores under
    # that key — using gamma_engine.SPY here would smoke-test a payload the
    # dashboard no longer produces.
    p = rg.compute({"gamma_spx": spx, "gamma_spy": sp.refresh(),
                    "vix_structure": vs.refresh(), "correlation": co.refresh(),
                    "volume_profile": vp.refresh(spx_spot=spx["spot"]),
                    "calendar": cc.refresh()})
    show("REGIME", p, ["regime", "label", "confidence", "invalidation",
                       "votes", "missing", "posture"])
    print("\n  confluence:")
    for lv in p["confluence"][:12]:
        print(f"    {lv['level']:>10.2f}  score {lv['score']}  "
              f"{lv['distance_pct']*100:+6.2f}%  {', '.join(lv['sources'])}")


# --------------------------------------------------------------------------- #
# Tab 2 — ticker sentiment. These take a symbol; everything above is fixed.
# --------------------------------------------------------------------------- #

def _t2_inputs(symbol: str):
    """Fetch the three shared inputs once, exactly as the route does."""
    from panels import _finviz, ticker_positioning as tp
    chain = quote = prices = None
    try:
        chain = tp.fetch_chain(symbol)
    except Exception as exc:
        print(f"  (no CBOE chain: {type(exc).__name__}: {exc})")
    try:
        quote = _finviz.fetch_quote(symbol)
    except Exception as exc:
        print(f"  (no Finviz quote: {type(exc).__name__}: {exc})")
    try:
        import yfinance as yf
        prices = yf.Ticker(symbol).history(period="1y")
    except Exception as exc:
        print(f"  (no price history: {type(exc).__name__}: {exc})")
    return chain, quote, prices


def run_t2(symbol: str = "NVDA") -> None:
    """Run every Tab 2 panel for one symbol and print each payload summary."""
    from panels import (ticker_events as ev, ticker_news as nw,
                        ticker_positioning as tp, ticker_sentiment as cs,
                        ticker_social as so, ticker_squeeze as sq,
                        vol_sentiment as vs)

    print(f"\n{'#' * 70}\n# TAB 2 — {symbol}\n{'#' * 70}")
    chain, quote, prices = _t2_inputs(symbol)
    panels: dict = {}

    if chain:
        panels["t2_positioning"] = tp.compute(chain, symbol)
        show(f"{symbol} DEALER POSITIONING", panels["t2_positioning"], [
            "spot", "regime", "zero_gamma", "cushion_pct", "net_gex", "dex",
            "gross_dex", "call_wall", "put_wall", "bucket", "display_pct",
            "n_contracts"])

        panels["t2_vol"] = vs.compute(chain, symbol, prices=prices, history=[])
        show(f"{symbol} OPTIONS & VOLATILITY", panels["t2_vol"], [
            "iv30", "atm_iv", "skew_25d", "skew_25d_pct", "skew_state",
            "pcr_oi", "pcr_vol", "term_slope", "term_state", "rv20",
            "ivrv_spread", "ivrv_state", "max_pain", "max_pain_dist_pct",
            "iv_rank", "history_days"])

    if quote:
        panels["t2_squeeze"] = sq.compute(quote, symbol)
        show(f"{symbol} SQUEEZE & OWNERSHIP", panels["t2_squeeze"], [
            "company", "spot", "market_cap", "short_float", "days_to_cover",
            "squeeze_score", "squeeze_band", "inst_own", "inst_trans",
            "insider_trans", "recom_label", "target_upside", "rsi",
            "rel_volume", "from_high", "perf_month"])

        panels["t2_news"] = nw.refresh(
            symbol, company=quote.get("company"), finviz_news=quote.get("news"))
        show(f"{symbol} NEWS", panels["t2_news"], [
            "count", "tone", "mean", "feeds", "themes", "errors"])

    panels["t2_social"] = so.refresh(symbol, history=[])
    show(f"{symbol} RETAIL CHATTER", panels["t2_social"], [
        "n", "bullish", "bearish", "untagged", "bull_pct", "untagged_mean",
        "blended", "tone", "unique_users", "velocity_state", "co_mentions"])

    panels["t2_events"] = ev.refresh(
        symbol, chain_json=chain, snapshot=panels.get("t2_squeeze"),
        prices=prices,
        security_type=((chain or {}).get("data") or {}).get("security_type"))
    show(f"{symbol} EARNINGS & CATALYSTS", panels["t2_events"], [
        "earnings_date", "earnings_when", "earnings_days_out", "implied_move",
        "implied_expiry", "covers_earnings", "move_ratio", "move_state",
        "historical_moves"])

    composite = cs.compute(panels, symbol)
    show(f"{symbol} COMPOSITE SENTIMENT", composite, [
        "composite", "band", "label", "confidence", "missing"])
    print("\n  sub-scores:")
    for v in composite["subscores"]:
        score = f"{v['score']:5.1f}" if v["available"] else "  n/a"
        print(f"    {v['label']:22} {score}  w {v['weight']:>2} "
              f"/ eff {v['weight_eff']:<5}  {v['reading']}")
    print("\n  divergences:")
    for d in composite["divergences"] or []:
        print(f"    [{d['severity']:5}] {d['label']}: {d['sentence']}")
    if not composite["divergences"]:
        print("    (none)")


# --------------------------------------------------------------------------- #
# Tab 3 — news screener
# --------------------------------------------------------------------------- #

def run_t3() -> None:
    """Run the whole screener and print each panel's two readings."""
    import time

    from panels import news_screener as ns

    print(f"\n{'#' * 70}\n# TAB 3 — NEWS SCREENER\n{'#' * 70}")
    started = time.time()
    panels = ns.refresh()
    print(f"  fetched in {time.time() - started:.1f}s")

    errors = next(iter(panels.values()))["errors"]
    print(f"  feed errors: {len(errors)}")
    for e in errors[:8]:
        print(f"    - {e}")

    print(f"\n  {'panel':<32} {'n':>3}  {'tone':>7}  {'asset':>7}  "
          f"{'cov':>5}  verdict")
    for payload in panels.values():
        asset = payload["asset"]
        score = "    —" if asset["score"] is None else f"{asset['score']:+.2f}"
        print(f"  {payload['title']:<32} {payload['count']:>3}  "
              f"{payload['mean']:+.3f}  {score:>7}  "
              f"{asset['coverage'] * 100:>4.0f}%  {asset['label']}"
              f"{'  [DIVERGENT]' if payload['divergence'] else ''}")

    for payload in panels.values():
        show(payload["title"].upper(), payload,
             ["count", "window_hours", "tone", "mean", "undated_dropped"])
        for row in payload["salient"][:4]:
            asset = "  n/a" if row["asset"] is None else f"{row['asset']:+.2f}"
            drivers = f"  [{', '.join(row['drivers'])}]" if row["drivers"] else ""
            print(f"    asset {asset}  tone {row['score']:+.2f}  "
                  f"{row['title'][:62]}{drivers}")
        if payload["quotes"]:
            strip = "  ".join(f"{q['label']} {q['display']} {q['delta']}"
                              for q in payload["quotes"])
            print(f"\n    {strip}")


RUNNERS = {"vix": run_vix, "correlation": run_correlation, "profile": run_profile,
           "calendar": run_calendar, "gamma": run_gamma, "spy": run_spy,
           "cftc": run_cftc, "regime": run_regime}

# Tab 2 runners take a symbol, so they are dispatched separately from the
# fixed-subject Tab 1 panels above.
TICKER_RUNNERS = {"t2": run_t2}

# Tab 3 takes no argument but is kept out of RUNNERS so that a bare
# `python tools/smoke.py` stays a Tab 1 run — the screener is 40 third-party
# requests and should be asked for explicitly.
TAB_RUNNERS = {"t3": run_t3}

if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] in TICKER_RUNNERS:
        # e.g. `python tools/smoke.py t2 WEN NVDA`
        symbols = args[1:] or ["NVDA"]
        for sym in symbols:
            TICKER_RUNNERS[args[0]](sym.upper())
        sys.exit(0)

    if args and args[0] in TAB_RUNNERS:
        TAB_RUNNERS[args[0]]()
        sys.exit(0)

    names = args or list(RUNNERS)
    for n in names:
        if n not in RUNNERS:
            print(f"unknown panel {n!r}; choose from "
                  f"{list(RUNNERS) + list(TICKER_RUNNERS) + list(TAB_RUNNERS)}")
            continue
        try:
            RUNNERS[n]()
        except Exception as exc:
            print(f"\n{n}: FAILED — {type(exc).__name__}: {exc}")
            raise
