"""Regime classifier and confluence scorer — RESEARCH.md "Composing Them".

A **pure function** of the other panels' payloads: no network, no I/O, fully
testable on synthetic inputs. It is computed last in the refresh cycle, from
whatever the other panels just produced.

Two outputs:

1. **Regime** — one of the four states in the research table, or MIXED when the
   signals genuinely conflict. The layering is deliberate: dealer gamma decides
   whether the tape is self-damping or self-amplifying, and the term structure
   plus implied correlation decide whether that state is stable or being
   undermined. Correlation is the orthogonal input — it is not derived from
   index price action, so it can flag fragility while everything else looks calm.

2. **Confluence** — candidate levels ranked by how many *independent* lenses
   support them. A level with one source is a note; a level with three or more
   is a trade location. Everything is expressed in SPX terms.

Degrades honestly: if an input panel is missing or errored, it classifies from
what remains, lowers the confidence, and names what is missing rather than
quietly guessing.
"""

from __future__ import annotations

# Regime keys
PIN_GRIND = "PIN_GRIND"
UNSTABLE_PIN = "UNSTABLE_PIN"
ACCELERATION = "ACCELERATION"
REFLEXIVE_REPAIR = "REFLEXIVE_REPAIR"
MIXED = "MIXED"

LABELS = {
    PIN_GRIND: "PIN & GRIND",
    UNSTABLE_PIN: "UNSTABLE PIN",
    ACCELERATION: "ACCELERATION",
    REFLEXIVE_REPAIR: "REFLEXIVE REPAIR",
    MIXED: "MIXED",
}

# Regimes group into two families. UNSTABLE PIN *is* PIN & GRIND plus a
# destabiliser, and REFLEXIVE REPAIR is ACCELERATION's book resolving the other
# way, so a vote for one member is not evidence against the other. Confidence is
# measured on the family; what makes the state unstable is reported separately
# as `destabilisers` rather than by docking the agreement score.
FAMILY = {
    PIN_GRIND: "pin",
    UNSTABLE_PIN: "pin",
    ACCELERATION: "trend",
    REFLEXIVE_REPAIR: "trend",
    MIXED: None,
}

POSTURE = {
    PIN_GRIND: ("Fade extremes toward the walls; short vol works; keep size small on "
                "breakouts. Realised vol should stay well under implied."),
    UNSTABLE_PIN: ("Keep the pin trade but buy cheap wings — the tail is being built "
                   "while the tape still looks calm. Watch the distance to the flip."),
    ACCELERATION: ("Do not fade. The put wall is an accelerant, not support. Trade with "
                   "the auction and expect realised vol above implied."),
    REFLEXIVE_REPAIR: ("Long delta or long call spreads — the melt-up is mechanically "
                       "driven by vanna and charm, not by sentiment."),
    MIXED: ("Signals conflict. Trade the levels rather than the regime, and size down "
            "until the inputs agree."),
}

# A level is "the same level" as another within this fraction of spot. SPX
# strikes are 25 points apart, so ~0.2% (~15 points) merges a gamma wall with a
# value-area edge sitting just beside it without swallowing the next strike.
CLUSTER_PCT = 0.002
# How close a level must be to spot to count as "in play" for the headline.
NEAR_SPOT_PCT = 0.015
CUSHION_FRAGILE = 0.0075     # matches gamma_engine's CUSHION_BAND


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _payload(panels: dict, key: str):
    """Accept either raw payloads or store records ({payload, status, ...})."""
    v = panels.get(key)
    if v is None:
        return None
    if isinstance(v, dict) and "payload" in v and "status" in v:
        if v.get("status") == "error" and not v.get("payload"):
            return None
        return v.get("payload")
    return v


def _vote(signal: str, reading: str, supports: str | None) -> dict:
    return {"signal": signal, "reading": reading, "supports": supports}


# --------------------------------------------------------------------------- #
# Regime classification
# --------------------------------------------------------------------------- #

