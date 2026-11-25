"""People-hours of lost service (built-in model ``people_hours``, assumption A-LOSS-01).

For each system that serves the population grid, a cell's outage is the supply-weighted downtime of the assets
that serve it; the loss is population x outage hours. Sprint 0 computed this inside the Monte Carlo loop; the
arithmetic is unchanged. Plugin module: imports only ``resilink.plugin_api`` from the core.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from resilink.plugin_api import NetworkView


@dataclass(frozen=True)
class SectorOutcome:
    cell_outage_h: np.ndarray  # outage hours per grid cell
    cell_disrupted: np.ndarray  # bool: outage above the disruption threshold
    people_hours: float  # population x outage hours, all causes
    people_hours_direct: float  # population x outage hours caused directly by hazard damage


class PeopleHours:
    def __init__(self, net: NetworkView, disruption_threshold_h: float) -> None:
        self.net = net
        self.threshold = disruption_threshold_h

    def evaluate(self, total_downtime_h: np.ndarray, direct_downtime_h: np.ndarray) -> dict[str, SectorOutcome]:
        out: dict[str, SectorOutcome] = {}
        for sector in self.net.sectors:
            out_t = self.net.supply[sector] @ total_downtime_h
            out_d = self.net.supply[sector] @ direct_downtime_h
            out[sector] = SectorOutcome(out_t, out_t > self.threshold, float(self.net.cell_pop @ out_t),
                                        float(self.net.cell_pop @ out_d))
        return out
