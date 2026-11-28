"""QA gates of a release (blueprint §5.6). A release is published only when every gate passes.

Schema validity, references between tables and the match with the installed system packs are checked when the
tables are assembled (a failure stops the build); the gates below measure the data against stated thresholds.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from scipy.spatial import cKDTree

from resilink.io.paths import load_yaml


@dataclass(frozen=True)
class Gate:
    id: str
    text: str
    value: float | None
    minimum: float
    maximum: float = float("inf")

    @property
    def passed(self) -> bool:
        return self.value is not None and self.minimum <= self.value <= self.maximum

    def row(self) -> dict[str, Any]:
        return {**asdict(self), "maximum": None if self.maximum == float("inf") else self.maximum, "pass": self.passed}


def dependency_candidates(nodes: gpd.GeoDataFrame, rules_path: str = "knowledge/dependency_rules.yaml"
                          ) -> dict[str, dict[str, Any]]:
    """Per dependency rule: core dependents with at least one supporter within the rule's distance."""
    out: dict[str, dict[str, Any]] = {}
    lc = nodes["layer"] + "." + nodes["class"]
    for rule in load_yaml(rules_path)["rules"]:
        dep = nodes[(lc == rule["dependent"]) & nodes["in_core"]]
        sup = nodes[lc == rule["supporter"]]
        if not len(dep):
            out[rule["id"]] = {"core_dependents": 0}
            continue
        d = cKDTree(shapely.get_coordinates(sup.geometry.to_numpy())).query(
            shapely.get_coordinates(dep.geometry.to_numpy()))[0] if len(sup) else np.full(len(dep), np.inf)
        p95 = float(np.quantile(d, 0.95)) if np.isfinite(d).all() else None
        out[rule["id"]] = {"core_dependents": len(dep), "max_distance_m": rule["max_distance_m"],
                           "with_candidate_share": round(float((d <= rule["max_distance_m"]).mean()), 4),
                           "nearest_m_p95": None if p95 is None else round(p95, 1)}
    return out


def evaluate(qa: dict[str, Any], nodes: gpd.GeoDataFrame, links: pd.DataFrame, grid: gpd.GeoDataFrame,
             insee_pop: float | None) -> list[Gate]:
    sites = nodes[(nodes["class"] == "mobile_site") & nodes["in_core"]]
    subs = nodes[nodes["class"] == "sub_mv_lv"]
    near = cKDTree(shapely.get_coordinates(subs.geometry.to_numpy())).query(
        shapely.get_coordinates(sites.geometry.to_numpy()))[0] if len(subs) and len(sites) else np.array([])
    pop = float(grid["pop"].sum())
    systems = {"elec", "gas", "telecom"}
    present = {s for s in systems if nodes.loc[nodes["layer"] == s, "is_source"].any()
               and (links["sector"] == s).any()}
    return [
        Gate("rte_substations_in_osm", "Core RTE substations in service found in OpenStreetMap",
             qa["elec"]["rte_matching"]["core_match_share"], 0.90),
        Gate("mv_lv_substations_attached", "Core MV/LV substations attached to an MV line",
             qa["elec"]["mv"]["core_attached_share"], 0.95),
        Gate("mv_lv_substations_fed", "Core MV/LV substations connected to a primary substation",
             qa["elec"]["mv"]["core_reachable_from_primary_share"], 0.90),
        Gate("mobile_sites_with_power_candidate", "Core mobile sites with an MV/LV substation within 1.5 km",
             round(float((near <= 1500).mean()), 4) if len(near) else None, 0.99),
        Gate("population_vs_insee", "Population of the release grid / INSEE Filosofi total of the core",
             round(pop / insee_pop, 4) if insee_pop else None, 0.99, 1.01),
        Gate("suppliers_served_at_baseline", "Linked suppliers connected to a source with no damage",
             qa["baseline"]["linked_suppliers_served_share"], 1.0),
        Gate("systems_present", "Electricity, gas and telecom each have a source and serve the grid",
             len(present) / len(systems), 1.0),
    ]
