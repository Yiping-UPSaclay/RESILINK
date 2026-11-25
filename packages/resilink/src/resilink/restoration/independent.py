"""Independent repair (built-in model ``independent_repair``): every damaged element is repaired after a lognormal
time around its class median (system packs, assumption A-REP-01), with no shared crews or spares.

Sprint 0 drew these times inside the fragility step; the draws keep the same order, so results are unchanged.
Plugin module: imports only ``resilink.plugin_api`` from the core.
"""

from __future__ import annotations

import numpy as np

from resilink.plugin_api import DamageState, NetworkView, RepairPlan


class IndependentRepair:
    def __init__(self, net: NetworkView, sigma: float) -> None:
        self.net = net
        self.sigma = sigma

    def plan(self, rng: np.random.Generator, damage: DamageState) -> RepairPlan:
        rep_n = rng.lognormal(np.log(self.net.repair_median_h), self.sigma)
        rep_e = rng.lognormal(np.log(self.net.edge_repair_median_h), self.sigma)
        return RepairPlan(np.where(damage.node_failed, rep_n, 0.0), np.where(damage.edge_failed, rep_e, 0.0))
