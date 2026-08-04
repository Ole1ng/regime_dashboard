"""Dealer-positioning engine for SPX and SPY — RESEARCH.md §1, §2, §6.

Snapshot pipeline: fetch the CBOE delayed option chain -> filter -> per-contract
GEX/DEX plus computed vanna/charm -> aggregate by strike -> derive levels (walls,
zero gamma via a spot ladder, OI magnets) -> headline numbers -> charm-decay
projection -> expiry-bucketed gamma -> deterministic rule-based commentary.
Returns one JSON-serialisable payload dict; persistence and HTTP are handled by
``app.py`` + ``store``.

Ported from market_almanack's SPY panel and generalised to run on either
underlying. The maths, filters and sign convention carried over unchanged
(they were verified before the port); what is new here is the SPX
configuration, the charm-decay projection (§2) and the expiry buckets (§6).

Sign convention (stated on the panel): dealers long calls, short puts, so a
per-contract Greek exposure G contributes +G*OI for calls and -G*OI for puts.
This is an *assumption*, not a measurement — RESEARCH.md §1 flags it as the
single largest error source in the whole framework.

BOOK DELTA vs HEDGING FLOW — the distinction that decides the sign of every
directional number here, so it is spelled out once:

    ``sign * greek * OI * mult * S`` is the *dealer's option-book delta* (or its
    derivative). A delta-neutral dealer holds stock ``H = -D_book``. When the
    book's delta moves by ``dD``, the dealer must trade ``-dD`` of stock to stay
    hedged. **The market-facing flow is therefore the negative of the book-delta
    change.**

    Worked example (the canonical charm tailwind, RESEARCH.md §2): dealers are
    short an OTM put, so their book delta is positive (+0.19) and they hedge by
    shorting stock. As time passes the put's delta rises toward zero, so the
    book's delta *falls* (-0.87/yr). Less short stock is needed, so the dealer
    **buys back** — a supportive drift. Book delta fell; the flow was positive.

    Accordingly ``charm_drift`` and the projection's ``charm_per_day`` /
    ``cum_hedge_flow`` are published as **flows** (positive = dealers must buy),
    with the raw derivative kept alongside as ``charm_book_delta_per_day`` for
    transparency. ``vanna_pressure`` needs no flip because it is quoted per a
    *falling* vol point, which already contains the negation: positive vanna
    pressure means a vol crush forces dealer buying.

    (market_almanack's SPY panel publishes the raw book-delta derivative but
    labels it as flow, which inverts its charm commentary. Corrected here.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import requests

from . import _bs
from . import calendar_context as cal

# --------------------------------------------------------------------------- #
# Shared config
# --------------------------------------------------------------------------- #

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

R_RATE = 0.04        # short risk-free proxy (SOFR-ish); precision barely matters
DIV_YIELD = 0.012    # S&P 500 dividend yield — applies to both SPX and SPY
EXPIRY_WINDOW_DAYS = 90
DISPLAY_PCT = 0.06   # strike chart window: spot +/-6%
LADDER_LO, LADDER_HI = 0.92, 1.08      # zero-gamma spot ladder bounds
CONTRACT_MULT = 100
STALE_MINUTES = 30
PROJECTION_DAYS = 10                   # charm decay horizon (trading days)

# Commentary thresholds shared across underlyings (dimensionless).
CUSHION_BAND = 0.0075       # 0.75%
WALL_NEAR = 0.005           # within 0.5% of a wall
COMPRESS_LOW = 0.025        # walls closer than 2.5% -> compressed
COMPRESS_HIGH = 0.06        # walls wider than 6% -> wide
FLIP_NEAR = 0.003           # within 0.3% of zero gamma -> sitting on trigger
ZERO_DTE_SHARE = 0.35
OPEX_NEAR_DAYS = 3
OPEX_FAR_DAYS = 10


@dataclass(frozen=True)
class Underlying:
    """Per-underlying configuration.

    CALIBRATION (2026-08-04, SPX 7710 / SPY 768 — rerun `tools/calibrate.py`):

        measure              SPX observed    SPY observed
        net GEX /1%             $116.3bn         $8.8bn
        DEX (+/-6% window)     $1697.5bn       $117.9bn
        vanna /vol pt            $19.2bn         $3.1bn
        charm flow /day           $2.6bn         $1.4bn

    market_almanack's SPY thresholds proved to be an order of magnitude below
    the live figures, so these are calibrated from the snapshot above rather
    than inherited. Note the SPX/SPY ratio is *not* a uniform 10x: at equal open
    interest the arithmetic ratio is exactly 10 (GEX carries S^2, BS gamma
    carries 1/S — see test_gamma_engine), but the two chains carry different
    open-interest distributions, so realised ratios run ~13x on gamma and ~2x on
    charm. Each threshold is therefore set from its own observation.

    The DEX band is deliberately set well above the observed level: under the
    long-calls assumption dealer delta is large and positive on essentially
    every snapshot, so a tighter band would fire the same sentence every day.
    """
    key: str
    label: str
    url: str
    sym_re: re.Pattern
    bucket: float          # strike bucket for the display chart
    ladder_step: float     # zero-gamma ladder resolution
    thin_chain: int        # fewer surviving contracts -> low confidence
    gex_heavy: float
    gex_moderate: float
    dex_band: float
    vanna_band: float
    charm_band: float
    commentary: bool = True
    early_exercise: bool = False   # SPY only: quarterly ex-div assignment risk
    roots: tuple[str, ...] = field(default=())


SPX = Underlying(
    key="gamma_spx",
    label="SPX",
    url="https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json",
    # Both roots are live: SPX = monthlies/AM-settled, SPXW = weeklies (the
    # majority of contracts). Verified 2026-08-04: 9,626 SPX + 19,786 SPXW.
    sym_re=re.compile(r"^(SPX|SPXW)(\d{6})([CP])(\d{8})$"),
    roots=("SPX", "SPXW"),
    bucket=25.0,
    ladder_step=2.5,
    thin_chain=300,
    gex_heavy=100e9,
    gex_moderate=40e9,
    dex_band=2500e9,
    vanna_band=12e9,
    charm_band=2e9,
    commentary=True,
)

SPY = Underlying(
    key="gamma_spy",
    label="SPY",
    url="https://cdn.cboe.com/api/global/delayed_quotes/options/SPY.json",
    sym_re=re.compile(r"^(SPY)(\d{6})([CP])(\d{8})$"),
    roots=("SPY",),
    bucket=5.0,
    ladder_step=0.25,
    thin_chain=150,
    gex_heavy=7e9,
    gex_moderate=3e9,
    dex_band=200e9,
    vanna_band=2e9,
    charm_band=1e9,
    commentary=False,       # SPX carries the narrative; SPY is the cross-check
    early_exercise=True,
)

UNDERLYINGS = {"SPX": SPX, "SPY": SPY}


# --------------------------------------------------------------------------- #
# Fetch + parse
# --------------------------------------------------------------------------- #

def fetch_chain(cfg: Underlying, timeout: float = 45.0) -> dict:
    """Pull the full delayed chain. Raises on network/HTTP failure."""
    resp = requests.get(cfg.url, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _parse_symbol(cfg: Underlying, sym: str):
    m = cfg.sym_re.match(sym or "")
    if not m:
        return None
    root = m.group(1)
    expiry = datetime.strptime(m.group(2), "%y%m%d").date()
    return root, expiry, m.group(3), int(m.group(4)) / 1000.0


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


def _snapshot_age(chain_json: dict, now: datetime):
    """Best-effort parse of the CBOE timestamp; returns (raw, is_stale)."""
    raw = (chain_json.get("timestamp")
           or (chain_json.get("data") or {}).get("last_trade_time"))
    if not raw:
        return now.isoformat(timespec="seconds"), False
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            ts = datetime.strptime(str(raw), fmt)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return str(raw), (now - ts).total_seconds() / 60.0 > STALE_MINUTES
        except ValueError:
            continue
    return str(raw), False


# --------------------------------------------------------------------------- #
# Core computation
# --------------------------------------------------------------------------- #

def compute(chain_json: dict, cfg: Underlying, now: datetime | None = None) -> dict:
    """Turn a raw CBOE payload into the panel snapshot dict.

    Raises ``ValueError`` when the snapshot is unusable (no spot, or no
    contracts survive filtering) so the caller can surface an honest failure.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()
    data = chain_json.get("data") or {}

    spot, spot_source, fallback_spot = _resolve_spot(data)
    if spot is None:
        raise ValueError(f"{cfg.label} snapshot has no usable spot price")

    rows = []
    root_counts: dict[str, int] = {}
    for opt in data.get("options", []):
        parsed = _parse_symbol(cfg, opt.get("option", ""))
        if not parsed:
            continue
        root, expiry, right, strike = parsed
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
        root_counts[root] = root_counts.get(root, 0) + 1
        rows.append({
            "root": root, "strike": strike, "right": right, "is_call": right == "C",
            "expiry_ord": expiry.toordinal(),
            "dte": dte, "T": max(dte, 0) / 365.0,
            "oi": int(oi), "gamma": float(gamma), "delta": float(delta),
            "iv": float(iv),
        })

    if not rows:
        raise ValueError(f"no {cfg.label} contracts survived filtering")

    df = pd.DataFrame(rows)
    sign = np.where(df["is_call"], 1.0, -1.0)  # dealer convention

    # --- GEX / DEX from quoted Greeks ------------------------------------- #
    df["gex"] = sign * df["gamma"] * df["oi"] * CONTRACT_MULT * spot * spot * 0.01
    df["dex"] = sign * df["delta"] * df["oi"] * CONTRACT_MULT * spot

    # --- vanna from Black-Scholes (computed, not quoted) ------------------- #
    # Charm is deliberately NOT taken as an instantaneous rate here: at T -> 0
    # the 1/(2T*sigma*sqrt(T)) term diverges, so 0DTE contracts (11% of the live
    # SPX book) swamp the sum with an unbounded annualised figure. The headline
    # charm number is instead the one-session repriced flow from
    # _charm_projection, which is bounded and is literally "per day".
    vanna = _bs.bs_vanna(spot, df["strike"].values, df["iv"].values,
                         df["T"].values, R_RATE, DIV_YIELD)
    # Quoted per a *falling* vol point, so this is already a flow: positive
    # means a vol crush forces dealer buying.
    df["vanna_$"] = sign * vanna * df["oi"] * CONTRACT_MULT * spot * 0.01

    net_gex = float(df["gex"].sum())
    dex = float(df["dex"].sum())
    vanna_pressure = float(df["vanna_$"].sum())

    # --- per-strike call/put GEX (for chart + walls) ---------------------- #
    calls = df[df["is_call"]].groupby("strike")["gex"].sum()
    puts = df[~df["is_call"]].groupby("strike")["gex"].sum()
    strike_gex = pd.DataFrame({"call_gex": calls, "put_gex": puts}).fillna(0.0)

    # --- walls (exact strikes within display window) ---------------------- #
    lo, hi = spot * (1 - DISPLAY_PCT), spot * (1 + DISPLAY_PCT)
    win = strike_gex[(strike_gex.index >= lo) & (strike_gex.index <= hi)]
    # Each wall must sit on its own side of spot. Taking a plain argmax over the
    # whole window lets one heavy round-number strike become *both* walls (the
    # live SPX chain does exactly this at 8000), which makes every downstream
    # sentence incoherent — "put wall above spot should act as support", and a
    # wall-to-wall range of 0.00%.
    above = win[win.index >= spot]
    below = win[win.index <= spot]
    call_wall = put_wall = None
    if not above.empty and above["call_gex"].max() > 0:
        call_wall = float(above["call_gex"].idxmax())
    if not below.empty and below["put_gex"].min() < 0:
        put_wall = float(below["put_gex"].idxmin())  # most negative

    # --- OI magnets: top 3 strikes by OI within 30 days AND the window ---- #
    # The window filter matters: without it the live SPY chain returns magnets
    # at 550 and 520 against a 768 spot — deep-ITM legacy open interest that no
    # tape is ever going to be pinned to.
    near = df[(df["dte"] <= 30) & (df["strike"] >= lo) & (df["strike"] <= hi)]
    magnets = []
    if not near.empty:
        oi_by_strike = near.groupby("strike")["oi"].sum().sort_values(ascending=False)
        magnets = [{"strike": float(k), "oi": int(v)}
                   for k, v in oi_by_strike.head(3).items()]

    # Dealer delta restricted to the same window. Total DEX is dominated by
    # deep-ITM open interest under the long-calls assumption (~$2.7tn on SPX),
    # which is arithmetically right but useless as a signal; the near-the-money
    # figure is what the commentary bands are calibrated against.
    in_win = (df["strike"] >= lo) & (df["strike"] <= hi)
    dex_window = float(df.loc[in_win, "dex"].sum())

    # --- bucketed strike chart -------------------------------------------- #
    chart = []
    if not win.empty:
        bdf = win.copy()
        bdf["bucket"] = np.round(win.index / cfg.bucket) * cfg.bucket
        agg = bdf.groupby("bucket").agg(call_gex=("call_gex", "sum"),
                                        put_gex=("put_gex", "sum"))
        chart = [{"strike": float(k), "call_gex": float(r.call_gex),
                  "put_gex": float(r.put_gex)} for k, r in agg.iterrows()]
        chart.sort(key=lambda x: x["strike"])

    # --- zero gamma via spot ladder --------------------------------------- #
    # Round first, then derive the cushion from the rounded level, so the three
    # displayed numbers (spot, flip, cushion) actually reconcile on the panel.
    zero_gamma, no_flip, regime = _zero_gamma(df, sign, spot, cfg)
    if zero_gamma is not None:
        zero_gamma = round(zero_gamma, 2)
    cushion = (spot - zero_gamma) if zero_gamma is not None else None
    cushion_pct = (cushion / spot) if cushion is not None else None

    # --- expiry buckets + 0DTE share (RESEARCH.md §6) --------------------- #
    calendar = cal.compute(today)
    days_to_opex = calendar["days_to_opex"]
    abs_gex = df["gex"].abs()
    total_abs = float(abs_gex.sum()) or 1.0
    zero_dte_share = float(abs_gex[df["dte"] == 0].sum()) / total_abs
    expiry_buckets = _expiry_buckets(df, days_to_opex)

    # --- charm decay projection (RESEARCH.md §2) -------------------------- #
    # The headline drift IS the projection's first session, so the number in the
    # cards and the first bar of the chart can never disagree.
    projection = _charm_projection(df, sign, spot, today, calendar)
    charm_drift = projection["headline_flow"]
    charm_book_delta = -charm_drift

    nearest_magnet = (min((m["strike"] for m in magnets),
                          key=lambda k: abs(k - spot)) if magnets else None)
    call_wall_is_magnet = bool(
        call_wall is not None and magnets
        and any(abs(m["strike"] - call_wall) < cfg.bucket / 2 for m in magnets))

    snap_ts, stale = _snapshot_age(chain_json, now)

    metrics = {
        "underlying": cfg.label,
        "spot": round(spot, 2),
        "spot_source": spot_source,
        "regime": regime,
        "zero_gamma": zero_gamma,
        "no_flip": no_flip,
        "cushion": cushion,
        "cushion_pct": cushion_pct,
        "net_gex": net_gex,
        "dex": dex,
        "dex_window": dex_window,
        "vanna_pressure": vanna_pressure,
        "charm_drift": charm_drift,
        "charm_book_delta_per_day": charm_book_delta,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "call_wall_is_magnet": call_wall_is_magnet,
        "oi_magnets": magnets,
        "nearest_magnet": nearest_magnet,
        "days_to_opex": days_to_opex,
        "days_since_opex": calendar["days_since_opex"],
        "next_opex": calendar["next_opex"],
        "zero_dte_gamma_share": zero_dte_share,
        "n_contracts": int(len(df)),
        "total_oi": int(df["oi"].sum()),
        "root_counts": root_counts,
        # quality flags consumed by the engine
        "stale": stale,
        "fallback_spot": fallback_spot,
        "thin_chain": len(df) < cfg.thin_chain,
        "ex_div_risk": bool(cfg.early_exercise and calendar["spy_ex_div_warning"]),
        "next_ex_div": calendar["next_spy_ex_div"] if cfg.early_exercise else None,
    }

    metrics["commentary"] = (generate_commentary(metrics, cfg)
                             if cfg.commentary else None)
    metrics["chart"] = chart
    metrics["bucket"] = cfg.bucket
    metrics["expiry_buckets"] = expiry_buckets
    metrics["charm_projection"] = projection
    metrics["expiry_window_days"] = EXPIRY_WINDOW_DAYS
    metrics["snapshot_ts"] = snap_ts
    return metrics


