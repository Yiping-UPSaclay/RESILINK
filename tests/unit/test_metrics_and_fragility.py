from __future__ import annotations

import numpy as np
import pytest

from resilink.fragility.lognormal import FragilityCurve, FragilityLibrary
from resilink.risk.metrics import amplification_ratio, eal_from_return_periods, var_cvar


def test_var_cvar_uniform_losses():
    x = np.arange(1, 101, dtype=float)  # 1..100
    var, cvar = var_cvar(x, 0.95)
    assert var == 95.0
    assert cvar == pytest.approx(95.0 + (1 + 2 + 3 + 4 + 5) / 100 / 0.05)  # = 98.0 (mean of the top 5 % tail)
    assert cvar >= var


def test_eal_trapezoid_with_flat_tail():
    # AEP 0.1, 0.01, 0.001 with losses 10, 100, 1000
    eal = eal_from_return_periods(np.array([10, 100, 1000]), np.array([10.0, 100.0, 1000.0]))
    body = (0.1 - 0.01) * (10 + 100) / 2 + (0.01 - 0.001) * (100 + 1000) / 2
    assert eal == pytest.approx(body + 0.001 * 1000)


def test_amplification_ratio():
    assert amplification_ratio(30.0, 10.0) == 3.0
    assert np.isnan(amplification_ratio(5.0, 0.0))


def test_lognormal_fragility_median_and_zero_intensity():
    lib = FragilityLibrary(version="t", curves=[FragilityCurve(asset_class="x", median=1.0, beta=0.5, assumption_id="A")])
    p = lib.p_fail(np.array(["x", "x", "x", "unknown"]), np.array([1.0, 0.0, 10.0, 5.0]))
    assert p[0] == pytest.approx(0.5)
    assert p[1] == 0.0
    assert p[2] > 0.99
    assert p[3] == 0.0  # no curve -> never fails
