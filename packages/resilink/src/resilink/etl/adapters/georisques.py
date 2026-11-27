"""Flood hazard (D5): Floods Directive TRI mapping, 2020 reporting (Géorisques, COVADIS DI v2 shapefiles).

For each TRI (Territoire à Risque important d'Inondation) and each flood type and scenario, the archives give flood
extents (``inondable``) and water-depth class polygons (``iso_ht``: 0-0.5, 0.5-1, 1-2 and over 2 m). The depth
polygons become the release's ``hazard_footprints`` (``im_max`` = upper bound of the class) and each (type, scenario)
becomes a ``hazard_scenarios`` row. Checked 2026-11-27:
* a TRI spanning several départements is copied whole into each département's archive (same ids and geometries), so
  each TRI layer is read once;
* the over-2 m class is coded ht_max = 9999; it is given a finite depth (assumption A-HAZ-01);
* scenario return periods follow the layer titles: frequent 10 y, medium 100 y, rare 1000 y (assumption A-HAZ-02);
  the medium scenario with climate change forms its own set;
* outside TRI perimeters, flooding is not mapped (assumption A-HAZ-03): the QA reports the share of the territory
  that TRIs cover;
* some layers hold invalid polygons (about a thousand per depth layer): they are repaired, and counted;
* depth classes can be missing over part of an extent (Avignon TRI, medium scenario: three quarters of its extent in
  the buffer). Where they cover less than 95 % of the extent, the rest becomes an "unknown" band at the lowest class
  bound, a lower bound (assumption A-HAZ-04);
* depth polygons of one scenario overlap (floodplains of several rivers): footprints keep them; the maximum depth
  at an asset is unaffected, but summed exposed line lengths must be taken over their union.
"""

from __future__ import annotations

import re
import zipfile

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely

from resilink.etl.staging import AdapterContext, Output, adapter

SOURCES = tuple(f"georisques.tri2020_{d}" for d in ("13", "30", "34", "83", "84"))
LAYER = re.compile(r"/(FRD_TRI_[A-Z0-9_]+)/n_tri_[a-z0-9_]+?_(inondable|iso_ht)_(\d\d)_(\d\d[a-z]+)_s_\d{3}\.shp$", re.I)
PERIMETER = re.compile(r"/(FRD_TRI_[A-Z0-9_]+)/n_tri_[a-z0-9_]+?_tri_s_\d{3}\.shp$", re.I)
FLOOD_TYPES = {"01": "river", "02": "runoff", "03": "sea"}
SCENARIOS = {"01for": (10.0, ""), "02moy": (100.0, ""), "03mcc": (100.0, "cc"), "04fai": (1000.0, "")}  # A-HAZ-02
OPEN_CLASS_DEPTH_M = 3.0  # A-HAZ-01: depth given to the "over 2 m" class (ht_max = 9999)
UNKNOWN_DEPTH_M = 0.5  # A-HAZ-04: depth given to flooded areas without a depth class (lowest class bound)
MIN_DEPTH_COVERAGE = 0.95
GENERATOR = "georisques.tri2020@1.0.0"


def _valid(gdf: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, int]:
    bad = ~gdf.geometry.is_valid
    if bad.any():
        gdf = gdf.copy()
        gdf.loc[bad, "geometry"] = shapely.make_valid(gdf.geometry[bad].to_numpy(), method="structure",
                                                      keep_collapsed=False)
    return gdf, int(bad.sum())


def _clipped_area(geoms: np.ndarray, area: shapely.Geometry) -> float:
    """Area of the polygons inside ``area`` (prepared), clipping only those that cross its boundary."""
    inside = shapely.contains(area, geoms)
    crossing = geoms[~inside]
    return float(shapely.area(geoms[inside]).sum() + shapely.area(shapely.intersection(crossing, area)).sum())


def scenario_id(flood_type: str, code: str) -> str:
    rp, suffix = SCENARIOS[code]
    return f"tri2020_{FLOOD_TYPES[flood_type]}_rp{int(rp)}{suffix}"


