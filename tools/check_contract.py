"""Verify the live payloads carry every field static/app.js reads.

The frontend and the panels are only coupled by the shape of these dicts, so a
renamed key fails silently as a blank card. This walks the required paths for
each renderer against a running server.

    python app.py                                   # in another shell
    python tools/check_contract.py                  # Tab 1 (default)
    python tools/check_contract.py --panels tab2    # after analysing a ticker
    python tools/check_contract.py --panels tab3    # after a screener refresh
    python tools/check_contract.py --panels all

Tabs 2 and 3 are opt-in because their panels hold no payload until they have
been refreshed at least once; requiring them by default would fail the Tab 1
check on a fresh database.

Exits non-zero if anything is missing.
"""

from __future__ import annotations

import json
import sys
import urllib.request

URL = "http://127.0.0.1:8020/api/state"

# panel_key -> list of required paths. "a.b" descends a dict; "a[].b" descends
# the first element of a list.
REQUIRED = {
    "regime": [
        "regime", "label", "confidence", "posture", "spot", "destabilisers",
        "invalidation.description", "invalidation.triggers",
        "votes[].signal", "votes[].reading",
        "confluence[].level", "confluence[].score", "confluence[].distance",
        "confluence[].distance_pct", "confluence[].sources", "confluence[].in_lvn",
        "commentary.headline", "commentary.sentences", "commentary.warnings",
    ],
    "calendar": [
        "window_days", "days_to_opex", "days_to_vix_expiry",
        "days_to_quarterly_opex", "days_to_quarter_end", "post_opex_week",
        "sessions_since_opex", "spy_ex_div_warning", "next_spy_ex_div",
        "jheqx_note", "events[].days", "events[].kind", "events[].label",
        "events[].date",
    ],
    "gamma_spx": [
        "spot", "regime", "zero_gamma", "cushion_pct", "net_gex", "charm_drift",
        "vanna_pressure", "zero_dte_gamma_share", "call_wall", "put_wall",
        "bucket", "snapshot_ts", "expiry_window_days", "n_contracts",
        "oi_magnets[].strike",
        "chart[].strike", "chart[].call_gex", "chart[].put_gex",
        "expiry_buckets[].label", "expiry_buckets[].net_gex",
        "expiry_buckets[].abs_share", "expiry_buckets[].n_contracts",
        "charm_projection.assumption",
        "charm_projection.series[].date", "charm_projection.series[].charm_per_day",
        "charm_projection.series[].cum_hedge_flow",
        "charm_projection.series[].contracts_alive",
        "charm_projection.series[].is_opex",
        "commentary.headline", "commentary.sentences",
    ],
    # Full SPY positioning panel (panels/spy_positioning.py). `spot`,
    # `call_wall` and `put_wall` are also read server-side by regime.confluence,
    # so they are load-bearing in two directions.
    "gamma_spy": [
        "spot", "regime", "zero_gamma", "cushion_pct", "net_gex", "dex",
        "vanna_pressure", "charm_drift", "zero_dte_gamma_share",
        "call_wall", "put_wall", "bucket", "snapshot_ts",
        "expiry_window_days", "n_contracts",
        "oi_magnets[].strike",
        "chart[].strike", "chart[].call_gex", "chart[].put_gex",
        "chart[].net_gex",
        "commentary.headline", "commentary.sentences", "commentary.warnings",
    ],
    "cftc_positioning": [
        "as_of", "caveat", "lookback_weeks",
        "contracts[].key", "contracts[].label", "contracts[].invert",
        "contracts[].am",
        "contracts[].lev.net", "contracts[].lev.pctl", "contracts[].lev.wow",
        "contracts[].lev.state", "contracts[].lev.verdict",
        "contracts[].lev.sentence",
        "contracts[].flags.stale", "contracts[].flags.short_history",
        "contracts[].flags.price_missing",
        "contracts[].series[].date", "contracts[].series[].lev_net",
        "contracts[].series[].price",
    ],
    "vix_structure": [
        "spot_vix", "structure", "basis", "cm30", "vix_vix3m", "vvix",
        "vix1d", "vix9d", "vix3m", "vix6m", "days_to_vix_expiry",
        "vx1.expiry", "vx1.forward", "vx2.expiry", "vx2.forward",
        "flags.vix3m_inverted", "flags.vvix_elevated", "flags.vvix_calm",
        "futures_curve[].days", "futures_curve[].forward",
        "futures_curve[].is_monthly", "futures_curve[].expiry",
        "index_curve[].days", "index_curve[].value", "index_curve[].label",
        "commentary.headline", "commentary.sentences",
    ],
    "correlation": [
        "cor1m", "cor3m", "cor1m_pctl_2y", "cor1m_pctl_all", "low_2y", "high_2y",
        "cor1m_change_pct", "cor1m_change", "cor1m_prev", "spread", "regime",
        "history_start", "history_days",
        "flags.extreme_low", "flags.extreme_high", "flags.spiking_from_lows",
        "flags.low", "flags.high", "flags.term_inverted",
        "spark[].date", "spark[].value",
        "commentary.headline", "commentary.sentences",
    ],
    "volume_profile": [
        "spy_spot", "spx_spot", "ratio", "composite_poc", "composite_vah",
        "composite_val", "composite_poc_spx", "composite_vah_spx",
        "composite_val_spx", "limitation", "composite_sessions",
        "composite_from", "composite_to", "interval", "bin_size",
        "naked_pocs", "lvn_zones", "n_sessions", "candle_interval",
        "chart[].price", "chart[].price_spx", "chart[].volume", "chart[].share",
        "candles[].t", "candles[].d", "candles[].o", "candles[].h",
        "candles[].l", "candles[].c", "candles[].in_composite",
        "commentary.headline", "commentary.sentences",
    ],
}

