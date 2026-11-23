"""Exposure: the only place where hazard geometry meets asset geometry (rule R3).

Output contract (one row per exposed asset and scenario):
``scenario_id, asset_rid, asset_kind (node|edge), im_type, im_value, method, exposed_length_m``.
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd

from resilink.io.release import Release
from resilink.network.compiled import CompiledNetwork

COLUMNS = ["scenario_id", "asset_rid", "asset_kind", "im_type", "im_value", "method", "exposed_length_m"]


def compute_exposure(release: Release, scenario_ids: list[str] | None = None) -> pd.DataFrame:
    scenarios = release.read("hazard_scenarios")
    foot: gpd.GeoDataFrame = release.read("hazard_footprints")
    if scenario_ids is not None:
        scenarios = scenarios[scenarios["scenario_id"].isin(scenario_ids)]
        foot = foot[foot["scenario_id"].isin(scenario_ids)]
    im_type = dict(zip(scenarios["scenario_id"], scenarios["im_type"], strict=True))
    nodes: gpd.GeoDataFrame = release.read("nodes")[["rid", "geometry"]]
    edges: gpd.GeoDataFrame = release.read("edges")[["rid", "geometry"]]
    foot = foot[["scenario_id", "im_max", "geometry"]]

    out: list[pd.DataFrame] = []
    pj = gpd.sjoin(nodes, foot, how="inner", predicate="intersects")
    if len(pj):
        agg = pj.groupby(["scenario_id", "rid"], as_index=False)["im_max"].max()
        out.append(pd.DataFrame({
            "scenario_id": agg["scenario_id"], "asset_rid": agg["rid"], "asset_kind": "node",
            "im_type": agg["scenario_id"].map(im_type), "im_value": agg["im_max"].astype(float),
            "method": "point_in_polygon", "exposed_length_m": 0.0,
        }))
    lj = gpd.sjoin(edges, foot, how="inner", predicate="intersects")
    if len(lj):
        geoms = foot.geometry.to_numpy()
        lengths = [
            float(line.intersection(geoms[i]).length)
            for line, i in zip(lj.geometry.to_numpy(), lj["index_right"].to_numpy(), strict=True)
        ]
        lj = lj.assign(exposed_length_m=lengths)
        agg = lj.groupby(["scenario_id", "rid"], as_index=False).agg(
            im_value=("im_max", "max"), exposed_length_m=("exposed_length_m", "sum")
        )
        out.append(pd.DataFrame({
            "scenario_id": agg["scenario_id"], "asset_rid": agg["rid"], "asset_kind": "edge",
            "im_type": agg["scenario_id"].map(im_type), "im_value": agg["im_value"].astype(float),
            "method": "max_along_line", "exposed_length_m": agg["exposed_length_m"].round(1),
        }))
    if not out:
        return pd.DataFrame(columns=COLUMNS)
    return pd.concat(out, ignore_index=True)[COLUMNS].sort_values(["scenario_id", "asset_rid"]).reset_index(drop=True)


def exposure_arrays(exposure: pd.DataFrame, net: CompiledNetwork, scenario_id: str) -> tuple[np.ndarray, np.ndarray]:
    node_im = np.zeros(net.n)
    edge_im = np.zeros(net.e)
    ex = exposure[exposure["scenario_id"] == scenario_id]
    nodes = ex[ex["asset_kind"] == "node"]
    edges = ex[ex["asset_kind"] == "edge"]
    node_im[[net.index[r] for r in nodes["asset_rid"]]] = nodes["im_value"].to_numpy()
    edge_im[[net.edge_index[r] for r in edges["asset_rid"]]] = edges["im_value"].to_numpy()
    return node_im, edge_im
