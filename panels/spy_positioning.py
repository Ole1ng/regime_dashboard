"""SPY dealer-positioning panel (panel key ``gamma_spy``).

Ported from market_almanack, where it was the full SPY positioning read. It
replaces the narrow gamma_engine "SPY cross-check" that used to fill this slot:
same panel key, richer payload, so ``regime.confluence`` keeps scoring the SPY
book as an independent lens (it reads ``spot`` / ``call_wall`` / ``put_wall``,
all of which this payload carries).

Snapshot pipeline: fetch CBOE delayed option chain -> filter -> per-contract
GEX/DEX + computed vanna/charm -> aggregate by strike -> derive levels (walls,
zero gamma via spot ladder, OI magnets) -> headline numbers -> deterministic
rule-based commentary. Returns one JSON-serialisable payload dict; persistence
and HTTP are handled by the caller (``app.py`` + ``store``), matching every
other panel.

Sign convention (stated on the panel): dealers long calls, short puts, so a
per-contract Greek exposure G contributes +G*OI for calls and -G*OI for puts.

Chart contract: each ``chart`` bucket carries ``call_gex``, ``put_gex`` and
their sum ``net_gex``. The frontend draws one bar per strike from ``net_gex``
(green positive / red negative), so the sum lives here — computed once, next to
the components it is derived from, and covered by a regression test — rather
than being re-derived in JavaScript.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

from . import _bs

# --------------------------------------------------------------------------- #
# Config constants (calibrated once to SPY scale; revisit occasionally)
# --------------------------------------------------------------------------- #

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/SPY.json"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

R_RATE = 0.04        # short risk-free proxy (SOFR-ish); precision barely matters
DIV_YIELD = 0.012    # SPY dividend yield
EXPIRY_WINDOW_DAYS = 90
DISPLAY_PCT = 0.06   # strike chart window: spot ±6%
BUCKET = 5.0         # $5 strike buckets for display
LADDER_LO, LADDER_HI, LADDER_STEP = 0.92, 1.08, 0.25  # zero-gamma spot ladder

_SYM_RE = re.compile(r"^(SPY)(\d{6})([CP])(\d{8})$")

CONTRACT_MULT = 100

# Commentary thresholds (Section 9.2) — config, not code.
GEX_HEAVY = 750e6
GEX_MODERATE = 250e6
CUSHION_BAND = 0.0075       # 0.75%
WALL_NEAR = 0.005           # within 0.5% of a wall
WALL_FAR = 0.015            # neither wall within 1.5% -> open field
COMPRESS_LOW = 0.025        # walls closer than 2.5% -> compressed
COMPRESS_HIGH = 0.06        # walls wider than 6% -> wide
FLIP_NEAR = 0.003           # within 0.3% of zero gamma -> sitting on trigger
DEX_BAND = 3e9
VANNA_BAND = 400e6
CHARM_BAND = 150e6
ZERO_DTE_SHARE = 0.35
OPEX_NEAR_DAYS = 3
OPEX_FAR_DAYS = 10
THIN_CHAIN = 150            # fewer surviving contracts -> low confidence
STALE_MINUTES = 30


# --------------------------------------------------------------------------- #
# Fetch + parse
# --------------------------------------------------------------------------- #

def fetch_chain(timeout: float = 20.0) -> dict:
    """Pull the full delayed SPY chain. Raises on network/HTTP failure."""
    resp = requests.get(CBOE_URL, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _parse_symbol(sym: str):
    m = _SYM_RE.match(sym or "")
    if not m:
        return None
    expiry = datetime.strptime(m.group(2), "%y%m%d").date()
    return expiry, m.group(3), int(m.group(4)) / 1000.0


def _resolve_spot(data: dict):
    """Return (spot, source, is_fallback). None spot means unusable snapshot."""
    cp = data.get("current_price")
    if cp:
        return float(cp), "current_price", False
    close = data.get("close")
    if close:
        return float(close), "close", True
    bid, ask = data.get("bid"), data.get("ask")
    if bid and ask:
        return (float(bid) + float(ask)) / 2.0, "bid/ask midpoint", True
    if close is not None:
        return float(close), "close", True
    return None, None, True


# --------------------------------------------------------------------------- #
# OPEX calendar helpers
# --------------------------------------------------------------------------- #

def _third_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    # weekday(): Mon=0 .. Fri=4. First Friday, then +14 days.
    first_friday = d + timedelta(days=(4 - d.weekday()) % 7)
    return first_friday + timedelta(days=14)


def _opex_context(today: date):
    """Days to the next monthly OPEX and days since the previous one."""
    this_month = _third_friday(today.year, today.month)
    if this_month >= today:
        nxt = this_month
    else:
        ny, nm = (today.year + (today.month == 12),
                  1 if today.month == 12 else today.month + 1)
        nxt = _third_friday(ny, nm)
    if this_month <= today:
        prev = this_month
    else:
        py, pm = (today.year - (today.month == 1),
                  12 if today.month == 1 else today.month - 1)
        prev = _third_friday(py, pm)
    return nxt, (nxt - today).days, (today - prev).days


# --------------------------------------------------------------------------- #
# Core computation
# --------------------------------------------------------------------------- #

def compute(chain_json: dict, now: datetime | None = None) -> dict:
    """Turn a raw CBOE payload into the panel snapshot dict.

    Raises ``ValueError`` when the snapshot is unusable (no spot, or no
    contracts survive filtering) so the caller can surface an honest failure.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()
    data = chain_json.get("data") or {}

    spot, spot_source, fallback_spot = _resolve_spot(data)
    if spot is None:
        raise ValueError("CBOE snapshot has no usable spot price")

    rows = []
    for opt in data.get("options", []):
        parsed = _parse_symbol(opt.get("option", ""))
        if not parsed:
            continue
        expiry, right, strike = parsed
        dte = (expiry - today).days
        if dte < 0 or dte > EXPIRY_WINDOW_DAYS:
            continue
        oi = opt.get("open_interest") or 0
        if oi <= 0:
            continue
        gamma = opt.get("gamma") or 0.0
        delta = opt.get("delta") or 0.0
        iv = opt.get("iv") or 0.0
        if (gamma == 0.0) and (iv == 0.0):
            continue
        if iv <= 0.01 or iv >= 5.0:
            continue
        rows.append({
            "strike": strike, "right": right, "is_call": right == "C",
            "dte": dte, "T": max(dte, 0) / 365.0,
            "oi": int(oi), "gamma": float(gamma), "delta": float(delta),
            "iv": float(iv),
        })

    if not rows:
        raise ValueError("no contracts survived filtering")

    df = pd.DataFrame(rows)
    sign = np.where(df["is_call"], 1.0, -1.0)  # dealer convention

    # --- GEX / DEX from quoted Greeks ------------------------------------- #
    df["gex"] = sign * df["gamma"] * df["oi"] * CONTRACT_MULT * spot * spot * 0.01
    df["dex"] = sign * df["delta"] * df["oi"] * CONTRACT_MULT * spot

    # --- vanna / charm from Black-Scholes (computed, not quoted) ----------- #
    vanna = _bs.bs_vanna(spot, df["strike"].values, df["iv"].values,
                         df["T"].values, R_RATE, DIV_YIELD)
    charm = _bs.bs_charm(spot, df["strike"].values, df["iv"].values,
                         df["T"].values, R_RATE, DIV_YIELD,
                         df["is_call"].values)
    df["vanna_$"] = sign * vanna * df["oi"] * CONTRACT_MULT * spot * 0.01
    df["charm_$"] = sign * charm * df["oi"] * CONTRACT_MULT * spot / 365.0

    net_gex = float(df["gex"].sum())
    dex = float(df["dex"].sum())
    vanna_pressure = float(df["vanna_$"].sum())
    charm_drift = float(df["charm_$"].sum())

    # --- per-strike call/put GEX (for chart + walls) ---------------------- #
    grp = df.groupby("strike")
    call_gex = grp.apply(lambda g: g.loc[g["is_call"], "gex"].sum())
    put_gex = grp.apply(lambda g: g.loc[~g["is_call"], "gex"].sum())
    strike_gex = pd.DataFrame({"call_gex": call_gex, "put_gex": put_gex}).fillna(0.0)

    # --- walls (exact strikes within display window) ---------------------- #
    lo, hi = spot * (1 - DISPLAY_PCT), spot * (1 + DISPLAY_PCT)
    win = strike_gex[(strike_gex.index >= lo) & (strike_gex.index <= hi)]
    call_wall = put_wall = None
    if not win.empty and win["call_gex"].max() > 0:
        call_wall = float(win["call_gex"].idxmax())
    if not win.empty and win["put_gex"].min() < 0:
        put_wall = float(win["put_gex"].idxmin())  # most negative

    # --- OI magnets: top 3 strikes by total OI within ~30 days ------------ #
    near = df[df["dte"] <= 30]
    magnets = []
    if not near.empty:
        oi_by_strike = near.groupby("strike")["oi"].sum().sort_values(ascending=False)
        magnets = [{"strike": float(k), "oi": int(v)}
                   for k, v in oi_by_strike.head(3).items()]

    # --- bucketed strike chart (±6%, $5 buckets) -------------------------- #
    chart = []
    if not win.empty:
        buckets = (np.round(win.index / BUCKET) * BUCKET)
        bdf = win.copy()
        bdf["bucket"] = buckets
        agg = bdf.groupby("bucket").agg(call_gex=("call_gex", "sum"),
                                        put_gex=("put_gex", "sum"))
        chart = [{"strike": float(k), "call_gex": float(r.call_gex),
                  "put_gex": float(r.put_gex),
                  "net_gex": float(r.call_gex) + float(r.put_gex)}
                 for k, r in agg.iterrows()]
        chart.sort(key=lambda x: x["strike"])

    # --- zero gamma via spot ladder --------------------------------------- #
    zero_gamma, no_flip, regime = _zero_gamma(df, sign, spot)
    cushion = (spot - zero_gamma) if zero_gamma is not None else None
    cushion_pct = (cushion / spot) if cushion is not None else None

    # --- 0DTE gamma share (engine-only, not displayed) -------------------- #
    abs_gex = df["gex"].abs()
    total_abs = float(abs_gex.sum()) or 1.0
    zero_dte_share = float(abs_gex[df["dte"] == 0].sum()) / total_abs

    next_opex, days_to_opex, days_since_opex = _opex_context(today)
    nearest_magnet = (min((m["strike"] for m in magnets),
                          key=lambda k: abs(k - spot)) if magnets else None)
    call_wall_is_magnet = bool(
        call_wall is not None and magnets
        and any(abs(m["strike"] - call_wall) < BUCKET / 2 for m in magnets))

    # --- snapshot timestamp + quality flags ------------------------------- #
    snap_ts, stale = _snapshot_age(chain_json, now)

    metrics = {
        "spot": round(spot, 2),
        "spot_source": spot_source,
        "regime": regime,
        "zero_gamma": round(zero_gamma, 2) if zero_gamma is not None else None,
        "no_flip": no_flip,
        "cushion": cushion,
        "cushion_pct": cushion_pct,
        "net_gex": net_gex,
        "dex": dex,
        "vanna_pressure": vanna_pressure,
        "charm_drift": charm_drift,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "call_wall_is_magnet": call_wall_is_magnet,
        "oi_magnets": magnets,
        "nearest_magnet": nearest_magnet,
        "days_to_opex": days_to_opex,
        "days_since_opex": days_since_opex,
        "next_opex": next_opex.isoformat(),
        "zero_dte_gamma_share": zero_dte_share,
        "n_contracts": int(len(df)),
        # quality flags consumed by the engine
        "stale": stale,
        "fallback_spot": fallback_spot,
        "thin_chain": len(df) < THIN_CHAIN,
    }

    commentary = generate_commentary(metrics)

    metrics["chart"] = chart
    metrics["bucket"] = BUCKET          # frontend matches magnets to bars with it
    metrics["expiry_window_days"] = EXPIRY_WINDOW_DAYS
    metrics["snapshot_ts"] = snap_ts
    metrics["commentary"] = commentary
    return metrics