def _layers(ctx: AdapterContext) -> tuple[dict[tuple[str, str, str, str], str], dict[str, str], int]:
    """First archive path of each (TRI, kind, type, scenario) layer and of each TRI perimeter; copies skipped."""
    found: dict[tuple[str, str, str, str], str] = {}
    perimeters: dict[str, str] = {}
    copies = 0
    for sid in SOURCES:
        zpath = ctx.path(sid)
        for name in sorted(zipfile.ZipFile(zpath).namelist()):
            m, p = LAYER.search("/" + name), PERIMETER.search("/" + name)
            if m:
                key = (m.group(1).upper(), m.group(2).lower(), m.group(3), m.group(4).lower())
                if key in found:
                    copies += 1
                else:
                    found[key] = f"/vsizip/{zpath}/{name}"
            elif p and p.group(1).upper() not in perimeters:
                perimeters[p.group(1).upper()] = f"/vsizip/{zpath}/{name}"
    return found, perimeters, copies


def _read(path: str, window: tuple[float, float, float, float], crs: str) -> gpd.GeoDataFrame:
    gdf = pyogrio.read_dataframe(path, bbox=window)
    if gdf.crs is None or gdf.crs.to_epsg() != int(crs.split(":")[-1]):
        raise ValueError(f"{path}: CRS {gdf.crs} is not {crs}")
    return gdf.set_crs(crs, allow_override=True)


@adapter("georisques.tri2020", version="1.0.0", sources=SOURCES, needs=("territory_extent",),
         produces=("hazard_scenarios", "hazard_footprints", "stg_hazard_tri_perimeters"))
