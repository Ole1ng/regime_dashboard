"""Options-derived sentiment for one ticker: skew, ratios, term structure, max pain.

Runs on the CBOE chain the positioning panel already fetched, so it costs no
extra request. It does **not** reuse that panel's DataFrame: ``ticker_positioning``
filters to ``EXPIRY_WINDOW_DAYS`` (90) because dealer exposure beyond a quarter
is noise, but the term-structure curve needs the back months and the
post-earnings expiry can sit outside that window. This module therefore parses
the raw payload itself with no DTE cap.

What each metric is actually telling you:

  * **25-delta skew** — the IV of a 25-delta put minus a 25-delta call. Puts
    almost always trade richer (crash risk is real and one-sided), so the level
    matters less than the *degree*: normalising by ATM IV makes it comparable
    across names and vol regimes. This is the cleanest fear gauge available for
    free.
  * **Put/call ratios** — OI is the standing book, volume is today's flow. They
    disagree often and usefully: a call-heavy book with put-heavy flow is
    positioning being unwound.
  * **Term structure** — front IV above back IV (inversion) means the market is
    pricing a near-dated event or is in stress. Contango is the resting state.
  * **IV vs realised** — implied above realised is a fear/event premium; implied
    *below* realised means options are cheap relative to how the stock has
    actually been moving. Both happen; the sign is not assumed.
  * **Max pain** — the strike where the most option value expires worthless.
    Treated here as a weak gravitational note, not a prediction.

Sign convention for the panel: metrics are reported raw, and the composite panel
decides direction. Nothing here is scored.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import date, datetime, timezone

import numpy as np

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

_SYM_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")

TARGET_DELTA = 0.25          # the wing we quote skew at
SKEW_MIN_DTE = 20            # front-week IV is noisy; quote skew further out
SKEW_MAX_DTE = 60
TERM_MAX_DTE = 400
MAX_PAIN_MAX_DTE = 45        # max pain only means anything near-dated
MIN_IV = 0.01                # CBOE writes iv: 0 for untraded contracts
MAX_IV = 5.0
MIN_WING_POINTS = 3          # need a few contracts to interpolate a wing

# Skew as a fraction of ATM IV.
SKEW_PUT_BID = 0.06
SKEW_CALL_BID = -0.02

# Put/call ratio bands (OI).
PCR_PUT_HEAVY = 1.20
PCR_CALL_HEAVY = 0.70

# Term-structure slope (back ATM IV - front ATM IV), in vol points.
TERM_FLAT = 0.02

# IV - RV, in vol points.
IVRV_RICH = 0.10
IVRV_CHEAP = -0.10

TRADING_DAYS = 252
MIN_HISTORY_DAYS = 60        # before IV rank is worth showing


def refresh(symbol: str, chain_json: dict, prices=None,
            history: list[dict] | None = None) -> dict:
    """Panel entry point. ``chain_json`` is shared with the positioning panel."""
    return compute(chain_json, symbol, prices=prices, history=history)


# --------------------------------------------------------------------------- #
# Compute
# --------------------------------------------------------------------------- #

def compute(chain_json: dict, symbol: str, prices=None,
            history: list[dict] | None = None,
            now: datetime | None = None) -> dict:
    """Pure: raw CBOE payload (+ optional price history) -> panel payload."""
    now = now or datetime.now(timezone.utc)
    today = now.date()

    data = (chain_json or {}).get("data") or {}
    spot = _spot(data)
    contracts = _contracts(data, symbol, today)

    iv30 = data.get("iv30")
    # CBOE quotes iv30 in PERCENT (52.421) while per-contract iv is a decimal
    # (0.524). Normalising here is the difference between a 52-vol and a
    # 5200-vol reading downstream.
    iv30 = float(iv30) / 100.0 if iv30 else None

    payload = {
        "symbol": symbol,
        "spot": spot,
        "snapshot_ts": data.get("last_trade_time"),
        "security_type": data.get("security_type"),
        "n_contracts": len(contracts),
        "iv30": iv30,
        "iv30_change": (float(data["iv30_change"]) / 100.0
                        if data.get("iv30_change") else None),
    }

    payload.update(_atm(contracts, spot))
    payload.update(_skew(contracts, spot, payload.get("atm_iv")))
    payload.update(_ratios(contracts))
    payload.update(_term(contracts, spot))
    payload.update(_realised(prices, iv30))
    payload.update(_max_pain(contracts, spot))
    payload.update(_iv_rank(history, iv30))

    payload["thin_chain"] = len(contracts) < 20
    payload["commentary"] = _commentary(payload)
    return payload


def _spot(data: dict) -> float | None:
    for key in ("current_price", "close"):
        value = data.get(key)
        if value:
            return float(value)
    bid, ask = data.get("bid"), data.get("ask")
    if bid and ask:
        return (float(bid) + float(ask)) / 2.0
    return None


def _contracts(data: dict, symbol: str, today: date) -> list[dict]:
    """Flatten the payload's OCC symbols into rows. No DTE cap — see docstring."""
    out = []
    for opt in data.get("options") or []:
        match = _SYM_RE.match(opt.get("option") or "")
        # Reject adjacent roots: a CBOE payload for a short symbol can contain
        # contracts belonging to a different, longer-rooted name.
        if not match or match.group(1) != symbol:
            continue
        try:
            expiry = datetime.strptime(match.group(2), "%y%m%d").date()
        except ValueError:
            continue
        dte = (expiry - today).days
        if dte < 0 or dte > TERM_MAX_DTE:
            continue

        iv = float(opt.get("iv") or 0.0)
        out.append({
            "expiry": expiry,
            "dte": dte,
            "is_call": match.group(3) == "C",
            "strike": int(match.group(4)) / 1000.0,
            "iv": iv,
            "delta": float(opt.get("delta") or 0.0),
            "oi": float(opt.get("open_interest") or 0.0),
            "volume": float(opt.get("volume") or 0.0),
            "bid": float(opt.get("bid") or 0.0),
            "ask": float(opt.get("ask") or 0.0),
            "usable_iv": MIN_IV < iv < MAX_IV,
        })
    return out


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #

