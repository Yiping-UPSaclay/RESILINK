"""Gas transmission network of a release (D6, blueprint §5.5).

* **Pipelines** are NaTran's published trace (generalised to about 250 m), noded with a 25 m tolerance. Where the
  trace leaves the territory buffer the cut point is a source: the national network beyond the buffer is not
  modelled (assumption A-GAS-04). The LNG terminals of Fos are sources too.
* **Stations** come from OpenStreetMap (NaTran publishes none; assumption A-GAS-03): delivery stations and
  interconnection points hang off the nearest pipeline within 250 m of their footprint by a short derived
  connector, so that a failed station never cuts the main it sits beside. Compression stations are inline.
* OpenStreetMap's own gas pipelines are not part of the network; they measure how much of NaTran's trace OSM
  also maps, and the reverse (QA).
"""

from __future__ import annotations

from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from resilink.domain.ids import make_rid
from resilink.etl.release.elec import SystemBuild
from resilink.etl.release.frames import Extent, Staged, edge_frame, network_frames, node_frame
from resilink.etl.topology import build_network

ACCURACY_M = 250.0  # NaTran: "précision de l'ordre de 250 m"
SNAP_M = SPLIT_M = 25.0
CLASS_OF_KIND = {"station": "delivery_station", "transmission": "junction", "compression": "compressor",
                 "lng_terminal": "lng_terminal"}
LEAF_KINDS = {"station", "transmission", "lng_terminal"}
SAMPLE_M = 50.0


def _connectors(stations: pd.DataFrame, lines: Any, radius: np.ndarray) -> tuple[pd.DataFrame, np.ndarray]:
    """A straight connector from each leaf station to the nearest point of the trace within its radius, and the
    distance of every station to the trace (inf beyond its radius)."""
    pts = stations.geometry.to_numpy()
    dist = np.full(len(pts), np.inf)
    out = []
    if len(pts) and len(lines):
        si, li = shapely.STRtree(lines).query_nearest(pts, max_distance=float(radius.max()), all_matches=False)
        for s, ln in zip(si, li, strict=True):
            p = pts[s]
            q = shapely.line_interpolate_point(lines[ln], shapely.line_locate_point(lines[ln], p))
            d = float(p.distance(q))
            if d <= radius[s]:
                dist[s] = d
                if d > SPLIT_M:
                    out.append({"key": stations["key"].iat[s], "geometry": shapely.LineString([p, q])})
    return pd.DataFrame(out, columns=["key", "geometry"]), dist