def tri2020(ctx: AdapterContext) -> dict[str, Output]:
    """Flood scenarios and water-depth footprints of the TRIs meeting the territory buffer."""
    t = ctx.territory
    extent = ctx.read("territory_extent").set_index("kind")
    core, grown = extent.geometry["core"], extent.geometry["buffer"]
    shapely.prepare(grown)
    window = tuple(float(v) for v in grown.bounds)
    layers, perimeter_paths, copies = _layers(ctx)

    def read_layer(path: str) -> tuple[gpd.GeoDataFrame, int]:
        gdf = _read(path, window, t.crs_metric)
        gdf, fixed = _valid(gdf[gdf.geometry.notna()])
        return gdf[~gdf.geometry.is_empty & shapely.intersects(gdf.geometry.to_numpy(), grown)], fixed

    parts, coverage, gaps, repaired = [], {}, {}, 0
    pairs = sorted({(tri, ftype, code) for (tri, kind, ftype, code) in layers if kind == "iso_ht"
                    and code in SCENARIOS and ftype in FLOOD_TYPES})
    for tri, ftype, code in pairs:
        dep, n1 = read_layer(layers[(tri, "iso_ht", ftype, code)])
        if not len(dep):
            continue
        ext, n2 = read_layer(layers[(tri, "inondable", ftype, code)]) if (tri, "inondable", ftype, code) in layers \
            else (dep.iloc[:0], 0)
        repaired += n1 + n2
        sid = scenario_id(ftype, code)
        ht_min = pd.to_numeric(dep["ht_min"], errors="coerce").astype(float)
        ht_max = pd.to_numeric(dep["ht_max"], errors="coerce").astype(float)
        open_ended = ht_max >= 999
        parts.append(gpd.GeoDataFrame({
            "scenario_id": sid, "tri": tri, "zone_id": dep["id"].astype(str).to_numpy(),
            "band": [f"{lo:g}+" if o else f"{lo:g}-{hi:g}" for lo, hi, o in zip(ht_min, ht_max, open_ended, strict=True)],
            "im_min": ht_min.to_numpy(), "im_max": np.where(open_ended, OPEN_CLASS_DEPTH_M, ht_max).astype(float),
            "depth_known": True, "depth_open_ended": open_ended.to_numpy(), "geometry": dep.geometry.to_numpy(),
        }, geometry="geometry", crs=t.crs_metric))
        if not len(ext):
            continue
        # coverage of the extent by depth classes, on areas clipped to the buffer (both layers overlap similarly)
        ratio = _clipped_area(dep.geometry.to_numpy(), grown) / _clipped_area(ext.geometry.to_numpy(), grown)
        coverage[f"{sid}|{tri}"] = round(ratio, 3)
        if ratio < MIN_DEPTH_COVERAGE:
            gap = shapely.difference(shapely.intersection(shapely.union_all(ext.geometry.to_numpy()), grown),
                                     shapely.union_all(dep.geometry.to_numpy()))
            polys = [g for g in shapely.get_parts(gap) if g.geom_type == "Polygon" and g.area >= 1.0]
            gaps[f"{sid}|{tri}"] = round(sum(g.area for g in polys) / 1e6, 1)
            parts.append(gpd.GeoDataFrame({
                "scenario_id": sid, "tri": tri, "zone_id": [f"gap-{i}" for i in range(len(polys))], "band": "unknown",
                "im_min": 0.0, "im_max": UNKNOWN_DEPTH_M, "depth_known": False, "depth_open_ended": False,
                "geometry": polys}, geometry="geometry", crs=t.crs_metric))
    foot = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), geometry="geometry", crs=t.crs_metric)
    foot = foot.sort_values(["scenario_id", "tri", "zone_id"]).reset_index(drop=True)
    if foot["im_min"].isna().any() or (foot["im_max"] < foot["im_min"]).any():
        raise ValueError("depth classes with missing or inverted bounds")

    rows = []
    for sid, grp in foot.groupby("scenario_id", sort=True):
        ftype = next(k for k, v in FLOOD_TYPES.items() if f"_{v}_" in sid)
        code = next(c for c in SCENARIOS if scenario_id(ftype, c) == sid)
        rp, cc = SCENARIOS[code]
        rows.append({"scenario_id": sid, "hazard": "flood", "set_id": f"tri2020_{FLOOD_TYPES[ftype]}" + ("_cc" if cc else ""),
                     "return_period_y": rp, "annual_rate": 1.0 / rp, "im_type": "depth_m", "im_unit": "m",
                     "generator": GENERATOR, "sensitivity": "public", "flood_type": ftype, "scenario_code": code,
                     "tris": ",".join(sorted(grp["tri"].unique()))})
    scenarios = pd.DataFrame(rows)

    per = []
    for tri, path in sorted(perimeter_paths.items()):
        gdf = _read(path, window, t.crs_metric)
        gdf = gdf[shapely.intersects(gdf.geometry.to_numpy(), grown)]
        if len(gdf):
            per.append(gpd.GeoDataFrame({
                "tri": tri, "name": gdf["nom"].fillna("").astype(str).to_numpy(),
                "inhabitants": pd.to_numeric(gdf["hab_perm"], errors="coerce").to_numpy(),
                "geometry": gdf.geometry.to_numpy()}, geometry="geometry", crs=t.crs_metric))
    perimeters = gpd.GeoDataFrame(pd.concat(per, ignore_index=True), geometry="geometry", crs=t.crs_metric)
    perimeters = perimeters.sort_values("tri").reset_index(drop=True)
    covered = shapely.union_all(perimeters.geometry.to_numpy())

    qa_foot = {
        "polygons": len(foot), "by_scenario": foot["scenario_id"].value_counts().sort_index().to_dict(),
        "open_ended_share": round(float(foot["depth_open_ended"].mean()), 4),
        "open_class_depth_m": OPEN_CLASS_DEPTH_M, "unknown_depth_m": UNKNOWN_DEPTH_M,
        "depth_coverage_of_extent_in_buffer": coverage, "unknown_depth_km2_added": gaps,
        "geometries_repaired": repaired, "tri_layer_copies_skipped": copies,
    }
    qa_scen = {"scenarios": scenarios["scenario_id"].tolist(), "sets": sorted(scenarios["set_id"].unique())}
    qa_per = {"tris": perimeters["tri"].tolist(),
              "core_share_in_tri_perimeters": round(float(covered.intersection(core).area / core.area), 4),
              "buffer_share_in_tri_perimeters": round(float(covered.intersection(grown).area / grown.area), 4)}
    return {"hazard_scenarios": Output(scenarios, "public", qa_scen),
            "hazard_footprints": Output(foot, "public", qa_foot),
            "stg_hazard_tri_perimeters": Output(perimeters, "public", qa_per)}