def _atm(contracts: list[dict], spot: float | None) -> dict:
    """ATM implied vol: mean of the nearest-strike call and put in the front month."""
    if not spot:
        return {"atm_iv": None, "atm_expiry": None, "atm_dte": None}

    usable = [c for c in contracts if c["usable_iv"] and c["dte"] >= SKEW_MIN_DTE]
    if not usable:
        usable = [c for c in contracts if c["usable_iv"]]
    if not usable:
        return {"atm_iv": None, "atm_expiry": None, "atm_dte": None}

    expiry = min(c["expiry"] for c in usable)
    front = [c for c in usable if c["expiry"] == expiry]
    nearest = min(abs(c["strike"] - spot) for c in front)
    at_strike = [c for c in front if abs(c["strike"] - spot) <= nearest + 1e-9]

    return {
        "atm_iv": round(float(np.mean([c["iv"] for c in at_strike])), 4),
        "atm_expiry": expiry.isoformat(),
        "atm_dte": front[0]["dte"],
    }


def _wing_iv(rows: list[dict], target: float) -> float | None:
    """IV interpolated at |delta| = target along one right's wing.

    Deltas are not monotonic in strike once the chain gets sparse, so this sorts
    by |delta| and interpolates rather than assuming an ordering.
    """
    points = sorted(((abs(r["delta"]), r["iv"]) for r in rows
                     if r["usable_iv"] and 0.02 < abs(r["delta"]) < 0.98),
                    key=lambda p: p[0])
    if len(points) < MIN_WING_POINTS:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    # np.interp clamps outside the range, which is the behaviour we want: a
    # chain that never reaches 25 delta returns its furthest wing instead of
    # extrapolating a fantasy.
    return float(np.interp(target, xs, ys))


def _skew(contracts: list[dict], spot: float | None,
          atm_iv: float | None) -> dict:
    """25-delta put IV minus 25-delta call IV, in the first expiry >= SKEW_MIN_DTE."""
    candidates = sorted({c["expiry"] for c in contracts
                         if SKEW_MIN_DTE <= c["dte"] <= SKEW_MAX_DTE})
    if not candidates:
        candidates = sorted({c["expiry"] for c in contracts
                             if c["dte"] >= SKEW_MIN_DTE})
    if not candidates:
        return {"skew_25d": None, "skew_25d_pct": None, "skew_state": "unknown",
                "skew_expiry": None, "skew_put_iv": None, "skew_call_iv": None}

    expiry = candidates[0]
    rows = [c for c in contracts if c["expiry"] == expiry]
    put_iv = _wing_iv([r for r in rows if not r["is_call"]], TARGET_DELTA)
    call_iv = _wing_iv([r for r in rows if r["is_call"]], TARGET_DELTA)

    if put_iv is None or call_iv is None:
        return {"skew_25d": None, "skew_25d_pct": None, "skew_state": "unknown",
                "skew_expiry": expiry.isoformat(),
                "skew_put_iv": put_iv, "skew_call_iv": call_iv}

    skew = put_iv - call_iv
    # Normalised by ATM so a 3-vol skew on a 20-vol name and on an 80-vol name
    # are not treated as the same thing.
    skew_pct = (skew / atm_iv) if atm_iv else None

    if skew_pct is None:
        state = "unknown"
    elif skew_pct >= SKEW_PUT_BID:
        state = "put-bid"
    elif skew_pct <= SKEW_CALL_BID:
        state = "call-bid"
    else:
        state = "balanced"

    return {"skew_25d": round(skew, 4),
            "skew_25d_pct": round(skew_pct, 4) if skew_pct is not None else None,
            "skew_state": state, "skew_expiry": expiry.isoformat(),
            "skew_put_iv": round(put_iv, 4), "skew_call_iv": round(call_iv, 4)}