def _coverage(a: Any, b: Any) -> float | None:
    """Share of the length of lines ``a`` lying within ``ACCURACY_M`` of lines ``b`` (sampled every 50 m)."""
    if len(a) == 0:
        return None
    if len(b) == 0:
        return 0.0
    lengths = shapely.length(a)
    pts, w = [], []
    for g, length in zip(a, lengths, strict=True):
        n = max(1, int(length // SAMPLE_M))
        pts.append(shapely.line_interpolate_point(g, (np.arange(n) + 0.5) / n, normalized=True))
        w.append(np.full(n, length / n))
    p, weight = np.concatenate(pts), np.concatenate(w)
    near = np.zeros(len(p), dtype=bool)
    idx, _ = shapely.STRtree(b).query(p, predicate="dwithin", distance=ACCURACY_M)
    near[np.unique(idx)] = True
    return round(float(weight[near].sum() / weight.sum()), 4)


def build_gas(st: Staged, extent: Extent) -> SystemBuild:
    trace = st.read("stg_gas_transmission_natran")
    fac = st.read("stg_gas_facilities_osm")
    fac = fac[fac["kind"].isin(CLASS_OF_KIND)].copy()
    fac["class"] = fac["kind"].map(CLASS_OF_KIND)
    fac["rid"] = [make_rid("gas", c, "osm", i) for c, i in zip(fac["class"], fac["osm_id"], strict=True)]
    fac["key"] = fac["rid"]
    radius = fac["reach_m"].to_numpy(dtype=float) + ACCURACY_M  # footprint reach + trace accuracy

    lines = trace.geometry.to_numpy()
    leaf = fac["kind"].isin(LEAF_KINDS).to_numpy()
    conn, dist = _connectors(fac[leaf], lines, radius[leaf])
    inline = dist <= SPLIT_M  # a station on the trace itself is cut into it
    segs = pd.concat([pd.DataFrame({"derived": False, "geometry": lines}),
                      pd.DataFrame({"derived": True, "geometry": conn["geometry"]})], ignore_index=True)
    segs = gpd.GeoDataFrame(segs, geometry="geometry", crs=trace.crs)
    anchors = pd.concat([
        pd.DataFrame({"rid": fac.loc[leaf, "key"], "x": shapely.get_x(fac.geometry[leaf].to_numpy()),
                      "y": shapely.get_y(fac.geometry[leaf].to_numpy()),
                      "radius_m": np.where(inline, SPLIT_M + 1.0, 1.0), "split": inline}),
        pd.DataFrame({"rid": fac.loc[~leaf, "key"], "x": shapely.get_x(fac.geometry[~leaf].to_numpy()),
                      "y": shapely.get_y(fac.geometry[~leaf].to_numpy()), "radius_m": radius[~leaf], "split": True}),
    ], ignore_index=True)
    net = build_network(segs, anchors, snap_m=SNAP_M, split_m=SPLIT_M, boundary=extent.buffer, same=("derived",))
    rid_of = dict(zip(fac["key"], fac["rid"], strict=True))
    junctions, edges = network_frames(net, "gas", junction_class="junction", edge_class="pipeline_tx",
                                      rid_of_anchor=rid_of, source="natran.trace250", confidence=0.7,
                                      pos_uncertainty_m=ACCURACY_M)
    edges.loc[edges["derived"], ["primary_source", "confidence", "name"]] = ["derived", 0.5, "Station connector (derived)"]
    edges = edges.drop(columns=["derived", "n_segments"])
    attached = set(net.vertices["anchor"].dropna())
    stations = fac[fac["key"].isin(attached)]
    station_nodes = pd.DataFrame({
        "rid": stations["rid"], "class": stations["class"], "name": stations["name"].fillna(""),
        "operator": stations["operator"].fillna(""), "is_source": (stations["class"] == "lng_terminal").to_numpy(),
        "pos_uncertainty_m": np.maximum(10.0, stations["reach_m"].to_numpy(dtype=float)), "confidence": 0.7,
        "primary_source": "osm", "geometry": stations.geometry.to_numpy()})
    nodes = node_frame(pd.concat([station_nodes, junctions], ignore_index=True).assign(layer="gas"), extent)
    edges = edge_frame(edges.assign(layer="gas"), extent)

    osm_pipes = st.read("stg_gas_pipelines_osm")
    core_trace = shapely.intersection(lines, extent.core)
    core_osm = shapely.intersection(osm_pipes.geometry.to_numpy(), extent.core)
    core_trace, core_osm = core_trace[~shapely.is_empty(core_trace)], core_osm[~shapely.is_empty(core_osm)]
    core_fac = fac[extent.in_core(fac.geometry.to_numpy())]
    qa = {**net.stats,
          "stations_by_class": stations["class"].value_counts().sort_index().to_dict(),
          "core_stations": len(core_fac), "core_stations_attached": int(core_fac["key"].isin(attached).sum()),
          "unattached": sorted(set(fac["rid"]) - attached),
          "connectors": len(conn), "lng_terminals_attached": int((stations["class"] == "lng_terminal").sum()),
          "core_km_natran": round(float(shapely.length(core_trace).sum()) / 1000, 1),
          "core_km_osm": round(float(shapely.length(core_osm).sum()) / 1000, 1),
          "core_natran_mapped_in_osm_share": _coverage(core_trace, osm_pipes.geometry.to_numpy()),
          "core_osm_in_natran_share": _coverage(core_osm, lines)}
    xref = pd.DataFrame({"rid": stations["rid"], "source_id": "osm", "source_key": stations["osm_id"]})
    return SystemBuild(nodes=nodes, edges=edges, xref=xref, qa=qa)