def _zero_gamma(df: pd.DataFrame, sign: np.ndarray, spot: float):
    """Spot-ladder zero gamma. Returns (level|None, no_flip, regime)."""
    K = df["strike"].values
    iv = df["iv"].values
    T = df["T"].values
    oi = df["oi"].values
    grid = np.arange(LADDER_LO * spot, LADDER_HI * spot + LADDER_STEP, LADDER_STEP)
    totals = np.empty_like(grid)
    for i, s_prime in enumerate(grid):
        g = _bs.bs_gamma(s_prime, K, iv, T, R_RATE, DIV_YIELD)
        gex = sign * g * oi * CONTRACT_MULT * s_prime * s_prime * 0.01
        totals[i] = gex.sum()

    if np.all(totals > 0):
        return None, True, "positive"
    if np.all(totals < 0):
        return None, True, "negative"

    # find sign change bracketing and linearly interpolate
    zero = None
    for i in range(len(grid) - 1):
        a, b = totals[i], totals[i + 1]
        if a == 0:
            zero = grid[i]
            break
        if a * b < 0:
            zero = grid[i] + (grid[i + 1] - grid[i]) * (-a) / (b - a)
            break
    if zero is None:
        # no crossing despite mixed signs (shouldn't happen) — treat as pinned
        return None, True, "positive" if totals[-1] > 0 else "negative"
    regime = "positive" if spot >= zero else "negative"
    return float(zero), False, regime


