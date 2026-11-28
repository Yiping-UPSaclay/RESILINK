"""Release build (blueprint §5.1 "build", roadmap D6): staged tables of a territory -> one immutable release.

The build is deterministic: the same staged tables give byte-identical release tables. Steps:

1. Build each system's network (``elec``, ``gas``, ``telecom``): entity matching, noding, contraction.
2. Keep what is connected to a source with no damage. A fragment that open data leaves cut off (a substation
   whose line is missing) would be out of service in every simulated world and inflate every loss, so it is
   dropped and counted in the QA report.
3. Give dense indices (``k``, ordered by rid) and validate every table: schemas, references, system packs.
4. Link the core's 200 m cells to their suppliers (``service``), then evaluate the QA gates (``qa``).

``publish`` writes the release only when every gate passes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.sparse import coo_array
from scipy.sparse.csgraph import connected_components

from resilink.domain.config import TerritoryConfig
from resilink.domain.schemas import TABLE_SCHEMAS
from resilink.etl.release import qa as qa_mod
from resilink.etl.release.elec import SystemBuild, build_elec
from resilink.etl.release.frames import Extent, Staged
from resilink.etl.release.gas import build_gas
from resilink.etl.release.service import service_links
from resilink.etl.release.telecom import build_telecom
from resilink.etl.staging import staging_dir
from resilink.io.release import Release, publish_release
from resilink.registry import SystemCatalog, system_catalog

Tables = dict[str, tuple[gpd.GeoDataFrame | pd.DataFrame, str]]
COPIED = {"admin_areas": "public", "territory_extent": "public", "hazard_scenarios": "public",
          "hazard_footprints": "public"}


@dataclass(frozen=True)
class ReleaseBuild:
    release_id: str
    tables: Tables
    qa: dict[str, Any]
    snapshots: list[dict[str, Any]]
    licences: dict[str, str]
    territory: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.qa.get("status") == "pass"


def _plain(obj: Any) -> Any:
    """JSON-ready copy (numpy scalars and arrays become Python numbers and lists) for the manifest."""
    def default(o: Any) -> Any:
        if isinstance(o, np.generic):
            return o.item()
        if isinstance(o, np.ndarray):
            return o.tolist()
        raise TypeError(f"not JSON serialisable: {type(o).__name__}")
    return json.loads(json.dumps(obj, default=default))


def connected_to_source(nodes: pd.DataFrame, edges: pd.DataFrame) -> np.ndarray:
    """Per node: in a component (over all edges) that holds a source of its own system."""
    index = pd.Series(np.arange(len(nodes)), index=nodes["rid"].to_numpy())
    src, dst = index.loc[edges["src_rid"]].to_numpy(), index.loc[edges["dst_rid"]].to_numpy()
    n = len(nodes)
    _, lab = connected_components(coo_array((np.ones(len(src)), (src, dst)), shape=(n, n)), directed=False)
    layer = nodes["layer"].to_numpy()
    ok = np.zeros(n, dtype=bool)
    for system in np.unique(layer):
        mine = layer == system
        fed = np.unique(lab[mine & nodes["is_source"].to_numpy(dtype=bool)])
        ok |= mine & np.isin(lab, fed)
    return ok


def _check_references(nodes: pd.DataFrame, edges: pd.DataFrame) -> None:
    if nodes["rid"].duplicated().any():
        raise ValueError(f"duplicate node rids: {nodes.loc[nodes['rid'].duplicated(), 'rid'].head().tolist()}")
    if edges["rid"].duplicated().any():
        raise ValueError(f"duplicate edge rids: {edges.loc[edges['rid'].duplicated(), 'rid'].head().tolist()}")
    known = set(nodes["rid"])
    dangling = edges[~edges["src_rid"].isin(known) | ~edges["dst_rid"].isin(known)]
    if len(dangling):
        raise ValueError(f"{len(dangling)} edges reference unknown nodes, e.g. {dangling['rid'].head(3).tolist()}")


def _dense(frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    frame = frame.sort_values("rid").reset_index(drop=True)
    frame.insert(1, "k", np.arange(len(frame), dtype=np.uint32))
    return frame


def build_release(territory: TerritoryConfig, version: str, *, root: Path | None = None,
                  catalog: SystemCatalog | None = None) -> ReleaseBuild:
    catalog = catalog or system_catalog()
    st = Staged(staging_dir(territory.id, root))
    ext = st.read("territory_extent").set_index("kind")
    extent = Extent(core=ext.geometry["core"], buffer=ext.geometry["buffer"])
    systems: dict[str, SystemBuild] = {"elec": build_elec(st, extent), "gas": build_gas(st, extent),
                                       "telecom": build_telecom(st, extent)}
    nodes = pd.concat([s.nodes for s in systems.values()], ignore_index=True)
    edges = pd.concat([s.edges for s in systems.values()], ignore_index=True)
    _check_references(nodes, edges)

    ok = connected_to_source(nodes, edges)
    dropped = nodes[~ok].groupby(["layer", "class"]).size()
    nodes = nodes[ok]
    kept = set(nodes["rid"])
    edge_ok = edges["src_rid"].isin(kept) & edges["dst_rid"].isin(kept)
    dropped_edges = edges[~edge_ok]
    edges = edges[edge_ok]
    nodes = _dense(gpd.GeoDataFrame(nodes, geometry="geometry", crs="EPSG:2154"))
    edges = _dense(gpd.GeoDataFrame(edges, geometry="geometry", crs="EPSG:2154"))
    problems = catalog.problems(nodes, edges)
    if problems:
        raise ValueError("release does not match the installed system packs: " + "; ".join(problems))

    grid = st.read("grid_cells")
    grid = grid[grid["in_core"]].sort_values("cell_id").reset_index(drop=True)
    grid["k"] = np.arange(len(grid), dtype=np.uint32)
    links, qa_links = service_links(grid, nodes, st.read("stg_gas_distribution_lines"), catalog)
    xref = pd.concat([s.xref for s in systems.values()], ignore_index=True)
    xref = xref[xref["rid"].isin(kept)].sort_values(["rid", "source_id", "source_key"]).reset_index(drop=True)

    tables: Tables = {"nodes": (nodes, "internal"), "edges": (edges, "internal"), "grid_cells": (grid, "public"),
                      "service_links": (links, "internal"), "entity_xref": (xref, "internal")}
    for table, tier in COPIED.items():
        tables[table] = (st.read(table), tier)
    for table, (frame, _) in tables.items():
        schema = TABLE_SCHEMAS.get(table)
        if schema is not None:
            schema.validate(frame.drop(columns=["bbox"], errors="ignore"))

    lost_km = dropped_edges.assign(km=dropped_edges["length_m"] / 1000).groupby(["layer", "class"])["km"].sum()
    report: dict[str, Any] = {name: s.qa for name, s in systems.items()}
    report["baseline"] = {
        "nodes_dropped": {f"{a}.{b}": int(n) for (a, b), n in dropped.items()},
        "core_nodes_dropped": {f"{a}.{b}": int(n) for (a, b), n in
                               systems_core_dropped(systems, kept).items()},
        "core_key_assets_dropped": sorted(set(dropped_rids(systems, kept)) & core_key_assets(systems)),
        "edges_dropped_km": {f"{a}.{b}": round(float(km), 1) for (a, b), km in lost_km.items()},
        "linked_suppliers_served_share": round(float(links["supplier_rid"].isin(kept).mean()), 4),
    }
    report["service_links"] = qa_links
    report["dependency_candidates"] = qa_mod.dependency_candidates(nodes)
    report["counts"] = {
        "nodes_by_class": {f"{a}.{b}": int(n) for (a, b), n in nodes.groupby(["layer", "class"]).size().items()},
        "core_edges_km_by_class": {f"{a}.{b}": round(float(km), 1) for (a, b), km in
                                   edges[edges["in_core"]].assign(km=lambda d: d["length_m"] / 1000)
                                   .groupby(["layer", "class"])["km"].sum().items()},
        "cells": len(grid), "population": round(float(grid["pop"].sum()), 1)}
    insee = st.record("grid_cells")["qa"].get("insee_pop_core_by_dominant_commune")
    gates = qa_mod.evaluate(report, nodes, links, grid, insee)
    qa = _plain({"status": "pass" if all(g.passed for g in gates) else "fail", "gates": [g.row() for g in gates],
                 "report": report})

    snapshots: dict[str, dict[str, Any]] = {}
    licences: dict[str, str] = {}
    for table in sorted(st.used):
        rec = st.record(table)
        licences.update(rec["licences"])
        for snap in rec["snapshots"]:
            entry = snapshots.setdefault(snap, {"id": snap, "source": snap.split("@", 1)[0], "staged_tables": []})
            entry["staged_tables"].append(table)
    for entry in snapshots.values():
        entry["licence"] = licences.get(entry["source"], "")
    meta = {"id": territory.id, "name": territory.name, "crs_metric": territory.crs_metric,
            "buffer_m": territory.buffer_m, "selector": territory.selector,
            "grid": "inhabited 200 m cells of the core (INSEE Filosofi 2021)",
            "networks": "core and buffer; cut at the buffer, where the cut points are sources"}
    return ReleaseBuild(release_id=f"{territory.id}-{version}", tables=tables, qa=qa,
                        snapshots=sorted(snapshots.values(), key=lambda s: s["id"]), licences=dict(sorted(licences.items())),
                        territory=meta)


KEY_CLASSES = {"sub_tx", "sub_primary", "generator", "lng_terminal", "compressor", "storage", "exchange"}


def dropped_rids(systems: dict[str, SystemBuild], kept: set[str]) -> list[str]:
    return [r for s in systems.values() for r in s.nodes["rid"] if r not in kept]


def core_key_assets(systems: dict[str, SystemBuild]) -> set[str]:
    return {r for s in systems.values() for r, c, core in zip(s.nodes["rid"], s.nodes["class"], s.nodes["in_core"],
                                                              strict=True) if core and c in KEY_CLASSES}


def systems_core_dropped(systems: dict[str, SystemBuild], kept: set[str]) -> pd.Series:
    allnodes = pd.concat([s.nodes for s in systems.values()], ignore_index=True)
    lost = allnodes[allnodes["in_core"] & ~allnodes["rid"].isin(kept)]
    return lost.groupby(["layer", "class"]).size()


def publish(build: ReleaseBuild, *, root: Path | None = None) -> Release:
    """Write the release (immutable). Refuses when a QA gate failed."""
    if not build.passed:
        failed = [g["id"] for g in build.qa["gates"] if not g["pass"]]
        raise ValueError(f"release {build.release_id} not published: QA gates failed: {', '.join(failed)}")
    return publish_release(build.release_id, build.tables, territory=build.territory, snapshots=build.snapshots,
                           qa=build.qa, licences=build.licences, root=root)
