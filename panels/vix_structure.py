"""VIX term structure — RESEARCH.md §3.

Answers "first month versus spot": is the curve in contango (the normal state,
which *causes* the long-gamma regime by making vol supply profitable) or in
backwardation (protection demand outrunning supply, dealer gamma flipping
short)?

Every direct CBOE VX futures endpoint returns 403 (see DATA_SOURCES.md), so the
futures curve is recovered from the VIX **option** chain instead. VIX options
are priced off VIX futures rather than spot VIX, so put-call parity returns the
forward for each listed expiry:

    F = K + e^(rT) * (C - P)      at the strike minimising |C - P|

That is CBOE's own method for extracting the forward in the VIX white paper, and
it agrees with the CFE settlement file to within ~0.1 vol point (verified
2026-08-04). The settlement CSV is fetched as a cross-check but is end-of-day,
so it never overrides the live parity curve.

RESEARCH.md warns that reading the raw front contract produces a sawtooth in the
slope at every roll, so the headline number is the **constant-maturity 30-day**
interpolation, not VX1 itself.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timezone

import numpy as np
import requests

from . import calendar_context as cal

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

QUOTE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/{sym}.json"
VIX_CHAIN_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/_VIX.json"
SETTLEMENT_URL = "https://www.cboe.com/us/futures/market_statistics/settlement/csv/"

# The whole constant-maturity index family is on the CBOE CDN (verified
# 2026-08-04), so this panel needs no third-party quote source at all.
INDEX_SYMBOLS = {
    "vix1d": ("_VIX1D", 1),
    "vix9d": ("_VIX9D", 9),
    "vix": ("_VIX", 30),
    "vix3m": ("_VIX3M", 93),
    "vix6m": ("_VIX6M", 186),
}
VVIX_SYMBOL = "_VVIX"

R_RATE = 0.04
_SYM_RE = re.compile(r"^(VIX|VIXW)(\d{6})([CP])(\d{8})$")

# Ratio thresholds (RESEARCH.md §3).
BACKWARDATION_RATIO = 1.0      # VIX/VIX3M above 1 = backwardation warning
FLAT_BAND = 0.25               # |spot - VX1| below this is "flat", in vol points
EVENT_RATIO = 1.0              # VIX9D/VIX above 1 = near-term event premium
VVIX_ELEVATED = 100.0
VVIX_CALM = 85.0


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #

def _get_json(url: str, timeout: float = 25.0) -> dict:
    r = requests.get(url, headers=_HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_index_quotes() -> dict:
    """Spot VIX and the constant-maturity index family, from CBOE."""
    out = {}
    for key, (sym, tenor) in INDEX_SYMBOLS.items():
        d = (_get_json(QUOTE_URL.format(sym=sym)).get("data") or {})
        price = d.get("current_price") or d.get("close")
        if price is None:
            raise ValueError(f"no price for {sym}")
        out[key] = {"symbol": d.get("symbol", sym), "value": float(price),
                    "prev_close": d.get("prev_day_close"), "tenor_days": tenor}
    d = (_get_json(QUOTE_URL.format(sym=VVIX_SYMBOL)).get("data") or {})
    out["vvix"] = {"symbol": d.get("symbol", VVIX_SYMBOL),
                   "value": float(d.get("current_price") or d.get("close") or 0.0),
                   "prev_close": d.get("prev_day_close"), "tenor_days": None}
    return out


def fetch_vix_chain(timeout: float = 40.0) -> dict:
    return _get_json(VIX_CHAIN_URL, timeout=timeout)


def fetch_settlements(timeout: float = 25.0) -> dict[str, float]:
    """VX settlement prices by expiry date. Cross-check only; never fatal.

    This is an end-of-day file: intraday it may carry the prior session, and
    contracts that did not trade repeat a reference price (six consecutive
    weeklies shared one value during the 2026-08-04 probe). Treat it as
    corroboration for the parity curve, not as a source.
    """
    r = requests.get(SETTLEMENT_URL, headers=_HEADERS, timeout=timeout)
    r.raise_for_status()
    out: dict[str, float] = {}
    for row in csv.DictReader(io.StringIO(r.text)):
        if (row.get("Product") or "").strip().upper() != "VX":
            continue
        try:
            out[(row.get("Expiration Date") or "").strip()] = float(row["Price"])
        except (TypeError, ValueError, KeyError):
            continue
    return out


# --------------------------------------------------------------------------- #
# Forward curve via put-call parity
# --------------------------------------------------------------------------- #

def forwards_from_chain(chain: dict, today: date) -> list[dict]:
    """Recover the VX forward for every expiry with two-sided quotes."""
    opts = (chain.get("data") or {}).get("options") or []
    # expiry -> strike -> {"C": mid, "P": mid}
    book: dict[str, dict[float, dict[str, float]]] = {}
    roots: dict[str, str] = {}
    for o in opts:
        m = _SYM_RE.match(o.get("option", "") or "")
        if not m:
            continue
        root, exp, right, strike = m.group(1), m.group(2), m.group(3), \
            int(m.group(4)) / 1000.0
        bid, ask = o.get("bid") or 0.0, o.get("ask") or 0.0
        if bid <= 0 or ask <= 0:
            continue
        book.setdefault(exp, {}).setdefault(strike, {})[right] = (bid + ask) / 2.0
        # Monthly (root VIX) wins if an expiry somehow carries both roots.
        roots[exp] = "VIX" if roots.get(exp) == "VIX" or root == "VIX" else root

    out = []
    for exp, strikes in sorted(book.items()):
        pairs = [(k, v["C"], v["P"]) for k, v in strikes.items()
                 if "C" in v and "P" in v]
        if not pairs:
            continue
        expiry = datetime.strptime(exp, "%y%m%d").date()
        days = (expiry - today).days
        if days < 0:
            continue
        T = max(days, 0) / 365.0
        disc = float(np.exp(R_RATE * T))
        pairs.sort(key=lambda t: abs(t[1] - t[2]))
        k, c, p = pairs[0]
        forward = k + disc * (c - p)
        # Dispersion across the three most ATM strikes = a quality signal.
        top = pairs[:3]
        fwds = [kk + disc * (cc - pp) for kk, cc, pp in top]
        out.append({
            "expiry": expiry.isoformat(),
            "days": days,
            "forward": round(forward, 4),
            "root": roots.get(exp, "VIXW"),
            "is_monthly": roots.get(exp) == "VIX",
            "atm_strike": k,
            "call": round(c, 4), "put": round(p, 4),
            "n_pairs": len(pairs),
            "spread": round(max(fwds) - min(fwds), 4) if len(fwds) > 1 else 0.0,
        })
    out.sort(key=lambda x: x["days"])
    return out


def interpolate(curve: list[dict], target_days: float) -> float | None:
    """Linear interpolation in days across a [{days, forward}] curve."""
    pts = sorted(((c["days"], c["forward"]) for c in curve), key=lambda t: t[0])
    if not pts:
        return None
    if len(pts) == 1:
        return pts[0][1]
    if target_days <= pts[0][0]:
        return pts[0][1]
    if target_days >= pts[-1][0]:
        return pts[-1][1]
    for (d0, f0), (d1, f1) in zip(pts, pts[1:]):
        if d0 <= target_days <= d1:
            if d1 == d0:
                return f0
            w = (target_days - d0) / (d1 - d0)
            return f0 + w * (f1 - f0)
    return None


# --------------------------------------------------------------------------- #
# Compute
# --------------------------------------------------------------------------- #

def compute(index_quotes: dict, chain: dict, settlements: dict | None,
            today: date | None = None) -> dict:
    today = today or datetime.now(timezone.utc).date()

    spot_vix = index_quotes["vix"]["value"]
    curve = forwards_from_chain(chain, today)
    if not curve:
        raise ValueError("no VIX forwards could be recovered from the option chain")

    monthlies = [c for c in curve if c["is_monthly"]]
    vx1 = monthlies[0] if monthlies else None
    vx2 = monthlies[1] if len(monthlies) > 1 else None

    # Canonical constant maturity uses the two front monthlies; the full curve
    # (weeklies included) gives a finer read, so both are reported.
    cm30_monthly = interpolate(monthlies, 30.0) if monthlies else None
    cm30_all = interpolate(curve, 30.0)

    basis = (spot_vix - vx1["forward"]) if vx1 else None
    if basis is None:
        structure = "unknown"
    elif basis < -FLAT_BAND:
        structure = "contango"
    elif basis > FLAT_BAND:
        structure = "backwardation"
    else:
        structure = "flat"

    vix = spot_vix
    vix3m = index_quotes["vix3m"]["value"]
    vix9d = index_quotes["vix9d"]["value"]
    vix_vix3m = vix / vix3m if vix3m else None
    vix9d_vix = vix9d / vix if vix else None
    vvix = index_quotes["vvix"]["value"]

    # Cross-check the parity curve against the settlement file where both exist.
    # Weeklies are shown but excluded from the agreement test: ones that did not
    # trade all repeat a single reference price (2026-08-04 had six consecutive
    # weeklies at 17.9466 while the live curve ran 16.20 -> 18.37), which would
    # otherwise raise a false disagreement every session.
    cross = []
    if settlements:
        for c in curve:
            s = settlements.get(c["expiry"])
            if s is not None:
                cross.append({"expiry": c["expiry"], "parity": c["forward"],
                              "settlement": round(s, 4),
                              "diff": round(c["forward"] - s, 4),
                              "is_monthly": c["is_monthly"]})
    max_diff = max((abs(x["diff"]) for x in cross if x["is_monthly"]), default=None)

    # Confirm the monthly expiries match the calendar rule; a mismatch means one
    # of the two is wrong and the panel should say so rather than quietly pick.
    expected = cal.next_vix_expiry(today).isoformat()
    expiry_rule_ok = bool(vx1 and vx1["expiry"] == expected)

    index_curve = [
        {"label": k.upper(), "days": v["tenor_days"], "value": v["value"]}
        for k, v in index_quotes.items() if v["tenor_days"] is not None
    ]
    index_curve.sort(key=lambda x: x["days"])

    payload = {
        "spot_vix": round(spot_vix, 2),
        "vx1": vx1,
        "vx2": vx2,
        "basis": round(basis, 4) if basis is not None else None,
        "basis_pct": round(basis / spot_vix, 4) if basis is not None else None,
        "structure": structure,
        "cm30": round(cm30_monthly, 4) if cm30_monthly is not None else None,
        "cm30_all_expiries": round(cm30_all, 4) if cm30_all is not None else None,
        "roll_vx1_vx2": (round(vx2["forward"] - vx1["forward"], 4)
                         if vx1 and vx2 else None),
        "vix1d": index_quotes["vix1d"]["value"],
        "vix9d": vix9d,
        "vix3m": vix3m,
        "vix6m": index_quotes["vix6m"]["value"],
        "vvix": vvix,
        "vix_vix3m": round(vix_vix3m, 4) if vix_vix3m else None,
        "vix9d_vix": round(vix9d_vix, 4) if vix9d_vix else None,
        "index_curve": index_curve,
        "futures_curve": curve,
        "n_monthlies": len(monthlies),
        "days_to_vix_expiry": (date.fromisoformat(vx1["expiry"]) - today).days
                              if vx1 else None,
        "expected_vix_expiry": expected,
        "expiry_rule_ok": expiry_rule_ok,
        "cross_check": cross,
        "cross_check_max_diff": max_diff,          # monthlies only, see above
        "cross_check_note": ("Agreement measured on monthly contracts only; "
                             "untraded weeklies repeat a reference settlement."),
        "settlement_available": bool(settlements),
        "snapshot_ts": chain.get("timestamp"),
    }
    payload["flags"] = _flags(payload)
    payload["commentary"] = _commentary(payload)
    return payload


def _flags(p: dict) -> dict:
    return {
        "backwardation": p["structure"] == "backwardation",
        "vix3m_inverted": bool(p["vix_vix3m"] and p["vix_vix3m"] > BACKWARDATION_RATIO),
        "near_term_event": bool(p["vix9d_vix"] and p["vix9d_vix"] > EVENT_RATIO),
        "vvix_elevated": p["vvix"] > VVIX_ELEVATED,
        "vvix_calm": p["vvix"] < VVIX_CALM,
        "curve_disagrees_with_settlement": bool(
            p["cross_check_max_diff"] is not None and p["cross_check_max_diff"] > 1.0),
        "expiry_rule_mismatch": not p["expiry_rule_ok"],
    }


def _commentary(p: dict) -> dict:
    f = p["flags"]
    sentences: list[str] = []
    warnings: list[str] = []

    if p["structure"] == "contango":
        headline = (f"Contango — VX1 {p['vx1']['forward']:.2f} over spot "
                    f"{p['spot_vix']:.2f}")
        sentences.append(
            f"The curve is in contango with spot VIX {abs(p['basis']):.2f} points below "
            f"the {p['vx1']['expiry']} front future; short-vol carry is positive, which "
            f"is what keeps dealers accumulating long gamma.")
    elif p["structure"] == "backwardation":
        headline = (f"Backwardation — spot {p['spot_vix']:.2f} over VX1 "
                    f"{p['vx1']['forward']:.2f}")
        sentences.append(
            f"The curve is backwardated with spot VIX {p['basis']:.2f} points above the "
            f"{p['vx1']['expiry']} front future; demand for immediate protection is "
            f"outrunning supply and vol sellers are being forced out.")
    elif p["structure"] == "flat":
        headline = f"Flat curve — spot {p['spot_vix']:.2f} versus VX1 " \
                   f"{p['vx1']['forward']:.2f}"
        sentences.append(
            "Spot and the front future are effectively level; the carry that normally "
            "underwrites vol supply has gone, which is how backwardation starts.")
    else:
        headline = f"Spot VIX {p['spot_vix']:.2f} — no futures curve recovered"

    if p["cm30"] is not None:
        sentences.append(
            f"Constant-maturity 30-day sits at {p['cm30']:.2f} against spot "
            f"{p['spot_vix']:.2f}; the constant-maturity read is the one to track, since "
            f"the raw front contract sawtooths at every roll.")

    if f["vix3m_inverted"]:
        sentences.append(
            f"VIX/VIX3M at {p['vix_vix3m']:.3f} is above 1 — the term structure is "
            f"inverted on the index family too, corroborating stress rather than noise.")
    else:
        sentences.append(
            f"VIX/VIX3M at {p['vix_vix3m']:.3f} is comfortably below 1, so the longer "
            f"index tenors still price calm.")

    if f["near_term_event"]:
        sentences.append(
            f"VIX9D above VIX ({p['vix9d']:.2f} vs {p['spot_vix']:.2f}) prices a "
            f"near-dated event inside the next fortnight.")

    if f["vvix_elevated"]:
        sentences.append(
            f"VVIX at {p['vvix']:.1f} shows active demand for convexity — the tail is "
            f"being bid even if spot vol looks contained.")
    elif f["vvix_calm"]:
        sentences.append(
            f"VVIX at {p['vvix']:.1f} is subdued; there is little bid for tail "
            f"protection, which is itself a fragility condition.")

    if p["days_to_vix_expiry"] is not None:
        sentences.append(
            f"VIX expiry is in {p['days_to_vix_expiry']} days ({p['vx1']['expiry']}), "
            f"a separate event from SPX OPEX.")

    if f["expiry_rule_mismatch"]:
        warnings.append(
            f"⚠ Front monthly from the chain ({p['vx1']['expiry'] if p['vx1'] else 'none'}) "
            f"does not match the calendar rule ({p['expected_vix_expiry']}).")
    if f["curve_disagrees_with_settlement"]:
        warnings.append(
            f"⚠ Parity curve differs from CFE monthly settlement by up to "
            f"{p['cross_check_max_diff']:.2f} vol points; settlement is end-of-day, "
            f"but check before trusting the level.")
    if not p["settlement_available"]:
        warnings.append("⚠ CFE settlement file unavailable — parity curve is unverified.")

    return {"headline": headline, "warnings": warnings, "sentences": sentences}


def refresh() -> dict:
    """Fetch + compute. The settlement cross-check is best-effort."""
    try:
        settlements = fetch_settlements()
    except Exception:
        settlements = {}
    return compute(fetch_index_quotes(), fetch_vix_chain(), settlements)