def _zero_gamma(df: pd.DataFrame, sign: np.ndarray, spot: float, cfg: Underlying):
    """Spot-ladder zero gamma. Returns (level|None, no_flip, regime).

    Gamma has to be *re-evaluated* at each hypothetical spot rather than held
    fixed — that is the whole point of the ladder, and why _bs.bs_gamma exists
    even though the feed quotes gamma.
    """
    K = df["strike"].values
    iv = df["iv"].values
    T = df["T"].values
    oi = df["oi"].values
    grid = np.arange(LADDER_LO * spot, LADDER_HI * spot + cfg.ladder_step,
                     cfg.ladder_step)
    totals = np.empty_like(grid)
    for i, s_prime in enumerate(grid):
        g = _bs.bs_gamma(s_prime, K, iv, T, R_RATE, DIV_YIELD)
        totals[i] = (sign * g * oi * CONTRACT_MULT * s_prime * s_prime * 0.01).sum()

    if np.all(totals > 0):
        return None, True, "positive"
    if np.all(totals < 0):
        return None, True, "negative"

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
        return None, True, "positive" if totals[-1] > 0 else "negative"
    return float(zero), False, ("positive" if spot >= zero else "negative")


def _expiry_buckets(df: pd.DataFrame, days_to_opex: int) -> list[dict]:
    """Net GEX split by tenor, so the UI can show what dies at the next expiry."""
    dte = df["dte"].values
    specs = [
        ("0DTE", dte == 0),
        ("1 week", (dte >= 1) & (dte <= 7)),
        (f"to OPEX ({days_to_opex}d)", (dte > 7) & (dte <= days_to_opex)),
        ("beyond OPEX", dte > max(7, days_to_opex)),
    ]
    total_abs = float(df["gex"].abs().sum()) or 1.0
    out = []
    for label, mask in specs:
        sub = df[mask]
        out.append({
            "label": label,
            "net_gex": float(sub["gex"].sum()),
            "abs_share": float(sub["gex"].abs().sum()) / total_abs,
            "n_contracts": int(len(sub)),
        })
    return out


