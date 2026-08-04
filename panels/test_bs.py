"""Tests for the Black-Scholes greeks.

The interesting ones validate vanna and charm against central finite differences
of delta, which is the definition each is meant to satisfy. That catches sign
errors and mis-transcribed terms — the two failure modes that would silently
corrupt every downstream flow number.

    pytest panels/test_bs.py
"""

from __future__ import annotations

import numpy as np

from panels import _bs

R, Q = 0.04, 0.012

# A spread of moneyness / tenor / vol, all comfortably away from T_FLOOR so the
# finite differences are clean.
CASES = [
    # S, K, sigma, T
    (7700.0, 7700.0, 0.15, 30 / 365),   # ATM, 1 month
    (7700.0, 8000.0, 0.14, 30 / 365),   # OTM call
    (7700.0, 7400.0, 0.19, 30 / 365),   # OTM put
    (7700.0, 7700.0, 0.13, 90 / 365),   # ATM, 3 months
    (7700.0, 8200.0, 0.16, 7 / 365),    # far OTM, 1 week
    (768.0, 768.0, 0.15, 30 / 365),     # SPY scale
    (768.0, 800.0, 0.17, 60 / 365),
]


def _delta(S, K, sigma, T, is_call):
    return float(_bs.bs_delta(S, K, sigma, T, R, Q, is_call))


def test_vanna_matches_finite_difference_of_delta():
    """vanna == dDelta/dSigma, and is identical for calls and puts."""
    h = 1e-5
    for S, K, sigma, T in CASES:
        for is_call in (True, False):
            num = (_delta(S, K, sigma + h, T, is_call)
                   - _delta(S, K, sigma - h, T, is_call)) / (2 * h)
            ana = float(_bs.bs_vanna(S, K, sigma, T, R, Q))
            assert abs(num - ana) < 1e-4 * max(1.0, abs(ana)), (
                f"vanna mismatch S={S} K={K} sig={sigma} T={T} call={is_call}: "
                f"numeric {num} vs analytic {ana}")


def test_charm_matches_finite_difference_of_delta():
    """charm == dDelta/dt with t running forward, i.e. -dDelta/dT."""
    h = 1e-6
    for S, K, sigma, T in CASES:
        for is_call in (True, False):
            # -dDelta/dT  ==  (Delta(T-h) - Delta(T+h)) / 2h
            num = (_delta(S, K, sigma, T - h, is_call)
                   - _delta(S, K, sigma, T + h, is_call)) / (2 * h)
            ana = float(_bs.bs_charm(S, K, sigma, T, R, Q, is_call))
            assert abs(num - ana) < 1e-3 * max(1.0, abs(ana)), (
                f"charm mismatch S={S} K={K} sig={sigma} T={T} call={is_call}: "
                f"numeric {num} vs analytic {ana}")


def test_gamma_matches_finite_difference_of_delta():
    h = 1e-3
    for S, K, sigma, T in CASES:
        num = (_delta(S + h, K, sigma, T, True)
               - _delta(S - h, K, sigma, T, True)) / (2 * h)
        ana = float(_bs.bs_gamma(S, K, sigma, T, R, Q))
        assert abs(num - ana) < 1e-6


def test_put_call_parity_of_delta():
    """delta_call - delta_put == e^(-qT) for every case."""
    for S, K, sigma, T in CASES:
        c = _delta(S, K, sigma, T, True)
        p = _delta(S, K, sigma, T, False)
        assert abs((c - p) - np.exp(-Q * T)) < 1e-12


def test_vanna_is_right_signed_around_the_money():
    """Vanna is positive for OTM calls / negative for ITM calls (d2 < 0 flips it)."""
    otm = float(_bs.bs_vanna(7700.0, 8000.0, 0.15, 30 / 365, R, Q))
    itm = float(_bs.bs_vanna(7700.0, 7400.0, 0.15, 30 / 365, R, Q))
    assert otm > 0 and itm < 0


def test_charm_pulls_otm_delta_toward_zero():
    """An OTM call must lose delta as time passes: charm < 0."""
    assert float(_bs.bs_charm(7700.0, 8100.0, 0.15, 30 / 365, R, Q, True)) < 0
    # An OTM put's delta is negative and rises toward zero, so charm > 0.
    assert float(_bs.bs_charm(7700.0, 7300.0, 0.15, 30 / 365, R, Q, False)) > 0


def test_arrays_broadcast_and_match_scalars():
    K = np.array([7400.0, 7700.0, 8000.0])
    sigma = np.array([0.19, 0.15, 0.14])
    T = np.array([30 / 365, 30 / 365, 30 / 365])
    is_call = np.array([True, False, True])
    v = _bs.bs_vanna(7700.0, K, sigma, T, R, Q)
    c = _bs.bs_charm(7700.0, K, sigma, T, R, Q, is_call)
    assert v.shape == (3,) and c.shape == (3,)
    for i in range(3):
        assert abs(v[i] - float(_bs.bs_vanna(7700.0, K[i], sigma[i], T[i], R, Q))) < 1e-12
        assert abs(c[i] - float(_bs.bs_charm(
            7700.0, K[i], sigma[i], T[i], R, Q, is_call[i]))) < 1e-12


def test_t_floor_keeps_expiring_contracts_finite():
    """T=0 must not produce inf/nan — 0DTE rows are common in the live chain."""
    for fn, args in (
        (_bs.bs_gamma, (7700.0, 7700.0, 0.15, 0.0, R, Q)),
        (_bs.bs_vanna, (7700.0, 7700.0, 0.15, 0.0, R, Q)),
        (_bs.bs_charm, (7700.0, 7700.0, 0.15, 0.0, R, Q, True)),
        (_bs.bs_delta, (7700.0, 7700.0, 0.15, 0.0, R, Q, True)),
    ):
        out = np.asarray(fn(*args), dtype=float)
        assert np.all(np.isfinite(out)), f"{fn.__name__} not finite at T=0"