def classify(gamma: dict | None, vix: dict | None, corr: dict | None,
             calendar: dict | None) -> dict:
    votes: list[dict] = []
    missing: list[str] = []

    if gamma is None:
        missing.append("gamma")
    if vix is None:
        missing.append("vix_structure")
    if corr is None:
        missing.append("correlation")
    if calendar is None:
        missing.append("calendar")

    # --- gamma: is the tape self-damping or self-amplifying? -------------- #
    gamma_positive = None
    fragile = False
    if gamma:
        gamma_positive = gamma.get("regime") == "positive"
        cp = gamma.get("cushion_pct")
        fragile = bool(cp is not None and abs(cp) <= CUSHION_FRAGILE)
        if gamma_positive:
            votes.append(_vote(
                "Dealer gamma",
                f"positive, {'thin' if fragile else 'firm'} cushion"
                + (f" ({cp * 100:.2f}%)" if cp is not None else ""),
                UNSTABLE_PIN if fragile else PIN_GRIND))
        else:
            votes.append(_vote(
                "Dealer gamma",
                "negative — hedging amplifies moves", ACCELERATION))

    # --- VIX term structure ------------------------------------------------ #
    backwardated = False
    contango = False
    if vix:
        structure = vix.get("structure")
        backwardated = structure == "backwardation"
        contango = structure == "contango"
        inverted = bool((vix.get("flags") or {}).get("vix3m_inverted"))
        if backwardated or inverted:
            votes.append(_vote("VIX term structure",
                               "backwardated — protection demand outrunning supply",
                               ACCELERATION))
        elif structure == "flat":
            votes.append(_vote("VIX term structure",
                               "flat — the carry underwriting vol supply has gone",
                               UNSTABLE_PIN))
        elif contango:
            votes.append(_vote("VIX term structure",
                               "contango — short-vol carry positive", PIN_GRIND))

    # --- implied correlation (the orthogonal input) ------------------------ #
    corr_spiking = False
    corr_low = False
    corr_high = False
    if corr:
        cf = corr.get("flags") or {}
        corr_spiking = bool(cf.get("spiking_from_lows"))
        corr_low = bool(cf.get("low"))
        corr_high = bool(cf.get("high"))
        pctl = corr.get("cor1m_pctl_2y")
        if corr_spiking:
            votes.append(_vote(
                "Implied correlation",
                f"spiking off the lows (+{corr.get('cor1m_change_pct')}%) — dispersion "
                f"unwind starting", UNSTABLE_PIN))
        elif cf.get("extreme_high"):
            votes.append(_vote("Implied correlation",
                               f"extreme ({pctl}th pctl) — macro shock, everything "
                               f"moving together", ACCELERATION))
        elif corr_high:
            votes.append(_vote("Implied correlation",
                               f"elevated ({pctl}th pctl) — macro driving, trends persist",
                               ACCELERATION))
        elif corr_low:
            votes.append(_vote(
                "Implied correlation",
                f"depressed ({pctl}th pctl) — dispersion crowded, index vol artificially "
                f"suppressed", UNSTABLE_PIN))
        else:
            votes.append(_vote("Implied correlation",
                               f"mid-range ({pctl}th pctl)", None))

    # --- OPEX timing ------------------------------------------------------- #
    if calendar:
        if calendar.get("post_opex_week"):
            votes.append(_vote(
                "OPEX cycle",
                f"post-OPEX week (session {calendar.get('sessions_since_opex')}) — the "
                f"charm bid is gone and gamma has reset", UNSTABLE_PIN))
        else:
            d = calendar.get("days_to_opex")
            votes.append(_vote("OPEX cycle",
                               f"{d} days to monthly OPEX — charm drift still running",
                               PIN_GRIND if not calendar.get("post_opex_week") else None))

    # --- resolve ----------------------------------------------------------- #
    # Conditions that undermine a pin while gamma is still positive. Collected
    # regardless of the branch taken so the panel can always show them.
    destabilisers = []
    if fragile:
        destabilisers.append("the cushion to the flip is thin")
    if corr_spiking:
        destabilisers.append("correlation is spiking off the lows")
    if backwardated:
        destabilisers.append("the VIX curve is backwardated")
    elif vix and vix.get("structure") == "flat":
        destabilisers.append("the VIX curve has gone flat")
    if vix and (vix.get("flags") or {}).get("vix3m_inverted"):
        destabilisers.append("VIX is above VIX3M")
    if calendar and calendar.get("post_opex_week"):
        destabilisers.append("the post-OPEX gamma reset has happened")

    if gamma_positive is None:
        regime = MIXED
        reason = "Dealer gamma is unavailable, so the regime cannot be anchored."
    elif gamma_positive:
        if destabilisers:
            regime = UNSTABLE_PIN
            reason = ("Dealer gamma is still positive so the pin holds intraday, but "
                      + " and ".join(destabilisers) + ".")
        else:
            regime = PIN_GRIND
            reason = ("Positive dealer gamma, positive short-vol carry and no fragility "
                      "signal — the tape is mechanically damped.")
    else:
        repairing = bool(contango and corr and not corr_high and not corr_spiking)
        if backwardated or corr_high:
            regime = ACCELERATION
            reason = ("Dealer gamma is negative and "
                      + ("the curve is backwardated" if backwardated
                         else "correlation is elevated")
                      + " — hedging and macro point the same way.")
        elif repairing:
            regime = REFLEXIVE_REPAIR
            reason = ("Gamma is still negative but the curve has re-steepened and "
                      "correlation is not elevated — the conditions for a vanna and "
                      "charm driven repair rally.")
        else:
            regime = ACCELERATION
            reason = ("Dealer gamma is negative, so hedging amplifies moves even without "
                      "corroboration from the vol complex.")

    family = FAMILY[regime]
    counted = [v for v in votes if v["supports"] is not None]
    supporting = [v for v in counted if FAMILY.get(v["supports"]) == family]
    confidence = (len(supporting) / len(counted)) if (counted and family) else 0.0
    # Missing inputs cap how confident the read can honestly be.
    if missing:
        confidence *= max(0.0, 1.0 - 0.25 * len(missing))

    return {
        "regime": regime,
        "label": LABELS[regime],
        "family": family,
        "posture": POSTURE[regime],
        "reason": reason,
        "votes": votes,
        "destabilisers": destabilisers,
        "confidence": round(confidence, 2),
        "missing": missing,
    }