def _charm_projection(df: pd.DataFrame, sign: np.ndarray, spot: float,
                      today: date, calendar: dict) -> dict:
    """Expected mechanical dealer flow over the next N trading days.

    Holds spot and implied vol fixed and lets time pass, re-pricing the whole
    book at each future session and dropping contracts as they expire. Two
    series come out, both published as **flows** (see the module docstring on
    book delta vs hedging flow — each is the negative of a book-delta change):

      * ``charm_per_day`` — the rate at which dealers must buy (+) or sell (-)
        on that date to stay hedged.
      * ``cum_hedge_flow`` — cumulative stock dealers must buy (+) or sell (-)
        between today and that date. Both ends are re-priced with Black-Scholes
        so the difference is self-consistent. This is the headline "dealers must
        buy this much over the next fortnight" number.

    The assumption is stated on the panel: it is a *mechanical* projection, not
    a forecast, and it is only valid while spot and vol stay put. Contracts
    expiring inside the window drop out, which is exactly why the drift dies at
    OPEX (RESEARCH.md §6).
    """
    K = df["strike"].values
    iv = df["iv"].values
    is_call = df["is_call"].values
    exp_ord = df["expiry_ord"].values
    notional = df["oi"].values * CONTRACT_MULT * spot

    def book_delta(mask: np.ndarray, ref_ord: int) -> np.ndarray:
        """Per-contract dealer book delta for `mask`, valued at date `ref_ord`."""
        if not mask.any():
            return np.zeros(0)
        T = np.maximum((exp_ord[mask] - ref_ord) / 365.0, 0.0)
        d = _bs.bs_delta(spot, K[mask], iv[mask], T, R_RATE, DIV_YIELD,
                         is_call[mask])
        return sign[mask] * d * notional[mask]

    all_rows = np.ones(len(df), dtype=bool)
    base_delta = float(book_delta(all_rows, today.toordinal()).sum())

    opex_ord = date.fromisoformat(calendar["next_opex"]).toordinal()
    series = []
    cum = 0.0
    a = today.toordinal()
    for i, d in enumerate(cal.trading_days_ahead(today, PROJECTION_DAYS), start=1):
        b = d.toordinal()
        # Smooth decay: only contracts alive at BOTH ends of the step, so an
        # expiry never masquerades as charm. Bounded by construction — delta is
        # in [-1, 1], so no T -> 0 singularity can leak in.
        survives = exp_ord >= b
        smooth = -float((book_delta(survives, b) - book_delta(survives, a)).sum())
        # Delta carried by contracts dying inside this step, reported separately
        # rather than folded into the drift: it is an OPEX-cliff effect (§6),
        # not the charm tailwind (§2).
        expiring = (exp_ord >= a) & (exp_ord < b)
        released = float(book_delta(expiring, a).sum())
        cum += smooth
        series.append({
            "day": i,
            "date": d.isoformat(),
            "charm_per_day": smooth,
            "cum_hedge_flow": cum,
            "expiry_delta_released": released,
            "contracts_alive": int(survives.sum()),
            "is_opex": b == opex_ord,
        })
        a = b

    return {
        "base_delta": base_delta,
        "headline_flow": series[0]["charm_per_day"] if series else 0.0,
        "series": series,
        "horizon_days": PROJECTION_DAYS,
        "assumption": ("Holds spot and implied vol constant. Each bar is the stock "
                       "dealers must trade that session purely from delta decay of "
                       "contracts surviving it; delta released by expiries is "
                       "tracked separately. A mechanical projection, not a forecast."),
    }


