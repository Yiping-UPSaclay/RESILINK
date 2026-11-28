"""Service links of a release (D6): which supplier serves each inhabited 200 m cell of the core, per system.

The supplier class of each system comes from its pack (``served.population_supplier``). Open data has no
customer-to-asset records, so links are proxies (all grade C, sensitivity-tested):

* electricity: the nearest MV/LV substation (assumption A-SVC-01);
* mobile telecommunications: for each operator, its nearest site, with an equal share per operator
  (assumption A-SVC-02);
* gas: cells crossed by a GRDF natural-gas distribution pipe (propane networks excluded), from the nearest
  delivery station (assumption A-SVC-03).

Only suppliers connected to a source at baseline can be linked, so no cell is cut off before any hazard.
"""

from __future__ import annotations

from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from scipy.spatial import cKDTree

from resilink.registry import SystemCatalog

MAX_SITE_M = 15_000.0  # beyond this an operator is taken to have no coverage of the cell


def _q(d: np.ndarray) -> dict[str, float]:
    return {str(q): round(float(np.quantile(d, q)), 1) for q in (0.5, 0.9, 0.99)} if len(d) else {}


def service_links(grid: gpd.GeoDataFrame, nodes: gpd.GeoDataFrame, gas_pipes: gpd.GeoDataFrame,
                  catalog: SystemCatalog) -> tuple[pd.DataFrame, dict[str, Any]]:
    centre = shapely.get_coordinates(grid.geometry.centroid.to_numpy())
    cell = grid["cell_id"].to_numpy()
    pop = grid["pop"].to_numpy(dtype=float)
    parts: list[pd.DataFrame] = []
    qa: dict[str, Any] = {}
    for system, cls in catalog.population_suppliers(sorted(set(nodes["layer"]))):
        sup = nodes[(nodes["layer"] == system) & (nodes["class"] == cls)]
        sxy = shapely.get_coordinates(sup.geometry.to_numpy())
        rid = sup["rid"].to_numpy()
        if system == "telecom":
            ops = sorted(sup["operator"].unique())
            for op in ops:
                mask = (sup["operator"] == op).to_numpy()
                d, i = cKDTree(sxy[mask]).query(centre, distance_upper_bound=MAX_SITE_M)
                ok = np.isfinite(d)
                parts.append(pd.DataFrame({"cell_id": cell[ok], "supplier_rid": rid[mask][i[ok]], "sector": system,
                                           "share": 1.0 / len(ops)}))
                qa[f"telecom_{op}"] = {"cells": int(ok.sum()), "distance_m": _q(d[ok])}
            continue
        target = np.ones(len(grid), dtype=bool)
        if system == "gas":
            natural = gas_pipes[~gas_pipes["propane"].astype(bool)]
            ci, _ = shapely.STRtree(natural.geometry.to_numpy()).query(grid.geometry.to_numpy(), predicate="intersects")
            target = np.zeros(len(grid), dtype=bool)
            target[np.unique(ci)] = True
        if not len(sup) or not target.any():
            qa[system] = {"cells": 0}
            continue
        d, i = cKDTree(sxy).query(centre[target])
        parts.append(pd.DataFrame({"cell_id": cell[target], "supplier_rid": rid[i], "sector": system, "share": 1.0}))
        qa[system] = {"cells": int(target.sum()), "population_share": round(float(pop[target].sum() / pop.sum()), 4),
                      "suppliers_used": len(np.unique(i)), "distance_m": _q(d)}
    columns = ["cell_id", "supplier_rid", "sector", "share"]
    links = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=columns)
    links = links[columns].sort_values(["sector", "cell_id", "supplier_rid"]).reset_index(drop=True)
    return links, qa