def _ratios(contracts: list[dict]) -> dict:
    """Put/call on open interest (the book) and on volume (today's flow)."""
    call_oi = sum(c["oi"] for c in contracts if c["is_call"])
    put_oi = sum(c["oi"] for c in contracts if not c["is_call"])
    call_vol = sum(c["volume"] for c in contracts if c["is_call"])
    put_vol = sum(c["volume"] for c in contracts if not c["is_call"])

    pcr_oi = (put_oi / call_oi) if call_oi > 0 else None
    pcr_vol = (put_vol / call_vol) if call_vol > 0 else None

    return {
        "call_oi": call_oi, "put_oi": put_oi,
        "call_volume": call_vol, "put_volume": put_vol,
        "pcr_oi": round(pcr_oi, 3) if pcr_oi is not None else None,
        "pcr_vol": round(pcr_vol, 3) if pcr_vol is not None else None,
        "pcr_oi_state": _pcr_state(pcr_oi),
        "pcr_vol_state": _pcr_state(pcr_vol),
    }


def _pcr_state(ratio: float | None) -> str:
    if ratio is None:
        return "unknown"
    if ratio >= PCR_PUT_HEAVY:
        return "put-heavy"
    if ratio <= PCR_CALL_HEAVY:
        return "call-heavy"
    return "balanced"


def _term(contracts: list[dict], spot: float | None) -> dict:
    """ATM IV per expiry, and the front-to-back slope."""
    if not spot:
        return {"term": [], "term_slope": None, "term_state": "unknown"}

    by_expiry: dict[date, list[dict]] = defaultdict(list)
    for c in contracts:
        if c["usable_iv"]:
            by_expiry[c["expiry"]].append(c)

    curve = []
    for expiry in sorted(by_expiry):
        rows = by_expiry[expiry]
        nearest = min(abs(r["strike"] - spot) for r in rows)
        at_strike = [r for r in rows if abs(r["strike"] - spot) <= nearest + 1e-9]
        curve.append({
            "expiry": expiry.isoformat(),
            "dte": rows[0]["dte"],
            "atm_iv": round(float(np.mean([r["iv"] for r in at_strike])), 4),
            "n": len(rows),
        })

    if len(curve) < 2:
        return {"term": curve, "term_slope": None, "term_state": "unknown"}

    front = curve[0]
    # Compare against ~60 DTE where available, else the furthest point.
    back = next((p for p in curve if p["dte"] >= 55), curve[-1])
    slope = back["atm_iv"] - front["atm_iv"]

    if slope > TERM_FLAT:
        state = "contango"
    elif slope < -TERM_FLAT:
        state = "inverted"
    else:
        state = "flat"

    return {"term": curve, "term_slope": round(slope, 4), "term_state": state,
            "term_front_dte": front["dte"], "term_back_dte": back["dte"]}


def _realised(prices, iv30: float | None) -> dict:
    """Annualised realised vol from daily closes, and the IV-RV spread."""
    out = {"rv20": None, "rv60": None, "ivrv_spread": None,
           "ivrv_state": "unknown", "rv_pctl_1y": None}
    if prices is None or len(prices) < 25:
        return out

    try:
        closes = prices["Close"].dropna()
        returns = np.log(closes / closes.shift(1)).dropna()
    except Exception:
        return out
    if len(returns) < 25:
        return out

    def annualised(window: int):
        if len(returns) < window:
            return None
        return float(returns.tail(window).std() * math.sqrt(TRADING_DAYS))

    rv20, rv60 = annualised(20), annualised(60)
    out["rv20"] = round(rv20, 4) if rv20 else None
    out["rv60"] = round(rv60, 4) if rv60 else None

    # Where does today's realised vol sit against its own past year? Unlike IV
    # rank this needs no stored history — the price series carries it.
    if len(returns) >= 80:
        rolling = returns.rolling(20).std().dropna() * math.sqrt(TRADING_DAYS)
        if len(rolling) > 10 and rv20:
            out["rv_pctl_1y"] = round(
                100.0 * float((rolling <= rv20).sum()) / len(rolling), 1)

    if iv30 is not None and rv20:
        spread = iv30 - rv20
        out["ivrv_spread"] = round(spread, 4)
        if spread >= IVRV_RICH:
            out["ivrv_state"] = "rich"
        elif spread <= IVRV_CHEAP:
            out["ivrv_state"] = "cheap"
        else:
            out["ivrv_state"] = "fair"

    return out


