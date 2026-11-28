"""Shared pieces of the release build: staged inputs, canonical node and edge frames, stable ids for derived assets."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import shapely

from resilink.domain.ids import line_fingerprint, make_rid, point_fingerprint
from resilink.etl.topology import Network
from resilink.io import geoparquet as gpq

CRS = "EPSG:2154"
NODE_COLUMNS = ["rid", "layer", "class", "name", "operator", "status", "is_source", "voltage_kv", "pos_uncertainty_m",
                "confidence", "primary_source", "sensitivity", "in_core", "geometry"]
EDGE_COLUMNS = ["rid", "layer", "class", "name", "src_rid", "dst_rid", "directed", "length_m", "aerial_share",
                "voltage_kv", "confidence", "primary_source", "sensitivity", "in_core", "geometry"]
NODE_DEFAULTS: dict[str, Any] = {"name": "", "operator": "", "status": "in_service", "is_source": False,
                                 "voltage_kv": np.nan, "sensitivity": "internal"}
EDGE_DEFAULTS: dict[str, Any] = {"name": "", "directed": False, "aerial_share": np.nan, "voltage_kv": np.nan,
                                 "sensitivity": "internal"}


@dataclass(frozen=True)
class Staged:
    """The staged tables of one territory, read on demand, with their JSON records (snapshots, licences, QA).
    ``used`` collects the tables read, whose snapshots and licences the release manifest lists."""

    root: Path
    used: set[str] = field(default_factory=set)

    def read(self, table: str) -> gpd.GeoDataFrame | pd.DataFrame:
        path = self.root / f"{table}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"staged table {table!r} missing in {self.root}: run `resilink etl stage`")
        self.used.add(table)
        is_geo = b"geo" in (pq.read_schema(path).metadata or {})
        return gpq.read_canonical(path) if is_geo else gpq.read_table(path)

    def record(self, table: str) -> dict[str, Any]:
        return json.loads((self.root / f"{table}.json").read_text(encoding="utf-8"))

    def tables(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json"))


@dataclass(frozen=True)
class Extent:
    core: Any
    buffer: Any

    def in_core(self, geoms: Any) -> np.ndarray:
        return shapely.intersects(np.asarray(geoms), self.core)


def unique(keys: list[str]) -> list[str]:
    """Repeated keys get '-2', '-3'... in the order given (callers sort deterministically first)."""
    seen: Counter[str] = Counter()
    out = []
    for k in keys:
        seen[k] += 1
        out.append(k if seen[k] == 1 else f"{k}-{seen[k]}")
    return out


def _defaults(gdf: gpd.GeoDataFrame, defaults: dict[str, Any]) -> gpd.GeoDataFrame:
    """Missing columns and missing values (frames of several systems concatenated) take the defaults."""
    for col, value in defaults.items():
        gdf[col] = gdf[col].where(gdf[col].notna(), value) if col in gdf else value
    return gdf


def node_frame(df: pd.DataFrame | list[dict[str, Any]], extent: Extent) -> gpd.GeoDataFrame:
    gdf = _defaults(gpd.GeoDataFrame(df, geometry="geometry", crs=CRS), NODE_DEFAULTS)
    gdf["is_source"] = gdf["is_source"].astype(bool)
    gdf["in_core"] = extent.in_core(gdf.geometry.to_numpy())
    return gdf[NODE_COLUMNS]


def edge_frame(df: pd.DataFrame | list[dict[str, Any]], extent: Extent) -> gpd.GeoDataFrame:
    gdf = _defaults(gpd.GeoDataFrame(df, geometry="geometry", crs=CRS), EDGE_DEFAULTS)
    gdf["directed"] = gdf["directed"].astype(bool)
    gdf["in_core"] = extent.in_core(gdf.geometry.to_numpy())
    return gdf[EDGE_COLUMNS]


def network_frames(net: Network, layer: str, *, junction_class: str, edge_class: str | pd.Series, rid_of_anchor: dict[str, str],
                   source: str, confidence: float, pos_uncertainty_m: float) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Junction nodes (vertices without an anchor; boundary vertices are sources) and edges with stable rids.

    Junction rids fingerprint the position at 1 m; edge rids fingerprint class, end points and length (blueprint
    §4.2). Repeats (two vertices in one square metre, parallel circuits of equal length) get a numeric suffix.
    """
    v = net.vertices
    free = v[v["anchor"].isna()].sort_values(["x", "y"])
    jkeys = unique([point_fingerprint(x, y) for x, y in zip(free["x"], free["y"], strict=True)])
    vertex_rid = dict(zip(free["vid"], (make_rid(layer, junction_class, "fp", k) for k in jkeys), strict=True))
    for vid, anchor in zip(v["vid"], v["anchor"], strict=True):
        if pd.notna(anchor):
            vertex_rid[vid] = rid_of_anchor[anchor]
    junctions = pd.DataFrame({
        "rid": [vertex_rid[i] for i in free["vid"]], "layer": layer, "class": junction_class,
        "name": np.where(free["boundary"], "Territory boundary (network continues)", ""),
        "is_source": free["boundary"].to_numpy(dtype=bool), "pos_uncertainty_m": pos_uncertainty_m,
        "confidence": confidence, "primary_source": source,
        "geometry": shapely.points(free["x"].to_numpy(), free["y"].to_numpy()),
    })
    e = net.edges.copy()
    e["class"] = edge_class if isinstance(edge_class, str) else edge_class.to_numpy()
    e["src_rid"] = e["u"].map(vertex_rid)
    e["dst_rid"] = e["v"].map(vertex_rid)
    keys = [line_fingerprint(shapely.get_coordinates(g), c, length) for g, c, length in
            zip(e.geometry, e["class"], e["length_m"], strict=True)]
    e = e.assign(_k=keys, _w=shapely.to_wkb(e.geometry.to_numpy())).sort_values(["_k", "src_rid", "dst_rid", "_w"])
    e["rid"] = [make_rid(layer, c, "fp", k) for c, k in zip(e["class"], unique(e["_k"].tolist()), strict=True)]
    e = e.drop(columns=["_k", "_w", "u", "v"]).assign(layer=layer, confidence=confidence, primary_source=source)
    return junctions, e.reset_index(drop=True)


