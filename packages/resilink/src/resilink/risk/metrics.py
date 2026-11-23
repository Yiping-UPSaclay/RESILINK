"""Risk metrics with explicit, documented conventions (assumption IDs in knowledge/assumptions.yaml)."""

from __future__ import annotations

import numpy as np


def var_cvar(loss: np.ndarray, alpha: float = 0.95, weights: np.ndarray | None = None) -> tuple[float, float]:
    """Value-at-Risk (alpha-quantile) and Conditional VaR (Rockafellar & Uryasev, 2000):
    CVaR = VaR + E[(L - VaR)^+] / (1 - alpha). Weighted version supports importance weights."""
    x = np.asarray(loss, dtype=float)
    if x.size == 0:
        return float("nan"), float("nan")
    w = np.ones_like(x) if weights is None else np.asarray(weights, dtype=float)
    w = w / w.sum()
    order = np.argsort(x, kind="stable")
    xs, ws = x[order], w[order]
    cdf = np.cumsum(ws)
    var = float(xs[np.searchsorted(cdf, alpha - 1e-12)])
    cvar = var + float(np.sum(ws * np.clip(xs - var, 0.0, None))) / (1.0 - alpha)
    return var, cvar


def eal_from_return_periods(return_periods: np.ndarray, mean_loss: np.ndarray) -> float:
    """Expected annual loss by trapezoidal integration over annual exceedance probability (AEP = 1/RP).

    Convention A-RISK-01: no loss is counted for events more frequent than the most frequent scenario,
    losses are interpolated linearly between scenarios, and the tail beyond the rarest scenario is closed with
    a flat rectangle ``AEP_min * L_rarest``.
    """
    rp = np.asarray(return_periods, dtype=float)
    loss = np.asarray(mean_loss, dtype=float)
    order = np.argsort(rp)
    aep, ls = 1.0 / rp[order], loss[order]
    body = float(np.sum((aep[:-1] - aep[1:]) * (ls[:-1] + ls[1:]) / 2.0)) if len(aep) > 1 else 0.0
    return body + float(aep[-1] * ls[-1])


def amplification_ratio(total: float, direct: float) -> float:
    """Total loss / loss attributable to directly damaged suppliers (interdependency amplification)."""
    return float(total / direct) if direct > 0 else float("nan")