# --------------------------------------------------------------------------- #
# Formatting helpers (used by the commentary engine)
# --------------------------------------------------------------------------- #

def fmt_usd(x: float) -> str:
    """$mm below $1bn, $bn above."""
    a = abs(x)
    sign = "-" if x < 0 else ""
    if a >= 1e9:
        return f"{sign}${a / 1e9:.1f}bn"
    return f"{sign}${a / 1e6:.0f}mm"


def _px(x) -> str:
    return f"{x:,.2f}" if x is not None else "n/a"


def _pct(frac) -> str:
    return f"{abs(frac) * 100:.2f}%" if frac is not None else "n/a"


# --------------------------------------------------------------------------- #
# Rule-based commentary engine
# --------------------------------------------------------------------------- #
#
# Ported from market_almanack, thresholds parameterised by underlying.
# Each rule: (name, group, priority, condition(m)->bool, render(m)->str).
# Composition: keep the highest-priority firing rule per group (walls keep 2);
# apply suppression edges; order regime->levels->flows->timing->synthesis;
# cap at 6 sentences. Quality warnings are prepended and never capped.

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


def _build_rules(cfg: Underlying) -> list[_Rule]:
    R = []

    # --- regime + cushion ------------------------------------------------- #
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

    # --- net GEX magnitude ------------------------------------------------ #
    R.append(_Rule(
        "gex_heavy", "gex_size", 3,
        lambda m: abs(m["net_gex"]) > cfg.gex_heavy,
        lambda m: (f"Net GEX of {fmt_usd(m['net_gex'])} per 1% is heavy, so realised "
                   f"volatility should stay compressed and intraday ranges contained.")))
    R.append(_Rule(
        "gex_moderate", "gex_size", 3,
        lambda m: cfg.gex_moderate <= abs(m["net_gex"]) <= cfg.gex_heavy,
        lambda m: (f"Net GEX of {fmt_usd(m['net_gex'])} per 1% is moderate — levels are "
                   f"meaningful but breakable on a catalyst.")))
    R.append(_Rule(
        "gex_light", "gex_size", 3,
        lambda m: abs(m["net_gex"]) < cfg.gex_moderate,
        lambda m: (f"Net GEX of just {fmt_usd(m['net_gex'])} per 1% is light; levels carry "
                   f"little force and the rest of this read should be discounted.")))

    # --- wall proximity --------------------------------------------------- #
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

    # --- wall compression ------------------------------------------------- #
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

    # --- zero gamma proximity --------------------------------------------- #
    R.append(_Rule(
        "flip_watch", "flip_watch", 2,
        lambda m: (not m["no_flip"] and m["zero_gamma"] is not None
                   and abs(m["spot"] - m["zero_gamma"]) / m["spot"] < FLIP_NEAR),
        lambda m: (f"Spot is sitting on the {_px(m['zero_gamma'])} vol trigger "
                   f"({_pct((m['spot'] - m['zero_gamma']) / m['spot'])} away); intraday "
                   f"regime flips are likely.")))

    # --- DEX (near-the-money; see `dex_window` in compute) ---------------- #
    R.append(_Rule(
        "dex_interaction", "dex", 2,
        lambda m: m["regime"] == "negative" and m["dex_window"] < -cfg.dex_band,
        lambda m: (f"With negative gamma and dealers short {fmt_usd(m['dex_window'])} of "
                   f"near-the-money delta, both hedging engines point the same way — "
                   f"expect outsized two-way moves.")))
    R.append(_Rule(
        "dex_standalone", "dex", 4,
        lambda m: abs(m["dex_window"]) > cfg.dex_band,
        lambda m: (f"Near-the-money dealer delta of {fmt_usd(m['dex_window'])} (net short) "
                   f"is squeeze fuel on rallies."
                   if m["dex_window"] < 0 else
                   f"Near-the-money dealer delta of {fmt_usd(m['dex_window'])} (net long) "
                   f"means rallies face passive supply.")))

    # --- vanna ------------------------------------------------------------ #
    R.append(_Rule(
        "vanna_pos", "vanna", 5,
        lambda m: m["vanna_pressure"] > cfg.vanna_band,
        lambda m: (f"Vanna of {fmt_usd(m['vanna_pressure'])} per vol point means a vol crush "
                   f"would fuel dealer buying — a supportive grind if IV bleeds.")))
    R.append(_Rule(
        "vanna_neg", "vanna", 5,
        lambda m: m["vanna_pressure"] < -cfg.vanna_band,
        lambda m: (f"Vanna of {fmt_usd(m['vanna_pressure'])} per vol point means a vol spike "
                   f"would force dealer selling — fragile to bad news.")))

    # --- charm (modulated by OPEX timing) --------------------------------- #
    def _charm_fires(m):
        if m["days_to_opex"] > OPEX_FAR_DAYS and abs(m["charm_drift"]) < cfg.charm_band:
            return False
        return abs(m["charm_drift"]) >= cfg.charm_band or m["days_to_opex"] <= OPEX_NEAR_DAYS

    def _charm_render(m):
        direction = "buying" if m["charm_drift"] > 0 else "selling"
        base = (f"Charm drift of {fmt_usd(m['charm_drift'])}/day points to dealer "
                f"{direction} into the close")
        if m["days_to_opex"] <= OPEX_NEAR_DAYS and m["nearest_magnet"] is not None:
            return (base + f"; pin pressure toward {_px(m['nearest_magnet'])} intensifies "
                    f"into Friday's expiry.")
        return base + "."

    R.append(_Rule("charm", "charm", 5, _charm_fires, _charm_render))

    # --- OPEX cycle timing ------------------------------------------------ #
    R.append(_Rule(
        "opex_week", "opex", 4,
        lambda m: m["days_to_opex"] <= OPEX_NEAR_DAYS,
        lambda m: (f"A large share of visible gamma rolls off at Friday's monthly OPEX "
                   f"({m['next_opex']}); expect the map to reset Monday.")))
    R.append(_Rule(
        "opex_unwind", "opex", 4,
        lambda m: m["days_to_opex"] > OPEX_NEAR_DAYS and m["days_since_opex"] in (1, 2, 3),
        lambda m: (f"Fresh positioning {m['days_since_opex']} day(s) after the monthly "
                   f"OPEX leaves walls less established; trust today's levels less.")))

    # --- 0DTE concentration ----------------------------------------------- #
    R.append(_Rule(
        "dte_dominated", "dte", 5,
        lambda m: m["zero_dte_gamma_share"] > ZERO_DTE_SHARE,
        lambda m: (f"Today's expiry accounts for {m['zero_dte_gamma_share'] * 100:.0f}% of "
                   f"visible gamma; these are intraday-only levels that dissolve at the close.")))

    # --- synthesis -------------------------------------------------------- #
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
        lambda m: ((m["regime"] == "positive" and m["vanna_pressure"] < -cfg.vanna_band)
                   or (m["regime"] == "negative" and m["vanna_pressure"] > cfg.vanna_band)),
        lambda m: (f"{m['regime'].capitalize()} gamma but opposing vanna of "
                   f"{fmt_usd(m['vanna_pressure'])} — a calm tape grinds, yet a vol spike "
                   f"flips the flows, and the vanna side dominates on any vol event.")))

    return R