def anchors_from(points: gpd.GeoDataFrame | pd.DataFrame, key: str, radius_m: Any, split: bool = True) -> pd.DataFrame:
    geom = points.geometry if isinstance(points, gpd.GeoDataFrame) else points["geometry"]
    return pd.DataFrame({"rid": points[key].to_numpy(), "x": shapely.get_x(geom.to_numpy()),
                         "y": shapely.get_y(geom.to_numpy()), "radius_m": radius_m, "split": split})


def link_edges(pairs: pd.DataFrame, nodes: gpd.GeoDataFrame, layer: str, cls: str, *, source: str,
               confidence: float, key: str = "src_rid") -> pd.DataFrame:
    """Straight logical or connection edges between two node sets (``pairs``: src_rid, dst_rid)."""
    xy = nodes.set_index("rid").geometry
    geoms = shapely.linestrings([[(a.x, a.y), (b.x, b.y)] for a, b in
                                 zip(xy.loc[pairs["src_rid"]], xy.loc[pairs["dst_rid"]], strict=True)])
    keys = [r.split(":", 2)[2] for r in pairs[key]]
    return pd.DataFrame({
        "rid": [make_rid(layer, cls, rid.split(":")[1], k) for rid, k in zip(pairs[key], keys, strict=True)],
        "layer": layer, "class": cls, "src_rid": pairs["src_rid"].to_numpy(), "dst_rid": pairs["dst_rid"].to_numpy(),
        "length_m": shapely.length(geoms), "confidence": confidence, "primary_source": source, "geometry": geoms,
    })
