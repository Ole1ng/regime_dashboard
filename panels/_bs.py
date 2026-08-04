"""Black-Scholes Greeks used by the gamma engine.

Ported from market_almanack/panels/_bs.py, which was verified correct before the
port. Isolated from the dashboard glue on purpose: this module knows nothing
about CBOE, FastAPI, or the store — it is pure maths so it can be unit-tested and
reasoned about on its own. All functions are numpy-friendly: pass scalars or
arrays for ``S, K, sigma, T`` and you get the matching shape back.

The CBOE feed quotes gamma and delta, so live snapshots use those directly; this
module supplies the two Greeks the feed omits (vanna, charm), plus:

  * ``bs_gamma`` — re-evaluated at hypothetical spots by the zero-gamma ladder.
  * ``bs_delta`` — used by the charm-decay projection (which re-prices the book at
    future dates) and by the finite-difference tests that validate vanna/charm.

Sign convention for ``bs_charm``: it is ∂Δ/∂t with *t running forward*, i.e.
−∂Δ/∂T. A contract's delta ``d`` days from now is therefore approximately
``delta + charm * d/365``.
"""

from __future__ import annotations

import numpy as np
from scipy.stats import norm

# Minimum time-to-expiry in years (≈ one hour) so 0DTE contracts do not blow up
# the 1/(sigma*sqrt(T)) terms.
T_FLOOR = 1.0 / (365.0 * 24.0)


def d1_d2(S, K, sigma, T, r, q):
    """Standard Black-Scholes d1/d2. Inputs may be scalars or numpy arrays."""
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    T = np.maximum(np.asarray(T, dtype=float), T_FLOOR)
    vol_t = sigma * np.sqrt(T)
    d1 = (np.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / vol_t
    d2 = d1 - vol_t
    return d1, d2


def bs_gamma(S, K, sigma, T, r, q):
    """Black-Scholes gamma — recomputed at hypothetical spots for the ladder."""
    S = np.asarray(S, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    T = np.maximum(np.asarray(T, dtype=float), T_FLOOR)
    d1, _ = d1_d2(S, K, sigma, T, r, q)
    return np.exp(-q * T) * norm.pdf(d1) / (S * sigma * np.sqrt(T))


def bs_delta(S, K, sigma, T, r, q, is_call):
    """Black-Scholes delta. ``is_call`` is a bool array (True=call, False=put)."""
    T = np.maximum(np.asarray(T, dtype=float), T_FLOOR)
    is_call = np.asarray(is_call, dtype=bool)
    d1, _ = d1_d2(S, K, sigma, T, r, q)
    disc = np.exp(-q * T)
    return np.where(is_call, disc * norm.cdf(d1), disc * (norm.cdf(d1) - 1.0))


def bs_vanna(S, K, sigma, T, r, q):
    """∂Δ/∂σ. Same expression for calls and puts."""
    sigma = np.asarray(sigma, dtype=float)
    T = np.maximum(np.asarray(T, dtype=float), T_FLOOR)
    d1, d2 = d1_d2(S, K, sigma, T, r, q)
    return -np.exp(-q * T) * norm.pdf(d1) * d2 / sigma


def bs_charm(S, K, sigma, T, r, q, is_call):
    """∂Δ/∂t per year (time running forward). ``is_call`` is a bool array."""
    sigma = np.asarray(sigma, dtype=float)
    T = np.maximum(np.asarray(T, dtype=float), T_FLOOR)
    is_call = np.asarray(is_call, dtype=bool)
    d1, d2 = d1_d2(S, K, sigma, T, r, q)
    vol_t = sigma * np.sqrt(T)
    charm_call = (
        q * np.exp(-q * T) * norm.cdf(d1)
        - np.exp(-q * T) * norm.pdf(d1) * (2 * (r - q) * T - d2 * vol_t) / (2 * T * vol_t)
    )
    charm_put = charm_call - q * np.exp(-q * T)
    return np.where(is_call, charm_call, charm_put)