def _snapshot_age(chain_json: dict, now: datetime):
    """Best-effort parse of the CBOE timestamp; returns (iso_or_raw, is_stale)."""
    raw = (chain_json.get("timestamp")
           or (chain_json.get("data") or {}).get("last_trade_time"))
    if not raw:
        return now.isoformat(timespec="seconds"), False
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            ts = datetime.strptime(str(raw), fmt)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_min = (now - ts).total_seconds() / 60.0
            return str(raw), age_min > STALE_MINUTES
        except ValueError:
            continue
    return str(raw), False


# --------------------------------------------------------------------------- #
# Formatting helpers (used by the commentary engine)
# --------------------------------------------------------------------------- #

def fmt_usd(x: float) -> str:
    """$mm with no decimals below $1bn, $bn with one decimal above."""
    a = abs(x)
    sign = "-" if x < 0 else ""
    if a >= 1e9:
        return f"{sign}${a / 1e9:.1f}bn"
    return f"{sign}${a / 1e6:.0f}mm"


def _px(x) -> str:
    return f"{x:.2f}" if x is not None else "n/a"


def _pct(frac) -> str:
    return f"{abs(frac) * 100:.2f}%" if frac is not None else "n/a"


# --------------------------------------------------------------------------- #
# Rule-based commentary engine (Section 9)
# --------------------------------------------------------------------------- #
#
# Each rule: (name, group, priority, condition(m)->bool, render(m)->str).
# Composition: keep highest-priority firing rule per group (walls keep 2);
# apply suppression edges; order regime->levels->flows->timing->synthesis;
# cap at 6 sentences. Quality warnings are prepended, never capped.