def _max_pain(contracts: list[dict], spot: float | None) -> dict:
    """Strike at which the most open-interest value expires worthless.

    Computed on the near-dated expiry carrying the most open interest, which is
    the only one where the effect is ever argued to bite.
    """
    near = [c for c in contracts if c["dte"] <= MAX_PAIN_MAX_DTE and c["oi"] > 0]
    if not near or not spot:
        return {"max_pain": None, "max_pain_dist_pct": None, "max_pain_expiry": None}

    oi_by_expiry: dict[date, float] = defaultdict(float)
    for c in near:
        oi_by_expiry[c["expiry"]] += c["oi"]
    expiry = max(oi_by_expiry, key=lambda e: oi_by_expiry[e])

    rows = [c for c in near if c["expiry"] == expiry]
    strikes = sorted({c["strike"] for c in rows})
    if len(strikes) < 3:
        return {"max_pain": None, "max_pain_dist_pct": None, "max_pain_expiry": None}

    def pain(settle: float) -> float:
        # Value left in the money to the holders — dealers' loss — at settle.
        return sum(c["oi"] * (max(0.0, settle - c["strike"]) if c["is_call"]
                              else max(0.0, c["strike"] - settle))
                   for c in rows)

    best = min(strikes, key=pain)
    return {
        "max_pain": best,
        "max_pain_dist_pct": round(best / spot - 1.0, 4),
        "max_pain_expiry": expiry.isoformat(),
        "max_pain_oi": oi_by_expiry[expiry],
    }


def _iv_rank(history: list[dict] | None, iv30: float | None) -> dict:
    """IV rank/percentile against this ticker's own stored history.

    Deliberately returns None until MIN_HISTORY_DAYS of snapshots exist. A
    percentile over four observations is worse than no number at all, so the
    renderer shows the progress instead.
    """
    rows = [r for r in (history or []) if r.get("iv30") is not None]
    n = len(rows)
    out = {"iv_rank": None, "iv_pctl": None, "history_days": n,
           "history_needed": MIN_HISTORY_DAYS}

    if iv30 is None or n < MIN_HISTORY_DAYS:
        return out

    values = [float(r["iv30"]) for r in rows]
    # Today's reading is included in the range. IV rank is "where does current
    # IV sit between its low and its high", and when today IS the new high or
    # low, excluding it puts the result outside 0-100 (a fresh high returned
    # 152.5 before this). Including it makes a new extreme read exactly
    # 100 or 0, which is what the measure is supposed to mean.
    lo, hi = min(values + [iv30]), max(values + [iv30])
    if hi > lo:
        out["iv_rank"] = round(100.0 * (iv30 - lo) / (hi - lo), 1)
    out["iv_pctl"] = round(100.0 * sum(1 for v in values if v <= iv30) / n, 1)
    return out


# --------------------------------------------------------------------------- #
# Commentary
# --------------------------------------------------------------------------- #