# Tab 2 is checked separately: its panels are empty until a ticker has been
# analysed, so requiring them unconditionally would fail the Tab 1 check on a
# fresh database. Select with `--panels tab2` (or `all`).
REQUIRED_TAB2 = {
    "t2_sentiment": [
        "symbol", "composite", "confidence", "band", "label", "missing",
        "caveat", "flags.amplifying",
        "subscores[].key", "subscores[].label", "subscores[].score",
        "subscores[].weight", "subscores[].weight_eff", "subscores[].available",
        "subscores[].reading",
        "divergences[].key", "divergences[].severity", "divergences[].label",
        "divergences[].sentence",
        "commentary.headline", "commentary.sentences", "commentary.warnings",
    ],
    "t2_positioning": [
        "symbol", "spot", "regime", "zero_gamma", "cushion_pct", "net_gex",
        "dex", "gross_dex", "vanna_pressure", "charm_drift", "call_wall",
        "put_wall", "bucket", "display_pct", "snapshot_ts", "n_contracts",
        "zero_dte_gamma_share", "expiry_window_days",
        "oi_magnets[].strike", "oi_magnets[].oi",
        "chart[].strike", "chart[].call_gex", "chart[].put_gex",
        "commentary.headline", "commentary.sentences",
    ],
    "t2_vol": [
        "symbol", "spot", "n_contracts", "iv30", "atm_iv",
        "skew_25d", "skew_25d_pct", "skew_state", "skew_expiry",
        "pcr_oi", "pcr_vol", "pcr_oi_state", "pcr_vol_state",
        "term_slope", "term_state", "rv20", "ivrv_spread", "ivrv_state",
        "max_pain", "max_pain_dist_pct", "iv_rank", "history_days",
        "history_needed", "thin_chain",
        "term[].expiry", "term[].dte", "term[].atm_iv", "term[].n",
        "commentary.headline", "commentary.sentences",
    ],
    "t2_squeeze": [
        "symbol", "company", "spot", "market_cap", "short_float",
        "short_interest", "days_to_cover", "squeeze_score", "squeeze_band",
        "inst_own", "inst_trans", "insider_own", "insider_trans",
        "target_price", "target_upside", "recom", "recom_label",
        "rsi", "rel_volume", "sma20", "sma50", "sma200",
        "from_high", "from_low", "perf_week", "perf_month",
        "trend.state", "trend.label",
        "commentary.headline", "commentary.sentences",
    ],
    "t2_social": [
        "symbol", "n", "empty", "bullish", "bearish", "untagged", "tagged",
        "bull_pct", "blended", "tone", "unique_users", "thin", "partial",
        "velocity_ratio", "velocity_state", "velocity_days", "velocity_needed",
        "top_terms[].term", "top_terms[].count",
        "top[].body", "top[].score",
        "reddit.available",
        "commentary.headline", "commentary.sentences",
    ],
    "t2_news": [
        "symbol", "count", "empty", "tone", "mean", "caveat",
        "sentiment.tone", "sentiment.mean", "sentiment.n",
        "sentiment.pos", "sentiment.neg", "sentiment.neu",
        "sentiment.pos_pct", "sentiment.neg_pct", "sentiment.neu_pct",
        "themes[].term", "themes[].count",
        "salient[].title", "salient[].link", "salient[].source", "salient[].score",
        "source_breakdown[].source", "source_breakdown[].mean",
        "source_breakdown[].n",
        "feeds[].feed", "feeds[].n",
        "commentary.headline", "commentary.sentences",
    ],
    "t2_events": [
        "symbol", "earnings_date", "earnings_when", "earnings_days_out",
        "earnings_soon", "implied_move", "implied_expiry", "covers_earnings",
        "move_ratio", "move_state", "market_cap",
        "historical_moves.n", "historical_moves.mean_abs",
        "historical_moves.moves",
        "filings.available",
        "commentary.headline", "commentary.sentences",
    ],
}

