"""Mobile telecommunications of a release (D6).

* **Mobile sites** are ARCEP's site list, one node per site and operator (``telecom.mobile_site:anfr:<id>-<op>``).
* **Exchanges** are OpenStreetMap telecom exchanges (central offices); data centres are not backhaul points and
  stay out. Exchanges are the system's sources.
* Open data says nothing about backhaul: each site is linked to its nearest exchange by a logical edge
  (assumption A-TEL-04). The edge has no fragility of its own; it carries the exchange's failure to the site.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import shapely
from scipy.spatial import cKDTree

from resilink.domain.ids import make_rid
from resilink.etl.release.elec import SystemBuild
from resilink.etl.release.frames import Extent, Staged, edge_frame, link_edges, node_frame


def build_telecom(st: Staged, extent: Extent) -> SystemBuild:
    sites = st.read("stg_telecom_mobile_sites")
    ex = st.read("stg_telecom_exchanges_osm")
    ex = ex[ex["telecom"] == "exchange"].copy()
    ex["rid"] = [make_rid("telecom", "exchange", "osm", i) for i in ex["osm_id"]]
    site_nodes = pd.DataFrame({
        "rid": sites["rid"], "class": "mobile_site", "name": sites["num_site"].fillna("").astype(str),
        "operator": sites["operator"].to_numpy(), "pos_uncertainty_m": 10.0, "confidence": 0.95,
        "primary_source": "arcep.sites", "geometry": sites.geometry.to_numpy()})
    ex_nodes = pd.DataFrame({
        "rid": ex["rid"], "class": "exchange", "name": ex["name"].fillna(""), "operator": ex["operator"].fillna(""),
        "is_source": True, "pos_uncertainty_m": 10.0, "confidence": 0.8, "primary_source": "osm",
        "geometry": ex.geometry.to_numpy()})
    nodes = node_frame(pd.concat([site_nodes, ex_nodes], ignore_index=True).assign(layer="telecom"), extent)

    sxy = shapely.get_coordinates(sites.geometry.to_numpy())
    exy = shapely.get_coordinates(ex.geometry.to_numpy())
    dist, idx = cKDTree(exy).query(sxy)
    pairs = pd.DataFrame({"src_rid": ex["rid"].to_numpy()[idx], "dst_rid": sites["rid"].to_numpy()})
    links = link_edges(pairs, nodes, "telecom", "backhaul_logical", source="derived", confidence=0.3, key="dst_rid")
    edges = edge_frame(links.assign(name="Backhaul to nearest exchange (logical)"), extent)

    core = nodes["in_core"].to_numpy()[: len(sites)]
    qa = {"sites": len(sites), "core_sites": int(core.sum()), "exchanges": len(ex),
          "core_exchanges": int(extent.in_core(ex.geometry.to_numpy()).sum()),
          "data_centres_left_out": int((st.read("stg_telecom_exchanges_osm")["telecom"] == "data_center").sum()),
          "core_backhaul_km_quantiles": {q: round(float(np.quantile(dist[core], q)) / 1000, 2)
                                         for q in (0.5, 0.9, 0.99)} if core.any() else {}}
    xref = pd.concat([
        pd.DataFrame({"rid": sites["rid"], "source_id": "arcep.sites", "source_key": sites["id_station_anfr"].fillna("")}),
        pd.DataFrame({"rid": ex["rid"], "source_id": "osm", "source_key": ex["osm_id"]}),
    ], ignore_index=True)
    return SystemBuild(nodes=nodes, edges=edges, xref=xref[xref["source_key"] != ""], qa=qa)