def _commentary(p: dict) -> dict:
    sentences: list[str] = []
    warnings: list[str] = []
    sym = p["symbol"]

    # --- skew --------------------------------------------------------------- #
    if p["skew_25d"] is not None:
        pct = p["skew_25d_pct"]
        pts = p["skew_25d"] * 100
        if p["skew_state"] == "put-bid":
            sentences.append(
                f"25-delta puts trade {pts:+.1f} vol points over calls "
                f"({pct:.0%} of ATM) — the wing bid is meaningful, and downside "
                f"protection is being paid for rather than sold.")
        elif p["skew_state"] == "call-bid":
            sentences.append(
                f"Calls trade over puts by {abs(pts):.1f} vol points — an "
                f"inverted skew. That is unusual and shows up in takeover "
                f"situations and squeezes, where the fat tail is to the upside.")
        else:
            sentences.append(
                f"25-delta skew is {pts:+.1f} vol points ({pct:.0%} of ATM) — "
                f"a normal, unremarkable put bid.")

    # --- ratios ------------------------------------------------------------- #
    oi, vol = p["pcr_oi"], p["pcr_vol"]
    if oi is not None and vol is not None:
        if p["pcr_vol_state"] == "call-heavy" and p["pcr_oi_state"] != "call-heavy":
            sentences.append(
                f"Today's flow is far more call-heavy than the standing book "
                f"(P/C volume {vol:.2f} against {oi:.2f} on open interest) — "
                f"fresh upside speculation rather than an established position.")
        elif p["pcr_vol_state"] == "put-heavy" and p["pcr_oi_state"] != "put-heavy":
            sentences.append(
                f"Flow has turned defensive: P/C volume {vol:.2f} against "
                f"{oi:.2f} on open interest.")
        elif p["pcr_oi_state"] == p["pcr_vol_state"] != "balanced":
            side = "call" if p["pcr_oi_state"] == "call-heavy" else "put"
            sentences.append(
                f"Both the standing book and today's flow are {side}-heavy "
                f"(P/C {oi:.2f} on open interest, {vol:.2f} on volume) — "
                f"one-sided positioning with no sign of a hedge underneath it.")
        else:
            sentences.append(
                f"Put/call sits at {oi:.2f} on open interest and {vol:.2f} on "
                f"today's volume — the book and the flow agree.")

    # --- term structure ----------------------------------------------------- #
    if p["term_slope"] is not None:
        pts = p["term_slope"] * 100
        if p["term_state"] == "inverted":
            sentences.append(
                f"The term structure is inverted by {abs(pts):.1f} vol points — "
                f"the front is bid over the back, which means a near-dated event "
                f"or genuine stress is being priced.")
        elif p["term_state"] == "contango":
            sentences.append(
                f"Term structure is in normal contango ({pts:+.1f} vol points "
                f"front to back); nothing near-dated is being singled out.")

    # --- IV vs realised ------------------------------------------------------ #
    if p["ivrv_spread"] is not None:
        iv, rv = p["iv30"] * 100, p["rv20"] * 100
        pts = p["ivrv_spread"] * 100
        if p["ivrv_state"] == "rich":
            sentences.append(
                f"30-day implied ({iv:.0f}%) sits {pts:.0f} points above "
                f"20-day realised ({rv:.0f}%) — options carry a fear or event "
                f"premium and are expensive to own.")
        elif p["ivrv_state"] == "cheap":
            sentences.append(
                f"30-day implied ({iv:.0f}%) is {abs(pts):.0f} points *below* "
                f"20-day realised ({rv:.0f}%) — the option market is charging "
                f"less than the stock has actually been moving.")
        else:
            sentences.append(
                f"Implied ({iv:.0f}%) and realised ({rv:.0f}%) are broadly in "
                f"line; volatility is fairly priced.")

    # --- max pain ------------------------------------------------------------ #
    if p["max_pain"] is not None:
        dist = p["max_pain_dist_pct"]
        sentences.append(
            f"Max pain for {p['max_pain_expiry']} sits at {p['max_pain']:g}, "
            f"{abs(dist):.1%} {'below' if dist < 0 else 'above'} spot — a weak "
            f"pull at best, and worth noting only into expiry.")

    # --- quality ------------------------------------------------------------- #
    if p["thin_chain"]:
        warnings.append(f"⚠ Only {p['n_contracts']} usable contracts for {sym} — "
                        f"every reading here is fragile.")
    if p["skew_25d"] is None:
        warnings.append("⚠ The chain does not reach 25 delta on both wings; "
                        "skew is unavailable.")
    if p["iv_rank"] is None and p["iv30"] is not None:
        warnings.append(f"⚠ IV rank needs {p['history_needed']} daily snapshots; "
                        f"{p['history_days']} stored so far.")

    return {"headline": _headline(p), "warnings": warnings, "sentences": sentences}


def _headline(p: dict) -> str:
    bits = []
    if p["iv30"] is not None:
        bits.append(f"IV30 {p['iv30']:.0%}")
    if p["ivrv_state"] not in ("unknown", None):
        bits.append(f"{p['ivrv_state']} vs realised")
    if p["skew_state"] not in ("unknown", None):
        bits.append(f"{p['skew_state']} skew")
    if p["term_state"] not in ("unknown", None):
        bits.append(p["term_state"])
    return " · ".join(bits) if bits else "No usable options data"