_RULE_CACHE: dict[str, list[_Rule]] = {}


def _rules_for(cfg: Underlying) -> list[_Rule]:
    if cfg.key not in _RULE_CACHE:
        _RULE_CACHE[cfg.key] = _build_rules(cfg)
    return _RULE_CACHE[cfg.key]


def _quality_warnings(m: dict) -> list[str]:
    out = []
    if m.get("stale"):
        out.append("⚠ Snapshot is over 30 minutes old; positioning may have shifted.")
    if m.get("fallback_spot"):
        out.append(f"⚠ Using fallback spot ({_px(m['spot'])}) — CBOE current_price "
                   f"was unavailable.")
    if m.get("thin_chain"):
        out.append(f"⚠ Unusually thin chain after filtering ({m['n_contracts']} "
                   f"contracts); figures are low-confidence.")
    if m.get("ex_div_risk"):
        out.append(f"⚠ SPY quarterly ex-dividend on {m.get('next_ex_div')} — ITM calls "
                   f"get exercised early, deleting open interest right at OPEX.")
    return out


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
        hl = (f"Positive gamma, pinned under the {_px(m['call_wall'])} call wall — "
              f"range day favoured."
              if m["regime"] == "positive" else
              f"Negative gamma under the {_px(m['call_wall'])} call wall — rallies fragile.")
    elif name == "wall_put_near":
        hl = (f"Positive gamma holding the {_px(m['put_wall'])} put wall — dip support intact."
              if m["regime"] == "positive" else
              f"Negative gamma below the {_px(m['put_wall'])} put wall — downside "
              f"acceleration risk.")
    elif m["regime"] == "positive":
        hl = "Positive gamma — dealer hedging dampens the tape."
    else:
        hl = "Negative gamma — moves amplified, trend conditions."

    if (m.get("zero_dte_gamma_share", 0) > ZERO_DTE_SHARE
            and "levels" in hl and "intraday" not in hl):
        hl = hl.replace("levels", "intraday levels")
    return hl


