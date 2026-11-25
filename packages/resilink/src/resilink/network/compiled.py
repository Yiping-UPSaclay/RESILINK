"""Compile release tables into integer-indexed sparse structures for fast cascade simulation."""

from __future__ import annotations

from dataclasses import dataclass, field

import geopandas as gpd
import numpy as np
import pandas as pd
import scipy.sparse as sp

from resilink.io.release import Release
from resilink.registry import SystemCatalog, system_catalog


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
    layer_names: tuple[str, ...] = ()  # systems present in the release, sorted; position = layer code

    @property
    def n(self) -> int:
        return len(self.rid)

    @property
    def sectors(self) -> tuple[str, ...]:
        """Systems that serve the population grid in this network, in system-id order."""
        return tuple(self.supply)

    def layer_code(self, system: str) -> int:
        """Internal integer code of a system in this network; -1 when the release has no such system."""
        return self.layer_names.index(system) if system in self.layer_names else -1

    @property
    def e(self) -> int:
        return len(self.edge_rid)

    def idx(self, rid: str) -> int:
        return self.index[rid]

    def full_adjacency(self) -> sp.csr_array:
        ones = np.ones(self.e, dtype=np.int8)
        a = sp.coo_array((ones, (self.edge_src, self.edge_dst)), shape=(self.n, self.n)).tocsr()
        return (a + a.T).tocsr()


def _repair_times(catalog: SystemCatalog, systems: pd.Series, classes: pd.Series) -> np.ndarray:
    pairs = list(zip(systems, classes, strict=True))
    lookup = {p: catalog.repair_median_h(*p) for p in set(pairs)}
    return np.array([lookup[p] for p in pairs], dtype=float)


def compile_network(release: Release, catalog: SystemCatalog | None = None) -> CompiledNetwork:
    """Compile a release; systems, classes, repair times and population suppliers come from the system packs."""
    catalog = catalog or system_catalog()
    nodes: gpd.GeoDataFrame = release.read("nodes").sort_values("k").reset_index(drop=True)
    edges: gpd.GeoDataFrame = release.read("edges").sort_values("k").reset_index(drop=True)
    grid: gpd.GeoDataFrame = release.read("grid_cells").sort_values("k").reset_index(drop=True)
    links: pd.DataFrame = release.read("service_links")
    problems = catalog.problems(nodes, edges)
    if problems:
        raise ValueError(f"release {release.release_id} does not match the installed system packs: "
                         + "; ".join(problems))

    # Layer codes are internal to this network: the systems present in the release, sorted, so that installing
    # another system pack never changes how an existing release compiles.
    layer_names = tuple(sorted(set(nodes["layer"])))
    layer_code = {name: i for i, name in enumerate(layer_names)}

    rid = nodes["rid"].to_numpy(dtype=object)
    index = {r: i for i, r in enumerate(rid)}
    layer = nodes["layer"].map(layer_code).to_numpy(dtype=np.int8)
    cls = nodes["class"].to_numpy(dtype=object)
    is_source = nodes["is_source"].fillna(False).to_numpy(dtype=bool)
    pts = nodes.geometry.representative_point()
    xy = np.column_stack([pts.x.to_numpy(), pts.y.to_numpy()])
    repair = _repair_times(catalog, nodes["layer"], nodes["class"])

    edge_rid = edges["rid"].to_numpy(dtype=object)
    edge_src = edges["src_rid"].map(index).to_numpy(dtype=np.int64)
    edge_dst = edges["dst_rid"].map(index).to_numpy(dtype=np.int64)
    edge_cls = edges["class"].to_numpy(dtype=object)
    edge_repair = _repair_times(catalog, edges["layer"], edges["class"])

    cell_id = grid["cell_id"].to_numpy(dtype=object)
    cell_index = {c: i for i, c in enumerate(cell_id)}
    supply: dict[str, sp.csr_array] = {}
    for sector, _supplier in catalog.population_suppliers(layer_names):
        sl = links[links["sector"] == sector]
        rows = sl["cell_id"].map(cell_index).to_numpy()
        cols = sl["supplier_rid"].map(index).to_numpy()
        supply[sector] = sp.coo_array(
            (sl["share"].to_numpy(dtype=float), (rows, cols)), shape=(len(cell_id), len(rid))
        ).tocsr()

    has_src = np.zeros(len(layer_names), dtype=bool)
    has_src[np.unique(layer[is_source])] = True

    return CompiledNetwork(
        release_id=release.release_id, rid=rid, layer=layer, cls=cls, is_source=is_source, xy=xy,
        repair_median_h=repair, edge_rid=edge_rid, edge_src=edge_src, edge_dst=edge_dst, edge_cls=edge_cls,
        edge_repair_median_h=edge_repair, cell_id=cell_id, cell_pop=grid["pop"].to_numpy(dtype=float),
        cell_commune=grid["admin_commune"].to_numpy(dtype=object), supply=supply, layer_has_source=has_src,
        index=index, edge_index={r: i for i, r in enumerate(edge_rid)}, layer_names=layer_names,
    )