GROUP_ORDER = {
    "regime": 0,
    "flip_watch": 1, "walls": 1, "range": 1, "gex_size": 1,
    "dex": 2, "vanna": 2,
    "charm": 3, "opex": 3, "dte": 3,
    "synthesis": 4,
}
GROUP_MAX = {"walls": 2}  # default 1
MAX_SENTENCES = 6


class _Rule:
    __slots__ = ("name", "group", "priority", "cond", "render")

    def __init__(self, name, group, priority, cond, render):
        self.name = name
        self.group = group
        self.priority = priority
        self.cond = cond
        self.render = render


def _build_rules() -> list[_Rule]:
    R = []

    # --- regime + cushion (group regime, exclusive, priority 1) ----------- #
    R.append(_Rule(
        "regime_no_flip", "regime", 1,
        lambda m: m["no_flip"],
        lambda m: (f"No gamma flip within ±8% of spot; the {m['regime']} gamma "
                   f"regime is pinned across the visible range at {_px(m['spot'])}.")))
    R.append(_Rule(
        "regime_pos_firm", "regime", 1,
        lambda m: (not m["no_flip"] and m["regime"] == "positive"
                   and m["cushion_pct"] is not None and m["cushion_pct"] > CUSHION_BAND),
        lambda m: (f"Positive gamma with a {_pct(m['cushion_pct'])} cushion above the "
                   f"{_px(m['zero_gamma'])} flip; dealer hedging dampens moves, favouring "
                   f"mean reversion with extremes faded.")))
    R.append(_Rule(
        "regime_pos_fragile", "regime", 1,
        lambda m: (not m["no_flip"] and m["regime"] == "positive"
                   and m["cushion_pct"] is not None and m["cushion_pct"] <= CUSHION_BAND),
        lambda m: (f"Positive but fragile gamma — only {_pct(m['cushion_pct'])} above the "
                   f"{_px(m['zero_gamma'])} flip, so a modest dip flips the regime; "
                   f"{_px(m['zero_gamma'])} is the line in the sand.")))
    R.append(_Rule(
        "regime_neg_marginal", "regime", 1,
        lambda m: (not m["no_flip"] and m["regime"] == "negative"
                   and m["cushion_pct"] is not None and abs(m["cushion_pct"]) <= CUSHION_BAND),
        lambda m: (f"Marginally negative gamma, {_pct(m['cushion_pct'])} below the "
                   f"{_px(m['zero_gamma'])} flip; hedging now amplifies moves and "
                   f"reclaiming {_px(m['zero_gamma'])} is the bull trigger.")))
    R.append(_Rule(
        "regime_neg_deep", "regime", 1,
        lambda m: (not m["no_flip"] and m["regime"] == "negative"
                   and m["cushion_pct"] is not None and abs(m["cushion_pct"]) > CUSHION_BAND),
        lambda m: (f"Deeply negative gamma, {_pct(m['cushion_pct'])} below the "
                   f"{_px(m['zero_gamma'])} flip; trend and vol-expansion conditions hold "
                   f"and momentum should not be faded.")))

    # --- net GEX magnitude (group gex_size, priority 3) ------------------- #
    R.append(_Rule(
        "gex_heavy", "gex_size", 3,
        lambda m: abs(m["net_gex"]) > GEX_HEAVY,
        lambda m: (f"Net GEX of {fmt_usd(m['net_gex'])} per 1% is heavy, so realised "
                   f"volatility should stay compressed and intraday ranges contained.")))
    R.append(_Rule(
        "gex_moderate", "gex_size", 3,
        lambda m: GEX_MODERATE <= abs(m["net_gex"]) <= GEX_HEAVY,
        lambda m: (f"Net GEX of {fmt_usd(m['net_gex'])} per 1% is moderate — levels are "
                   f"meaningful but breakable on a catalyst.")))
    R.append(_Rule(
        "gex_light", "gex_size", 3,
        lambda m: abs(m["net_gex"]) < GEX_MODERATE,
        lambda m: (f"Net GEX of just {fmt_usd(m['net_gex'])} per 1% is light; levels carry "
                   f"little force and the rest of this read should be discounted.")))

    # --- wall proximity (group walls, up to 2, priority 2) ---------------- #
    def _call_near(m):
        return (m["call_wall"] is not None
                and abs(m["spot"] - m["call_wall"]) / m["spot"] < WALL_NEAR)

    def _put_near(m):
        return (m["put_wall"] is not None
                and abs(m["spot"] - m["put_wall"]) / m["spot"] < WALL_NEAR)

    R.append(_Rule(
        "wall_call_near", "walls", 2, _call_near,
        lambda m: (
            (f"Spot is pinned just under the {_px(m['call_wall'])} call wall, which "
             f"coincides with a high-OI magnet — a pin candidate rather than mere resistance.")
            if m["call_wall_is_magnet"] else
            (f"Rallies are likely to stall into the {_px(m['call_wall'])} call wall, "
             f"{_pct((m['call_wall'] - m['spot']) / m['spot'])} above spot."))))
    R.append(_Rule(
        "wall_put_near", "walls", 2, _put_near,
        lambda m: (
            (f"With gamma positive, the {_px(m['put_wall'])} put wall "
             f"{_pct((m['spot'] - m['put_wall']) / m['spot'])} below should act as support.")
            if m["regime"] == "positive" else
            (f"With gamma negative, the {_px(m['put_wall'])} put wall is an acceleration "
             f"point if breached, not support."))))
    R.append(_Rule(
        "wall_open_field", "walls", 2,
        lambda m: (not _call_near(m) and not _put_near(m)
                   and m["call_wall"] is not None and m["put_wall"] is not None),
        lambda m: (f"Spot sits mid-range between the {_px(m['put_wall'])} put wall and "
                   f"{_px(m['call_wall'])} call wall, which frame the expected range.")))

    # --- wall compression (group range, priority 4) ----------------------- #
    def _width(m):
        if m["call_wall"] is None or m["put_wall"] is None:
            return None
        return (m["call_wall"] - m["put_wall"]) / m["spot"]

    R.append(_Rule(
        "range_compressed", "range", 4,
        lambda m: (_width(m) is not None and _width(m) < COMPRESS_LOW),
        lambda m: (f"Walls are compressed within {_pct(_width(m))} "
                   f"({_px(m['put_wall'])}–{_px(m['call_wall'])}); range-bound pinning is "
                   f"likely and a break of either wall is the signal.")))
    R.append(_Rule(
        "range_wide", "range", 4,
        lambda m: (_width(m) is not None and _width(m) > COMPRESS_HIGH),
        lambda m: (f"Walls are wide ({_px(m['put_wall'])}–{_px(m['call_wall'])}, "
                   f"{_pct(_width(m))}); levels are weak guides today.")))

    # --- zero gamma proximity (group flip_watch, priority 2) -------------- #
    R.append(_Rule(
        "flip_watch", "flip_watch", 2,
        lambda m: (not m["no_flip"] and m["zero_gamma"] is not None
                   and abs(m["spot"] - m["zero_gamma"]) / m["spot"] < FLIP_NEAR),
        lambda m: (f"Spot is sitting on the {_px(m['zero_gamma'])} vol trigger "
                   f"({_pct((m['spot'] - m['zero_gamma']) / m['spot'])} away); intraday "
                   f"regime flips are likely.")))

    # --- DEX (group dex; interaction pri 2 suppresses standalone pri 4) --- #
    R.append(_Rule(
        "dex_interaction", "dex", 2,
        lambda m: m["regime"] == "negative" and m["dex"] < -DEX_BAND,
        lambda m: (f"With negative gamma and dealers short {fmt_usd(m['dex'])} of delta, both "
                   f"hedging engines point the same way — expect outsized two-way moves.")))
    R.append(_Rule(
        "dex_standalone", "dex", 4,
        lambda m: abs(m["dex"]) > DEX_BAND,
        lambda m: (f"Dealer delta of {fmt_usd(m['dex'])} (net short) is squeeze fuel on rallies."
                   if m["dex"] < 0 else
                   f"Dealer delta of {fmt_usd(m['dex'])} (net long) means rallies face passive supply.")))

    # --- vanna (group vanna, priority 5; silent inside band) -------------- #
    R.append(_Rule(
        "vanna_pos", "vanna", 5,
        lambda m: m["vanna_pressure"] > VANNA_BAND,
        lambda m: (f"Vanna of {fmt_usd(m['vanna_pressure'])} per vol point means a vol crush "
                   f"would fuel dealer buying — a supportive grind if IV bleeds.")))
    R.append(_Rule(
        "vanna_neg", "vanna", 5,
        lambda m: m["vanna_pressure"] < -VANNA_BAND,
        lambda m: (f"Vanna of {fmt_usd(m['vanna_pressure'])} per vol point means a vol spike "
                   f"would force dealer selling — fragile to bad news.")))

    # --- charm (group charm, priority 5, modulated by OPEX timing) -------- #
    def _charm_fires(m):
        if m["days_to_opex"] > OPEX_FAR_DAYS and abs(m["charm_drift"]) < CHARM_BAND:
            return False  # muted to silence
        return abs(m["charm_drift"]) >= CHARM_BAND or m["days_to_opex"] <= OPEX_NEAR_DAYS

    def _charm_render(m):
        direction = "buying" if m["charm_drift"] > 0 else "selling"
        base = (f"Charm drift of {fmt_usd(m['charm_drift'])}/day points to dealer "
                f"{direction} into the close")
        if m["days_to_opex"] <= OPEX_NEAR_DAYS and m["nearest_magnet"] is not None:
            return (base + f"; pin pressure toward {_px(m['nearest_magnet'])} intensifies "
                    f"into Friday's expiry.")
        return base + "."

    R.append(_Rule("charm", "charm", 5, _charm_fires, _charm_render))

    # --- OPEX cycle timing (group opex, priority 4) ----------------------- #
    R.append(_Rule(
        "opex_week", "opex", 4,
        lambda m: m["days_to_opex"] <= OPEX_NEAR_DAYS,
        lambda m: (f"A large share of visible gamma rolls off at Friday's monthly OPEX "
                   f"({m['next_opex']}); expect the map to reset Monday.")))
    R.append(_Rule(
        "opex_unwind", "opex", 4,
        lambda m: m["days_to_opex"] > OPEX_NEAR_DAYS and m["days_since_opex"] in (1, 2),
        lambda m: (f"Fresh positioning {m['days_since_opex']} session(s) after the monthly "
                   f"OPEX leaves walls less established; trust today's levels less.")))

    # --- 0DTE concentration (group dte, priority 5) ----------------------- #
    R.append(_Rule(
        "dte_dominated", "dte", 5,
        lambda m: m["zero_dte_gamma_share"] > ZERO_DTE_SHARE,
        lambda m: (f"Today's expiry accounts for {m['zero_dte_gamma_share'] * 100:.0f}% of "
                   f"visible gamma; these are intraday-only levels that dissolve at the close.")))

    # --- synthesis (group synthesis, priority 3, max 1) ------------------- #
    R.append(_Rule(
        "synth_supportive", "synthesis", 3,
        lambda m: (m["regime"] == "positive" and m["vanna_pressure"] > 0
                   and m["charm_drift"] > 0 and m["call_wall"] is not None),
        lambda m: (f"Passive flows are uniformly supportive; the path of least resistance is "
                   f"a slow grind toward the {_px(m['call_wall'])} call wall.")))
    R.append(_Rule(
        "synth_adverse", "synthesis", 3,
        lambda m: (m["regime"] == "negative" and m["vanna_pressure"] < 0
                   and m["charm_drift"] < 0 and m["put_wall"] is not None),
        lambda m: (f"Passive flows are uniformly adverse; the drift of least resistance is "
                   f"toward the {_px(m['put_wall'])} put wall.")))
    R.append(_Rule(
        "synth_conflict", "synthesis", 3,
        lambda m: ((m["regime"] == "positive" and m["vanna_pressure"] < -VANNA_BAND)
                   or (m["regime"] == "negative" and m["vanna_pressure"] > VANNA_BAND)),
        lambda m: (f"{m['regime'].capitalize()} gamma but opposing vanna of "
                   f"{fmt_usd(m['vanna_pressure'])} — a calm tape grinds, yet a vol spike "
                   f"flips the flows, and the vanna side dominates on any vol event.")))

    return R