# Tab 3 shares one renderer across all eight panels, so one field list covers
# them all — and a break in any single panel's payload is a break in every
# panel's rendering.
_TAB3_PATHS = [
    "panel", "title", "count", "empty", "window_hours", "newest",
    "tone", "mean", "caveat", "undated_dropped",
    "sentiment.tone", "sentiment.mean", "sentiment.n",
    "sentiment.pos", "sentiment.neg", "sentiment.neu",
    "sentiment.pos_pct", "sentiment.neg_pct", "sentiment.neu_pct",
    # The directional layer. `score` is legitimately None when no headline
    # fired a rule, but the key itself must always be present — the renderer
    # branches on it.
    "asset.score", "asset.label", "asset.noun", "asset.coverage",
    "asset.n_fired", "asset.n", "asset.thin", "asset.two_sided",
    "asset.bull", "asset.bear",
    "themes[].term", "themes[].count",
    "drivers[].driver", "drivers[].count",
    "salient[].title", "salient[].link", "salient[].source",
    "salient[].score", "salient[].asset", "salient[].drivers",
    "source_breakdown[].source", "source_breakdown[].mean",
    "source_breakdown[].n",
    "feeds[].feed", "feeds[].n",
    "quotes[].label", "quotes[].display", "quotes[].delta", "quotes[].dir",
    "items[].title", "items[].link", "items[].source", "items[].published",
    "items[].compound", "items[].asset",
    "commentary.headline", "commentary.sentences", "commentary.warnings",
]

REQUIRED_TAB3 = {
    "t3_us_macro": _TAB3_PATHS,
    "t3_us_equities": _TAB3_PATHS,
    "t3_us_rates": _TAB3_PATHS,
    "t3_eu_macro": _TAB3_PATHS,
    "t3_eu_markets": _TAB3_PATHS,
    "t3_energy": _TAB3_PATHS,
    "t3_precious": _TAB3_PATHS,
    "t3_metals": _TAB3_PATHS,
}

# Paths that are allowed to be absent because the list they live in may be
# legitimately empty on a given day.
OPTIONAL_IF_EMPTY = {"naked_pocs", "lvn_zones", "oi_magnets", "confluence",
                     "destabilisers",
                     # Tab 2: all legitimately empty for some tickers.
                     "divergences", "themes", "salient", "source_breakdown",
                     "feeds", "term", "top_terms", "top", "chart", "moves",
                     # Tab 3: a quiet weekend empties a niche panel, no driver
                     # rule need fire, and the quote strip is optional by
                     # design — none of these is a contract break.
                     "drivers", "quotes", "items"}


def resolve(obj, path: str):
    """Return (ok, note). Missing keys are failures; empty lists are noted."""
    cur = obj
    for part in path.split("."):
        if part.endswith("[]"):
            key = part[:-2]
            if not isinstance(cur, dict) or key not in cur:
                return False, f"missing key {key!r}"
            cur = cur[key]
            if not isinstance(cur, list):
                return False, f"{key!r} is {type(cur).__name__}, expected list"
            if not cur:
                return True, f"{key!r} is empty (cannot verify element fields)"
            cur = cur[0]
        else:
            if not isinstance(cur, dict) or part not in cur:
                return False, f"missing key {part!r}"
            cur = cur[part]
    return True, None


def main() -> int:
    args = sys.argv[1:]
    which = "tab1"
    if "--panels" in args:
        i = args.index("--panels")
        which = args[i + 1] if i + 1 < len(args) else ""
    if which not in ("tab1", "tab2", "tab3", "all"):
        print(f"unknown --panels {which!r}; choose tab1, tab2, tab3 or all")
        return 2

    required = {}
    if which in ("tab1", "all"):
        required.update(REQUIRED)
    if which in ("tab2", "all"):
        required.update(REQUIRED_TAB2)
    if which in ("tab3", "all"):
        required.update(REQUIRED_TAB3)

    with urllib.request.urlopen(URL, timeout=30) as r:
        state = json.load(r)

    failures, notes = [], []
    for key, paths in required.items():
        rec = state.get(key)
        if not rec or not rec.get("payload"):
            failures.append(f"{key}: no payload (status={(rec or {}).get('status')})")
            continue
        payload = rec["payload"]
        for path in paths:
            ok, note = resolve(payload, path)
            if not ok:
                head = path.split(".")[0].replace("[]", "")
                if head in OPTIONAL_IF_EMPTY:
                    notes.append(f"{key}.{path}: {note} (optional)")
                else:
                    failures.append(f"{key}.{path}: {note}")
            elif note:
                notes.append(f"{key}.{path}: {note}")

    for n in notes:
        print(f"  note  {n}")
    if failures:
        print(f"\n{len(failures)} CONTRACT FAILURES:")
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    total = sum(len(v) for v in required.values())
    print(f"\nOK — all {total} frontend field paths present across "
          f"{len(required)} panels ({which}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