def invalidation(regime: str, gamma: dict | None, vix: dict | None,
                 corr: dict | None) -> dict:
    """The price level and non-price triggers that would flip the read."""
    price = None
    desc = None
    if gamma and gamma.get("zero_gamma") is not None:
        flip = gamma["zero_gamma"]
        spot = gamma.get("spot")
        price = flip
        if regime in (PIN_GRIND, UNSTABLE_PIN):
            desc = (f"Losing {flip:,.2f} flips dealer gamma negative and voids the "
                    f"pin read.")
        elif regime == ACCELERATION:
            desc = f"Reclaiming {flip:,.2f} restores positive gamma and ends the trend."
        elif regime == REFLEXIVE_REPAIR:
            desc = f"The repair only completes on a reclaim of {flip:,.2f}."
        if spot and desc:
            desc += f" Spot is {abs(spot - flip) / spot * 100:.2f}% away."
    elif gamma and gamma.get("no_flip"):
        desc = "No gamma flip within ±8% of spot — no price-based invalidation in range."

    triggers = []
    if vix:
        f = vix.get("flags") or {}
        if not f.get("vix3m_inverted"):
            triggers.append("VIX/VIX3M crossing above 1.0 (term structure inverting)")
        else:
            triggers.append("VIX/VIX3M falling back below 1.0")
        if vix.get("structure") == "contango":
            triggers.append("the front future dropping below spot VIX (backwardation)")
    if corr:
        cf = corr.get("flags") or {}
        if cf.get("low") and not cf.get("spiking_from_lows"):
            triggers.append("a sharp upward jump in COR1M off the lows")
        elif cf.get("spiking_from_lows"):
            triggers.append("COR1M continuing higher, extending the dispersion unwind")

    return {"price": price, "description": desc, "triggers": triggers}


# --------------------------------------------------------------------------- #
# Confluence scorer
# --------------------------------------------------------------------------- #

def _candidates(gamma_spx, gamma_spy, profile, ratio):
    out: list[tuple[float, str, str]] = []   # (price_spx, source, detail)

    if gamma_spx:
        if gamma_spx.get("call_wall") is not None:
            out.append((gamma_spx["call_wall"], "gamma wall", "SPX call wall"))
        if gamma_spx.get("put_wall") is not None:
            out.append((gamma_spx["put_wall"], "gamma wall", "SPX put wall"))
        if gamma_spx.get("zero_gamma") is not None:
            out.append((gamma_spx["zero_gamma"], "gamma flip", "zero gamma"))
        for mg in gamma_spx.get("oi_magnets") or []:
            out.append((mg["strike"], "OI magnet", f"{mg['oi']:,} contracts"))

    # SPY confirming the same level is an independent book, so it scores.
    if gamma_spy and ratio:
        for key, detail in (("call_wall", "SPY call wall"), ("put_wall", "SPY put wall")):
            v = gamma_spy.get(key)
            if v is not None:
                out.append((v * ratio, "SPY book", detail))

    if profile:
        for key, label in (("composite_poc_spx", "POC"),
                           ("composite_vah_spx", "value area high"),
                           ("composite_val_spx", "value area low")):
            v = profile.get(key)
            if v is not None:
                out.append((v, "value area", label))
        for n in profile.get("naked_pocs") or []:
            if n.get("spx") is not None:
                out.append((n["spx"], "naked POC", f"from {n['date']}"))
    return out