_RULES = _build_rules()


def _quality_warnings(m: dict) -> list[str]:
    out = []
    if m.get("stale"):
        out.append("⚠ Snapshot is over 30 minutes old; positioning may have shifted.")
    if m.get("fallback_spot"):
        out.append(f"⚠ Using fallback spot ({_px(m['spot'])}) — CBOE current_price was unavailable.")
    if m.get("thin_chain"):
        out.append(f"⚠ Unusually thin chain after filtering ({m['n_contracts']} contracts); "
                   f"figures are low-confidence.")
    return out


# Headline feature precedence (spec 9.3): the distinctive features that carry a
# dedicated headline, most important first. Filler rules that fire on almost
# every snapshot (gex heavy/moderate, open-field walls, range, charm, opex) are
# deliberately excluded so they never crowd out the headline — they remain as
# body sentences.
HEADLINE_PRECEDENCE = [
    "flip_watch", "dex_interaction", "wall_call_near", "wall_put_near",
    "synth_conflict", "gex_light", "dte_dominated",
]


def _headline(m: dict, survivor_names: set[str]) -> str:
    regime_word = "Positive gamma" if m["regime"] == "positive" else "Negative gamma"
    name = next((n for n in HEADLINE_PRECEDENCE if n in survivor_names), None)

    if name == "flip_watch":
        hl = f"Sitting on the vol trigger at {_px(m['zero_gamma'])} — regime in play."
    elif name == "gex_light":
        hl = "Light positioning — levels carry little weight today."
    elif name == "dex_interaction":
        hl = "Negative gamma with dealers short delta — two-way volatility risk elevated."
    elif name == "synth_conflict":
        hl = f"{regime_word} but adverse vanna — fragile to a vol spike."
    elif name == "dte_dominated":
        hl = f"{regime_word} on a 0DTE-dominated tape — intraday levels only."
    elif name == "wall_call_near":
        hl = (f"Positive gamma, pinned under the {_px(m['call_wall'])} call wall — range day favoured."
              if m["regime"] == "positive" else
              f"Negative gamma under the {_px(m['call_wall'])} call wall — rallies fragile.")
    elif name == "wall_put_near":
        hl = (f"Positive gamma holding the {_px(m['put_wall'])} put wall — dip support intact."
              if m["regime"] == "positive" else
              f"Negative gamma below the {_px(m['put_wall'])} put wall — downside acceleration risk.")
    elif m["regime"] == "positive":
        hl = "Positive gamma — dealer hedging dampens the tape."
    else:
        hl = "Negative gamma — moves amplified, trend conditions."

    # 0DTE conditions the wording from "levels" to "intraday levels"
    if (m.get("zero_dte_gamma_share", 0) > ZERO_DTE_SHARE
            and "levels" in hl and "intraday" not in hl):
        hl = hl.replace("levels", "intraday levels")
    return hl


