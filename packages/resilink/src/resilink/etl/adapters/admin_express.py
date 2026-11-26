"""IGN ADMIN EXPRESS COG -> ``territory_extent`` and ``admin_areas`` (D1)."""

from __future__ import annotations

import re

import geopandas as gpd
import pandas as pd
import shapely

from resilink.etl.staging import AdapterContext, Output, adapter

DEP = "ign.admin_express.departement"
COM = "ign.admin_express.commune"


def read_metric(ctx: AdapterContext, source_id: str) -> tuple[gpd.GeoDataFrame, int]:
    """A WFS snapshot in the territory's metric CRS, with invalid geometries repaired (returns the repair count)."""
    gdf = gpd.read_file(ctx.path(source_id))
    want = ctx.territory.crs_metric
    if gdf.crs is None or gdf.crs.to_epsg() != int(want.split(":")[-1]):
        raise ValueError(f"{source_id}: snapshot CRS {gdf.crs} is not the territory's {want}")
    gdf = gdf.set_crs(want, allow_override=True)
    bad = ~gdf.geometry.is_valid
    if bad.any():
        gdf.loc[bad, "geometry"] = shapely.make_valid(gdf.geometry[bad].values)
    return gdf, int(bad.sum())


def cog_year(ctx: AdapterContext) -> int:
    """The COG edition pinned by the sources' feature types (``ADMINEXPRESS-COG.<year>:...``), checked against
    the territory's ``selector.cog_year`` so ids from different editions never mix."""
    years = set()
    for sid in (DEP, COM):
        m = re.search(r"\.(\d{4}):", ctx.sources[sid].dataset)
        if not m:
            raise ValueError(f"{sid}: feature type {ctx.sources[sid].dataset!r} pins no COG year")
        years.add(int(m.group(1)))
    if len(years) != 1:
        raise ValueError(f"ADMIN EXPRESS sources pin different COG editions: {sorted(years)}")
    year = years.pop()
    wanted = ctx.territory.selector.get("cog_year")
    if wanted is not None and int(wanted) != year:
        raise ValueError(f"territory {ctx.territory.id} expects COG {wanted}, sources pin COG {year}")
    return year


@adapter("ign.admin_express", version="1.1.0", sources=(DEP, COM), produces=("admin_areas", "territory_extent"))
def admin_express(ctx: AdapterContext) -> dict[str, Output]:
    """Territory extent (core départements, and the core grown by the buffer) and admin areas (every
    département and commune meeting the buffer; ``in_core`` marks the territory's own).

    The WFS returns areas in a bounding-box window; the ones meeting the buffer itself are kept. Ids follow the
    COG edition of the sources, e.g. ``admin.commune:cog2026:13055``. Buffer départements carry no population
    (only part of them lies in the territory). 1.1.0 adds the buffer départements (their names identify RTE sites).
    """
    t = ctx.territory
    year = cog_year(ctx)
    codes = [str(c) for c in t.selector.get("departement", [])]
    deps, fixed_d = read_metric(ctx, DEP)
    core_deps = deps[deps["code_insee"].isin(codes)].sort_values("code_insee")
    missing = sorted(set(codes) - set(core_deps["code_insee"]))
    if missing:
        raise ValueError(f"départements {missing} are not in the {DEP} snapshot")
    core = shapely.union_all(core_deps.geometry.values)
    grown = core.buffer(t.buffer_m)
    extent = gpd.GeoDataFrame({
        "extent_id": [f"{t.id}:core", f"{t.id}:buffer"], "kind": ["core", "buffer"],
        "buffer_m": [0.0, float(t.buffer_m)], "departements": [",".join(codes)] * 2, "geometry": [core, grown],
    }, geometry="geometry", crs=t.crs_metric)

    com, fixed_c = read_metric(ctx, COM)
    window = len(com)
    com = com[com.geometry.intersects(grown)].copy()
    in_core = com["code_insee_du_departement"].isin(codes)
    population = pd.to_numeric(com["population"], errors="coerce")
    census = com["date_du_recensement"].astype(str).str[:10]
    communes = gpd.GeoDataFrame({
        "admin_id": f"admin.commune:cog{year}:" + com["code_insee"],
        "level": "commune",
        "code": com["code_insee"],
        "name": com["nom_officiel"],
        "parent_id": f"admin.departement:cog{year}:" + com["code_insee_du_departement"],
        "cog_year": year,
        "status": com["statut"].fillna(""),
        "population": population.fillna(0).astype("int64"),
        "census_date": census,
        "in_core": in_core,
        "area_km2": com.geometry.area / 1e6,
        "geometry": com.geometry,
    }, geometry="geometry", crs=t.crs_metric)
    dep_pop = communes[communes["in_core"]].groupby(communes["parent_id"])["population"].sum()
    near = deps[deps.geometry.intersects(grown)].sort_values("code_insee")
    near_core = near["code_insee"].isin(codes)
    departements = gpd.GeoDataFrame({
        "admin_id": f"admin.departement:cog{year}:" + near["code_insee"],
        "level": "departement",
        "code": near["code_insee"],
        "name": near["nom_officiel"],
        "parent_id": f"admin.region:cog{year}:" + near["code_insee_de_la_region"],
        "cog_year": year,
        "status": "",
        "population": pd.array([int(dep_pop.get(f"admin.departement:cog{year}:{c}", 0)) if core else pd.NA
                                for c, core in zip(near["code_insee"], near_core, strict=True)], dtype="Int64"),
        "census_date": [(sorted(set(census[in_core]))[-1] if in_core.any() else "") if core else ""
                        for core in near_core],
        "in_core": near_core.to_numpy(),
        "area_km2": near.geometry.area / 1e6,
        "geometry": near.geometry,
    }, geometry="geometry", crs=t.crs_metric)
    communes["population"] = communes["population"].astype("Int64")
    admin = pd.concat([departements, communes], ignore_index=True).sort_values("admin_id").reset_index(drop=True)
    admin = gpd.GeoDataFrame(admin, geometry="geometry", crs=t.crs_metric)

    qa_admin = {
        "cog_year": year, "departements": codes,
        "departements_buffer_only": sorted(near.loc[~near_core, "code_insee"]),
        "communes_core": int(in_core.sum()), "communes_buffer_only": int((~in_core).sum()),
        "communes_in_wfs_window": window, "communes_without_population": int(population.isna().sum()),
        "census_population_core": int(communes.loc[communes["in_core"], "population"].sum()),
        "census_dates": sorted(set(census)), "geometries_repaired": fixed_d + fixed_c,
    }
    qa_extent = {"core_area_km2": round(core.area / 1e6, 1), "buffer_area_km2": round(grown.area / 1e6, 1),
                 "buffer_m": float(t.buffer_m), "departements": codes}
    return {"admin_areas": Output(admin, "public", qa_admin), "territory_extent": Output(extent, "public", qa_extent)}
