"""Synthetic 8 km x 8 km territory (EPSG:2154 around Marseille) with electricity, gas and telecom layers,
a 200 m population lattice, four communes and a three-scenario river flood (RP10 / RP100 / RP1000).

Everything is deterministic (fixed seed), so golden evals can compute ground truth with the same ops the
agents use. Structure is deliberately hand-checkable:

* elec: T1 (RTE-like source) -> P1, P2, P3 (primary substations) -> radial MV feeders of MV/LV substations,
  one tie line between the P2 and P3 feeders, gas-fired generator G1 (second source) on P3.
* gas: LNG terminal L1 (source) -> pipeline L1-J1-C1(compressor)-J2-J3-J4 with delivery stations D1..D4
  and a spur J1-D5 that fuels G1.
* telecom: exchange X1 (source) -> aggregation A1, A2 -> 16 mobile sites.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, Point, box

from resilink.domain.ids import make_rid, point_fingerprint
from resilink.domain.vocab import SECTOR_SUPPLIER_CLASS
from resilink.io.release import Release, load_release, publish_release

SYNTHETIC_RELEASE_ID = "synthetic-2026.11.0"
CRS = "EPSG:2154"
X0, Y0 = 888_000.0, 6_240_000.0  # south-west corner (Lambert-93), Marseille area
SIZE = 8_000.0
CELL = 200.0
SEED = 20261116

RIVER_COORDS = [(0, 3600), (1500, 3300), (3000, 3500), (4500, 3100), (6000, 3300), (8000, 2900)]
FLOOD_SCENARIOS = [  # scenario_id, return period (years), half-width (m), max depth in core band (m)
    ("flood_rp10", 10, 250.0, 0.6),
    ("flood_rp100", 100, 550.0, 1.4),
    ("flood_rp1000", 1000, 900.0, 2.6),
]


def _pt(x: float, y: float) -> Point:
    return Point(X0 + x, Y0 + y)


def _node(rows: list[dict], layer: str, cls: str, key: str | None, x: float, y: float, *, name: str,
          is_source: bool = False, voltage_kv: float | None = None, ns: str = "syn") -> str:
    p = _pt(x, y)
    if key is None:
        ns, key = "fp", point_fingerprint(p.x, p.y)
    rid = make_rid(layer, cls, ns, key)
    rows.append({
        "rid": rid, "layer": layer, "class": cls, "name": name, "operator": "synthetic",
        "status": "in_service", "is_source": is_source, "voltage_kv": voltage_kv,
        "pos_uncertainty_m": 1.0, "confidence": 1.0, "primary_source": "synthetic",
        "sensitivity": "internal", "geometry": p,
    })
    return rid


def _edge(rows: list[dict], nodes: dict[str, Point], layer: str, cls: str, a: str, b: str, *, name: str,
          voltage_kv: float | None = None) -> None:
    line = LineString([nodes[a], nodes[b]])
    key = f"{a.split(':')[-1]}-{b.split(':')[-1]}".replace("_", "")
    rows.append({
        "rid": make_rid(layer, cls, "syn", key), "layer": layer, "class": cls, "name": name,
        "src_rid": a, "dst_rid": b, "directed": False, "length_m": float(line.length),
        "aerial_share": 1.0 if cls in ("line_hv", "line_mv_aerial") else 0.0, "voltage_kv": voltage_kv,
        "confidence": 1.0, "primary_source": "synthetic", "sensitivity": "internal", "geometry": line,
    })


def build_synthetic_tables() -> dict[str, tuple[gpd.GeoDataFrame | pd.DataFrame, str]]:
    rng = np.random.default_rng(SEED)
    n: list[dict] = []
    e: list[dict] = []

    # --- electricity -------------------------------------------------------------------------------
    t1 = _node(n, "elec", "sub_tx", "T1", 4000, 7500, name="T1 (400/225 kV)", is_source=True, voltage_kv=225)
    primaries = {
        "P1": _node(n, "elec", "sub_primary", "P1", 1500, 5200, name="Poste source P1", voltage_kv=63),
        "P2": _node(n, "elec", "sub_primary", "P2", 4000, 4300, name="Poste source P2", voltage_kv=63),
        "P3": _node(n, "elec", "sub_primary", "P3", 6500, 5200, name="Poste source P3", voltage_kv=63),
    }
    g1 = _node(n, "elec", "generator", "G1", 7200, 6100, name="CCGT G1 (gas-fired)", is_source=True, voltage_kv=63)
    feeders: dict[str, list[list[str]]] = {}
    for pname in primaries:
        px, py = {"P1": (1500, 5200), "P2": (4000, 4300), "P3": (6500, 5200)}[pname]
        branches = []
        for b, dx in enumerate((-650, 650)):
            chain = []
            for s in range(4):
                x = px + dx * (1 + 0.35 * s) + rng.normal(0, 40)
                y = py - 700 * (s + 1) + rng.normal(0, 40)
                chain.append(_node(n, "elec", "sub_mv_lv", None, x, y, name=f"{pname}-F{b + 1}-S{s + 1}", voltage_kv=20))
            branches.append(chain)
        feeders[pname] = branches

    # --- gas -----------------------------------------------------------------------------------------
    l1 = _node(n, "gas", "lng_terminal", "L1", 7600, 600, name="LNG terminal L1", is_source=True)
    j = {k: _node(n, "gas", "junction", k, x, y, name=f"Junction {k}")
         for k, (x, y) in {"J1": (6500, 1400), "J2": (4500, 1900), "J3": (2500, 2400), "J4": (1000, 2800)}.items()}
    c1 = _node(n, "gas", "compressor", "C1", 5500, 1650, name="Compressor C1")
    d = {k: _node(n, "gas", "delivery_station", k, x, y, name=f"Delivery station {k}")
         for k, (x, y) in {"D1": (6900, 2300), "D2": (4300, 2700), "D3": (2300, 3150), "D4": (900, 3500),
                           "D5": (7000, 5600)}.items()}

    # --- telecom -------------------------------------------------------------------------------------
    x1 = _node(n, "telecom", "exchange", "X1", 4100, 6200, name="Exchange X1", is_source=True)
    aggs = {"A1": _node(n, "telecom", "aggregation_logical", "A1", 2200, 4800, name="Aggregation A1"),
            "A2": _node(n, "telecom", "aggregation_logical", "A2", 5800, 4800, name="Aggregation A2")}
    sites = []
    for ix in range(4):
        for iy in range(4):
            x = 1000 + 2000 * ix + rng.normal(0, 150)
            y = 1000 + 2000 * iy + rng.normal(0, 150)
            sites.append(_node(n, "telecom", "mobile_site", f"S{ix}{iy}", x, y, name=f"Mobile site S{ix}{iy}"))

    nodes = gpd.GeoDataFrame(n, geometry="geometry", crs=CRS)
    pts = dict(zip(nodes["rid"], nodes.geometry, strict=True))

    # --- edges ---------------------------------------------------------------------------------------
    for rid in primaries.values():
        _edge(e, pts, "elec", "line_hv", t1, rid, name=f"HV {t1[-2:]}-{rid[-2:]}", voltage_kv=63)
    _edge(e, pts, "elec", "line_hv", primaries["P3"], g1, name="HV P3-G1", voltage_kv=63)
    for pname, branches in feeders.items():
        for chain in branches:
            prev = primaries[pname]
            for s, rid in enumerate(chain):
                _edge(e, pts, "elec", "line_mv_aerial" if s % 2 == 0 else "cable_mv", prev, rid,
                      name=f"MV {pname}", voltage_kv=20)
                prev = rid
    _edge(e, pts, "elec", "cable_mv", feeders["P2"][1][-1], feeders["P3"][0][-1], name="MV tie P2-P3", voltage_kv=20)
    for a, b in [(l1, j["J1"]), (j["J1"], c1), (c1, j["J2"]), (j["J2"], j["J3"]), (j["J3"], j["J4"]),
                 (j["J1"], d["D1"]), (j["J2"], d["D2"]), (j["J3"], d["D3"]), (j["J4"], d["D4"]), (j["J1"], d["D5"])]:
        _edge(e, pts, "gas", "pipeline_tx", a, b, name="Pipeline")
    for agg in aggs.values():
        _edge(e, pts, "telecom", "backhaul_logical", x1, agg, name="Backhaul core")
    agg_xy = {k: pts[v] for k, v in aggs.items()}
    for s in sites:
        nearest = min(aggs, key=lambda k: pts[s].distance(agg_xy[k]))
        _edge(e, pts, "telecom", "backhaul_logical", aggs[nearest], s, name="Backhaul access")
    edges = gpd.GeoDataFrame(e, geometry="geometry", crs=CRS)

    # --- admin areas (4 communes = quadrants) -------------------------------------------------------
    communes = []
    for q, (qx, qy) in enumerate([(0, 0), (1, 0), (0, 1), (1, 1)]):
        code = f"SYN00{q + 1}"
        communes.append({
            "admin_id": f"admin.commune:synthetic:{code}", "level": "commune", "code": code,
            "name": f"Commune {code}", "parent_id": "admin.departement:synthetic:SYN", "cog_year": 2025,
            "geometry": box(X0 + qx * SIZE / 2, Y0 + qy * SIZE / 2, X0 + (qx + 1) * SIZE / 2, Y0 + (qy + 1) * SIZE / 2),
        })
    admin = gpd.GeoDataFrame(communes, geometry="geometry", crs=CRS)

    # --- 200 m population lattice -------------------------------------------------------------------
    ncell = int(SIZE // CELL)
    ix, iy = np.meshgrid(np.arange(ncell), np.arange(ncell), indexing="ij")
    ix, iy = ix.ravel(), iy.ravel()
    cx, cy = (ix + 0.5) * CELL, (iy + 0.5) * CELL
    r2 = (cx - 4200) ** 2 + (cy - 5000) ** 2
    pop = 260.0 * np.exp(-r2 / (2 * 1800.0**2)) + rng.gamma(2.0, 6.0, size=ix.size)
    pop = np.round(pop, 1)
    quadrant = (cx >= SIZE / 2).astype(int) + 2 * (cy >= SIZE / 2).astype(int)
    grid = gpd.GeoDataFrame({
        "cell_id": [f"SYN200mN{b:03d}E{a:03d}" for a, b in zip(ix, iy, strict=True)],
        "pop": pop.astype(float),
        "admin_commune": [communes[q]["code"] for q in quadrant],
        "geometry": [box(X0 + a * CELL, Y0 + b * CELL, X0 + (a + 1) * CELL, Y0 + (b + 1) * CELL)
                     for a, b in zip(ix, iy, strict=True)],
    }, geometry="geometry", crs=CRS)
    grid.insert(1, "k", np.arange(len(grid), dtype=np.uint32))

    # --- service links (cell -> supplier) ----------------------------------------------------------
    centroids = np.column_stack([X0 + cx, Y0 + cy])
    links = []
    for sector, (layer, cls) in SECTOR_SUPPLIER_CLASS.items():
        sup = nodes[(nodes["layer"] == layer) & (nodes["class"] == cls)]
        sxy = np.column_stack([sup.geometry.x, sup.geometry.y])
        dist = np.sqrt(((centroids[:, None, :] - sxy[None, :, :]) ** 2).sum(-1))
        if sector == "telecom":  # two serving sites within 2.5 km (redundant coverage), shares 0.5 / 0.5
            order = np.argsort(dist, axis=1)[:, :2]
            for c in range(len(grid)):
                ok = [o for o in order[c] if dist[c, o] <= 2500.0] or [order[c, 0]]
                for o in ok:
                    links.append((grid["cell_id"].iat[c], sup["rid"].iat[o], sector, 1.0 / len(ok)))
        elif sector == "gas":  # gas distribution only within 3 km of a delivery station
            nearest = dist.argmin(axis=1)
            for c in range(len(grid)):
                if dist[c, nearest[c]] <= 3000.0:
                    links.append((grid["cell_id"].iat[c], sup["rid"].iat[nearest[c]], sector, 1.0))
        else:
            nearest = dist.argmin(axis=1)
            for c in range(len(grid)):
                links.append((grid["cell_id"].iat[c], sup["rid"].iat[nearest[c]], sector, 1.0))
    service_links = pd.DataFrame(links, columns=["cell_id", "supplier_rid", "sector", "share"])

    # --- flood scenarios (two depth bands per scenario) --------------------------------------------
    river = LineString([(X0 + x, Y0 + y) for x, y in RIVER_COORDS])
    scen_rows, foot_rows = [], []
    for sid, rp, half_width, depth in FLOOD_SCENARIOS:
        scen_rows.append({"scenario_id": sid, "hazard": "flood", "set_id": "synthetic_river", "return_period_y": float(rp),
                          "annual_rate": 1.0 / rp, "im_type": "depth_m", "im_unit": "m",
                          "generator": "synthetic.buffer_bands@1", "sensitivity": "public"})
        core = river.buffer(half_width / 2.0)
        outer = river.buffer(half_width).difference(core)
        foot_rows.append({"scenario_id": sid, "band": "core", "im_min": depth / 2, "im_max": depth, "geometry": core})
        foot_rows.append({"scenario_id": sid, "band": "margin", "im_min": 0.0, "im_max": depth / 2, "geometry": outer})
    scenarios = pd.DataFrame(scen_rows)
    footprints = gpd.GeoDataFrame(foot_rows, geometry="geometry", crs=CRS)

    # --- dense indices -------------------------------------------------------------------------------
    nodes = nodes.sort_values("rid").reset_index(drop=True)
    nodes.insert(1, "k", np.arange(len(nodes), dtype=np.uint32))
    edges = edges.sort_values("rid").reset_index(drop=True)
    edges.insert(1, "k", np.arange(len(edges), dtype=np.uint32))

    return {
        "nodes": (nodes, "internal"),
        "edges": (edges, "internal"),
        "admin_areas": (admin, "public"),
        "grid_cells": (grid, "public"),
        "service_links": (service_links, "internal"),
        "hazard_scenarios": (scenarios, "public"),
        "hazard_footprints": (footprints, "public"),
    }


def ensure_synthetic_release(root: Path | None = None) -> Release:
    """Load the synthetic release, building it on first use (deterministic, so rebuilds are byte-identical)."""
    try:
        return load_release(SYNTHETIC_RELEASE_ID, root)
    except FileNotFoundError:
        return publish_release(
            SYNTHETIC_RELEASE_ID,
            build_synthetic_tables(),
            territory={"id": "synthetic", "name": "Synthetic 8 km x 8 km test territory", "crs_metric": CRS},
            snapshots=[{"id": "synthetic@deterministic", "licence": "CC0 (generated)"}],
            qa={"status": "pass", "note": "synthetic fixture"},
            licences={"all": "CC0-1.0 (synthetic)"},
            root=root,
        )