def generate_commentary(metrics: dict) -> dict:
    """Deterministic headline + warnings + 3-6 sentences. Pure function."""
    # 1. evaluate
    fired = [r for r in _RULES if r.cond(metrics)]

    # 2. reduce within groups (keep highest priority; walls keep up to 2)
    by_group: dict[str, list[_Rule]] = {}
    for r in fired:
        by_group.setdefault(r.group, []).append(r)
    survivors: list[_Rule] = []
    for group, rules in by_group.items():
        rules.sort(key=lambda r: (r.priority, r.name))
        survivors.extend(rules[: GROUP_MAX.get(group, 1)])

    names = {r.name for r in survivors}

    # 3. suppression edges
    if "flip_watch" in names:                       # flip_watch suppresses gex_size
        survivors = [r for r in survivors if r.group != "gex_size"]
    if any(r.group == "synthesis" for r in survivors):  # synthesis suppresses raw flows
        survivors = [r for r in survivors if r.group not in ("dex", "vanna")]

    # 4. headline keys off the distinctive features that survived (full set,
    #    before the sentence cap, so a dropped body sentence still informs it)
    survivor_names = {r.name for r in survivors}

    # 5. cap to 6, dropping lowest priority first (always keep regime)
    survivors.sort(key=lambda r: (GROUP_ORDER[r.group], r.priority, r.name))
    if len(survivors) > MAX_SENTENCES:
        keep = {r.name for r in survivors if r.group == "regime"}
        ranked = sorted(survivors, key=lambda r: (r.priority, GROUP_ORDER[r.group], r.name))
        for r in ranked:
            if len(keep) >= MAX_SENTENCES:
                break
            keep.add(r.name)
        survivors = [r for r in survivors if r.name in keep][:MAX_SENTENCES]

    sentences = [r.render(metrics) for r in survivors]
    return {
        "headline": _headline(metrics, survivor_names),
        "warnings": _quality_warnings(metrics),
        "sentences": sentences,
    }


# --------------------------------------------------------------------------- #
# Panel entry point (called by app.py)
# --------------------------------------------------------------------------- #

def refresh() -> dict:
    """Fetch + compute the full snapshot. Raises on any failure."""
    chain = fetch_chain()
    return compute(chain)