def generate_commentary(metrics: dict, cfg: Underlying = SPX) -> dict:
    """Deterministic headline + warnings + 3-6 sentences. Pure function."""
    rules = _rules_for(cfg)
    fired = [r for r in rules if r.cond(metrics)]

    by_group: dict[str, list[_Rule]] = {}
    for r in fired:
        by_group.setdefault(r.group, []).append(r)
    survivors: list[_Rule] = []
    for group, rs in by_group.items():
        rs.sort(key=lambda r: (r.priority, r.name))
        survivors.extend(rs[: GROUP_MAX.get(group, 1)])

    names = {r.name for r in survivors}
    if "flip_watch" in names:                            # suppresses gex_size
        survivors = [r for r in survivors if r.group != "gex_size"]
    if any(r.group == "synthesis" for r in survivors):   # suppresses raw flows
        survivors = [r for r in survivors if r.group not in ("dex", "vanna")]

    survivor_names = {r.name for r in survivors}

    survivors.sort(key=lambda r: (GROUP_ORDER[r.group], r.priority, r.name))
    if len(survivors) > MAX_SENTENCES:
        keep = {r.name for r in survivors if r.group == "regime"}
        ranked = sorted(survivors, key=lambda r: (r.priority, GROUP_ORDER[r.group], r.name))
        for r in ranked:
            if len(keep) >= MAX_SENTENCES:
                break
            keep.add(r.name)
        survivors = [r for r in survivors if r.name in keep][:MAX_SENTENCES]

    return {
        "headline": _headline(metrics, survivor_names),
        "warnings": _quality_warnings(metrics),
        "sentences": [r.render(metrics) for r in survivors],
    }


# --------------------------------------------------------------------------- #
# Panel entry point (called by app.py)
# --------------------------------------------------------------------------- #

def refresh(cfg: Underlying) -> dict:
    """Fetch + compute the full snapshot. Raises on any failure."""
    return compute(fetch_chain(cfg), cfg)
