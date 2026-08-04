"""Verify the live payloads carry every field static/app.js reads.

The frontend and the panels are only coupled by the shape of these dicts, so a
renamed key fails silently as a blank card. This walks the required paths for
each renderer against a running server.

    python app.py                 # in another shell
    python tools/check_contract.py

Exits non-zero if anything is missing.
"""

from __future__ import annotations

import json
import sys
import urllib.request

URL = "http://127.0.0.1:8010/api/state"

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
    "gamma_spy": [
        "spot", "regime", "zero_gamma", "cushion_pct", "net_gex", "charm_drift",
        "call_wall", "put_wall", "snapshot_ts", "n_contracts",
        "oi_magnets[].strike",
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
        "naked_pocs", "lvn_zones",
        "chart[].price", "chart[].price_spx", "chart[].volume", "chart[].share",
        "commentary.headline", "commentary.sentences",
    ],
}

# Paths that are allowed to be absent because the list they live in may be
# legitimately empty on a given day.
OPTIONAL_IF_EMPTY = {"naked_pocs", "lvn_zones", "oi_magnets", "confluence",
                     "destabilisers"}


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
    with urllib.request.urlopen(URL, timeout=30) as r:
        state = json.load(r)

    failures, notes = [], []
    for key, paths in REQUIRED.items():
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
    total = sum(len(v) for v in REQUIRED.values())
    print(f"\nOK — all {total} frontend field paths present across "
          f"{len(REQUIRED)} panels.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