def confluence(gamma_spx, gamma_spy, profile, calendar=None, top=10) -> list[dict]:
    """Cluster candidate levels and score by independent supporting lenses."""
    if not gamma_spx or gamma_spx.get("spot") is None:
        return []
    spot = gamma_spx["spot"]
    ratio = None
    if profile and profile.get("ratio"):
        ratio = profile["ratio"]
    elif gamma_spy and gamma_spy.get("spot"):
        ratio = spot / gamma_spy["spot"]

    cands = _candidates(gamma_spx, gamma_spy, profile, ratio)
    if not cands:
        return []

    tol = spot * CLUSTER_PCT
    cands.sort(key=lambda c: c[0])
    clusters: list[list[tuple[float, str, str]]] = [[cands[0]]]
    for c in cands[1:]:
        if abs(c[0] - clusters[-1][-1][0]) <= tol:
            clusters[-1].append(c)
        else:
            clusters.append([c])

    charm = (gamma_spx or {}).get("charm_drift") or 0.0
    lvn = (profile or {}).get("lvn_zones") or []

    out = []
    for cl in clusters:
        level = sum(c[0] for c in cl) / len(cl)
        sources = sorted({c[1] for c in cl})
        details = [f"{c[1]}: {c[2]}" for c in cl]

        # The charm drift is an independent lens when it points at the level.
        if charm > 0 and level > spot:
            sources.append("charm drift")
            details.append("charm drift points up toward it")
        elif charm < 0 and level < spot:
            sources.append("charm drift")
            details.append("charm drift points down toward it")

        # A level sitting inside a low-volume corridor is *weakened*, not
        # strengthened: nobody agreed on value there (RESEARCH.md §4).
        in_lvn = any(z.get("lo_spx") is not None
                     and z["lo_spx"] <= level <= z["hi_spx"] for z in lvn)

        out.append({
            "level": round(level, 2),
            "sources": sources,
            "details": details,
            "score": len(set(sources)),
            "distance": round(level - spot, 2),
            "distance_pct": round((level - spot) / spot, 5),
            "side": "above" if level > spot else "below",
            "in_lvn": in_lvn,
            "near_spot": abs(level - spot) / spot <= NEAR_SPOT_PCT,
        })

    out.sort(key=lambda x: (-x["score"], abs(x["distance_pct"])))
    return out[:top]


# --------------------------------------------------------------------------- #
# Compute
# --------------------------------------------------------------------------- #

def compute(panels: dict) -> dict:
    gamma_spx = _payload(panels, "gamma_spx")
    gamma_spy = _payload(panels, "gamma_spy")
    vix = _payload(panels, "vix_structure")
    corr = _payload(panels, "correlation")
    profile = _payload(panels, "volume_profile")
    calendar = _payload(panels, "calendar")

    cls = classify(gamma_spx, vix, corr, calendar)
    inv = invalidation(cls["regime"], gamma_spx, vix, corr)
    conf = confluence(gamma_spx, gamma_spy, profile, calendar)

    payload = {
        **cls,
        "invalidation": inv,
        "confluence": conf,
        "spot": (gamma_spx or {}).get("spot"),
        "strong_levels": [c for c in conf if c["score"] >= 3],
    }
    payload["commentary"] = _commentary(payload)
    return payload


def _commentary(p: dict) -> dict:
    sentences = [p["reason"], p["posture"]]
    warnings = []

    inv = p["invalidation"]
    if inv.get("description"):
        sentences.append(inv["description"])
    if inv.get("triggers"):
        sentences.append("Non-price triggers to watch: " + "; ".join(inv["triggers"]) + ".")

    strong = p["strong_levels"]
    if strong:
        near = sorted(strong, key=lambda c: abs(c["distance_pct"]))[0]
        sentences.append(
            f"{near['level']:,.0f} is the highest-conviction level "
            f"({near['score']} independent sources: {', '.join(near['sources'])}), "
            f"{abs(near['distance_pct']) * 100:.2f}% {near['side']} spot.")
    else:
        sentences.append(
            "No level currently carries three or more independent sources; treat the "
            "individual walls and value-area edges as notes rather than trade locations.")

    if p["missing"]:
        warnings.append("⚠ Classified without " + ", ".join(p["missing"])
                        + " — confidence is reduced accordingly.")
    if p["confidence"] < 0.5:
        warnings.append(f"⚠ Low agreement between inputs (confidence "
                        f"{p['confidence']:.0%}); the signals are not lining up.")

    return {"headline": f"{p['label']} — {p['confidence']:.0%} signal agreement",
            "warnings": warnings, "sentences": sentences}
