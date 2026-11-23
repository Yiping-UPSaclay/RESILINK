"""Compile release tables into integer-indexed sparse structures for fast cascade simulation."""

from __future__ import annotations

from dataclasses import dataclass, field

import geopandas as gpd
import numpy as np
import pandas as pd
import scipy.sparse as sp
from scipy.sparse import csgraph

from resilink.domain.vocab import LAYER_CODE, REPAIR_MEDIAN_H, SECTORS
from resilink.io.release import Release


@dataclass(frozen=True)
class CompiledNetwork:
    release_id: str
    rid: np.ndarray  # (N,) object
    layer: np.ndarray  # (N,) int8
    cls: np.ndarray  # (N,) object
    is_source: np.ndarray  # (N,) bool
    xy: np.ndarray  # (N, 2) float — only for dependency inference / explanation, never used by the cascade
    repair_median_h: np.ndarray  # (N,)
    edge_rid: np.ndarray  # (E,)
    edge_src: np.ndarray  # (E,) int
    edge_dst: np.ndarray  # (E,) int
    edge_cls: np.ndarray  # (E,)
    edge_repair_median_h: np.ndarray  # (E,)
    cell_id: np.ndarray  # (C,)
    cell_pop: np.ndarray  # (C,)
    cell_commune: np.ndarray  # (C,)
    supply: dict[str, sp.csr_array]  # sector -> (C x N) supply shares
    layer_has_source: np.ndarray  # (n_layers,) bool
    index: dict[str, int] = field(repr=False, default_factory=dict)
    edge_index: dict[str, int] = field(repr=False, default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.rid)

    @property
    def e(self) -> int:
        return len(self.edge_rid)

    def idx(self, rid: str) -> int:
        return self.index[rid]

    def full_adjacency(self) -> sp.csr_array:
        ones = np.ones(self.e, dtype=np.int8)
        a = sp.coo_array((ones, (self.edge_src, self.edge_dst)), shape=(self.n, self.n)).tocsr()
        return (a + a.T).tocsr()


def compile_network(release: Release) -> CompiledNetwork:
    nodes: gpd.GeoDataFrame = release.read("nodes").sort_values("k").reset_index(drop=True)
    edges: gpd.GeoDataFrame = release.read("edges").sort_values("k").reset_index(drop=True)
    grid: gpd.GeoDataFrame = release.read("grid_cells").sort_values("k").reset_index(drop=True)
    links: pd.DataFrame = release.read("service_links")

    rid = nodes["rid"].to_numpy(dtype=object)
    index = {r: i for i, r in enumerate(rid)}
    layer = nodes["layer"].map(LAYER_CODE).to_numpy(dtype=np.int8)
    cls = nodes["class"].to_numpy(dtype=object)
    is_source = nodes["is_source"].fillna(False).to_numpy(dtype=bool)
    pts = nodes.geometry.representative_point()
    xy = np.column_stack([pts.x.to_numpy(), pts.y.to_numpy()])
    repair = np.array([REPAIR_MEDIAN_H.get(c, 24.0) for c in cls], dtype=float)

    edge_rid = edges["rid"].to_numpy(dtype=object)
    edge_src = edges["src_rid"].map(index).to_numpy(dtype=np.int64)
    edge_dst = edges["dst_rid"].map(index).to_numpy(dtype=np.int64)
    edge_cls = edges["class"].to_numpy(dtype=object)
    edge_repair = np.array([REPAIR_MEDIAN_H.get(c, 24.0) for c in edge_cls], dtype=float)

    cell_id = grid["cell_id"].to_numpy(dtype=object)
    cell_index = {c: i for i, c in enumerate(cell_id)}
    supply: dict[str, sp.csr_array] = {}
    for sector in SECTORS:
        sl = links[links["sector"] == sector]
        rows = sl["cell_id"].map(cell_index).to_numpy()
        cols = sl["supplier_rid"].map(index).to_numpy()
        supply[sector] = sp.coo_array(
            (sl["share"].to_numpy(dtype=float), (rows, cols)), shape=(len(cell_id), len(rid))
        ).tocsr()

    n_layers = int(layer.max()) + 1 if len(layer) else 0
    has_src = np.zeros(max(n_layers, len(LAYER_CODE)), dtype=bool)
    has_src[np.unique(layer[is_source])] = True

    return CompiledNetwork(
        release_id=release.release_id, rid=rid, layer=layer, cls=cls, is_source=is_source, xy=xy,
        repair_median_h=repair, edge_rid=edge_rid, edge_src=edge_src, edge_dst=edge_dst, edge_cls=edge_cls,
        edge_repair_median_h=edge_repair, cell_id=cell_id, cell_pop=grid["pop"].to_numpy(dtype=float),
        cell_commune=grid["admin_commune"].to_numpy(dtype=object), supply=supply, layer_has_source=has_src,
        index=index, edge_index={r: i for i, r in enumerate(edge_rid)},
    )


def served(net: CompiledNetwork, node_ok: np.ndarray, edge_ok: np.ndarray) -> np.ndarray:
    """Intra-layer service: a node is served if it is ok and its component (over ok nodes and ok edges)
    contains an ok source of its layer. Layers without any source are served whenever the node is ok."""
    m = edge_ok & node_ok[net.edge_src] & node_ok[net.edge_dst]
    ones = np.ones(int(m.sum()), dtype=np.int8)
    a = sp.coo_array((ones, (net.edge_src[m], net.edge_dst[m])), shape=(net.n, net.n)).tocsr()
    _, lab = csgraph.connected_components(a, directed=False)
    has = np.zeros(lab.max() + 1, dtype=bool)
    has[lab[net.is_source & node_ok]] = True
    out = has[lab] & node_ok
    no_src_layer = ~net.layer_has_source[net.layer]
    out[no_src_layer] = node_ok[no_src_layer]
    return out
