"""Gas network (D4): NaTran transmission trace, GRDF distribution pipes, gas-fired power stations, and OpenStreetMap
pipelines, stations and LNG terminals.

* NaTran publishes a simplified trace of its transmission network, accurate to about 250 m ("never for works"),
  and no delivery stations; OpenStreetMap maps the pipelines more precisely and the stations
  (``pipeline=substation``) that open data otherwise lacks (assumption A-GAS-01). Both are staged; D6 reconciles them.
* GRDF's map of its distribution pipes shows where the public gas network reaches, without pressure or diameter.
* Gas-fired power stations couple gas and electricity (one row per site and year in the source; the latest is kept).
* LNG terminals are OpenStreetMap industrial areas named as such ("Terminal méthanier", LNG, GNL); the QA lists them.
"""

from __future__ import annotations

import re

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import shapely

from resilink.etl.adapters.osm_power import SOURCES as OSM_SOURCES
from resilink.etl.osm import read_osm, way_geometry
from resilink.etl.staging import AdapterContext, Output, adapter

NATRAN, GRDF, PLANTS = "natran.trace250", "grdf.network", "odre.gas_power_plants"
GAS = ("gas", "natural_gas")
GAS_OPERATORS = re.compile(r"natran|grt ?gaz|grdf|t[ée]r[ée]ga|storengy|elengy|fosmax|g[ée]om[ée]thane", re.IGNORECASE)
LNG_NAME = r"(?i)(?:m[ée]thanier|\bLNG\b|\bGNL\b)"
OSM_TAGS = ("man_made", "pipeline", "substance", "operator", "name", "ref", "diameter", "pressure", "location",
            "substation", "landuse")
NATRAN_UNCERTAINTY_M = 250.0


def _extent(ctx: AdapterContext) -> tuple[shapely.Geometry, shapely.Geometry]:
    extent = ctx.read("territory_extent").set_index("kind")
    core, grown = extent.geometry["core"], extent.geometry["buffer"]
    shapely.prepare(core)
    shapely.prepare(grown)
    return core, grown


def read_wkb_parquet(path: object, crs: str) -> gpd.GeoDataFrame:
    """An Opendatasoft Parquet export (``geo_shape`` as WKB, WGS84) in the territory's metric CRS."""
    table = pq.read_table(path)
    df = table.drop([c for c in ("geo_shape", "geo_point_2d") if c in table.schema.names]).to_pandas()
    geom = shapely.from_wkb(table.column("geo_shape").to_pylist())
    return gpd.GeoDataFrame(df, geometry=geom, crs="EPSG:4326").to_crs(crs)


def _lines_in(gdf: gpd.GeoDataFrame, core: shapely.Geometry, grown: shapely.Geometry) -> gpd.GeoDataFrame:
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    gdf = gdf[shapely.intersects(gdf.geometry.to_numpy(), grown)].copy()
    gdf["in_core"] = shapely.intersects(gdf.geometry.to_numpy(), core)
    gdf["length_m"] = gdf.geometry.length
    return gdf


def _km(gdf: gpd.GeoDataFrame) -> float:
    return round(float(gdf["length_m"].sum()) / 1000, 1)


@adapter("natran.network", version="1.0.0", sources=(NATRAN,), needs=("territory_extent",),
         produces=("stg_gas_transmission_natran",))
def natran_network(ctx: AdapterContext) -> dict[str, Output]:
    """NaTran transmission trace segments meeting the territory buffer (positions accurate to about 250 m)."""
    core, grown = _extent(ctx)
    raw = read_wkb_parquet(ctx.path(NATRAN), ctx.territory.crs_metric)
    seg = _lines_in(raw, core, grown)
    out = gpd.GeoDataFrame({
        "natran_object_id": seg["object_id"].astype(str).to_numpy(), "departement": seg["departement"].fillna("").to_numpy(),
        "in_core": seg["in_core"].to_numpy(), "length_m": seg["length_m"].to_numpy(),
        "pos_uncertainty_m": NATRAN_UNCERTAINTY_M, "geometry": seg.geometry.to_numpy(),
    }, geometry="geometry", crs=ctx.territory.crs_metric).sort_values("natran_object_id").reset_index(drop=True)
    qa = {"segments_national": len(raw), "segments": len(out), "segments_core": int(out["in_core"].sum()),
          "km_core": _km(out[out["in_core"]]), "km": _km(out)}
    return {"stg_gas_transmission_natran": Output(out, "internal", qa)}


@adapter("grdf.network", version="1.0.0", sources=(GRDF,), needs=("territory_extent", "admin_areas"),
         produces=("stg_gas_distribution_lines",))
def grdf_network(ctx: AdapterContext) -> dict[str, Output]:
    """GRDF distribution pipes meeting the territory buffer, and which of the territory's communes they reach."""
    t = ctx.territory
    core, grown = _extent(ctx)
    raw = read_wkb_parquet(ctx.path(GRDF), t.crs_metric)
    seg = _lines_in(raw, core, grown)
    code = seg["code_insee_administratif"].fillna("").astype(str).str.strip()
    # nulls first: with pandas' string dtype a null stays missing through astype(str) and never equals "nan"
    propane = ~seg["propane"].fillna("").astype(str).str.strip().str.lower().isin(("", "none", "non", "false", "0"))
    out = gpd.GeoDataFrame({
        "code_commune": np.where(code.str.isdigit(), code.str.zfill(5), code), "propane": propane.to_numpy(),
        "in_core": seg["in_core"].to_numpy(), "length_m": seg["length_m"].to_numpy(), "geometry": seg.geometry.to_numpy(),
    }, geometry="geometry", crs=t.crs_metric)
    out = out.iloc[np.lexsort((out.geometry.bounds["miny"].to_numpy(), out.geometry.bounds["minx"].to_numpy(),
                               out["code_commune"].to_numpy()))].reset_index(drop=True)
    admin = ctx.read("admin_areas")
    communes = admin[(admin["level"] == "commune") & admin["in_core"]]
    reached = set(out.loc[out["in_core"], "code_commune"])
    served = communes["code"].isin(reached)
    pop = communes["population"].astype("float")
    qa = {"segments_in_window": len(raw), "segments": len(out), "segments_core": int(out["in_core"].sum()),
          "km_core": _km(out[out["in_core"]]), "propane_segments": int(out["propane"].sum()),
          "core_communes_reached": int(served.sum()), "core_communes": len(communes),
          "core_census_population_in_reached_communes": round(float(pop[served].sum() / pop.sum()), 4)
          if pop.sum() else None,
          "core_communes_not_reached": sorted(communes.loc[~served, "code"])[:40]}
    return {"stg_gas_distribution_lines": Output(out, "internal", qa)}


@adapter("odre.gas_power_plants", version="1.0.0", sources=(PLANTS,), needs=("territory_extent",),
         produces=("stg_gas_power_plants",))
def gas_power_plants(ctx: AdapterContext) -> dict[str, Output]:
    """Gas-fired power stations inside the territory buffer, at their latest reference year."""
    t = ctx.territory
    core, grown = _extent(ctx)
    df = pd.read_csv(ctx.path(PLANTS), sep=";", dtype=str, encoding="utf-8-sig")
    df["year"] = pd.to_numeric(df["annee_de_reference"], errors="coerce")
    latest = df.sort_values(["site", "year"]).groupby("site", as_index=False).tail(1)
    lon = pd.to_numeric(latest["longitude_site"], errors="coerce").to_numpy(float)
    lat = pd.to_numeric(latest["latitude_site"], errors="coerce").to_numpy(float)
    pts = gpd.GeoSeries(shapely.points(lon, lat), crs="EPSG:4326").to_crs(t.crs_metric)
    x, y = pts.x.to_numpy(), pts.y.to_numpy()
    ok = np.isfinite(x) & np.isfinite(y)
    inside = ok & shapely.contains_xy(grown, np.where(ok, x, 0.0), np.where(ok, y, 0.0))
    sel = latest[inside]
    out = gpd.GeoDataFrame({
        "site": sel["site"].str.strip().to_numpy(), "operator": sel["operateur"].fillna("").to_numpy(),
        "status": sel["statut"].fillna("").to_numpy(),
        "power_mw": pd.to_numeric(sel["puissance_installee_mw"], errors="coerce").to_numpy(float),
        "reference_year": sel["year"].astype("Int64").to_numpy(), "commissioned": sel["date_de_mise_en_service"].fillna("").to_numpy(),
        "in_core": shapely.contains_xy(core, x[inside], y[inside]), "geometry": pts[inside].to_numpy(),
    }, geometry="geometry", crs=t.crs_metric).sort_values("site").reset_index(drop=True)
    core_rows = out[out["in_core"]]
    qa = {"sites_national": int(df["site"].nunique()), "reference_years": sorted(int(y) for y in df["year"].dropna().unique()),
          "plants": len(out), "plants_core": len(core_rows), "mw_core": float(core_rows["power_mw"].sum()),
          "core_sites": core_rows["site"].tolist()}
    return {"stg_gas_power_plants": Output(out, "public", qa)}


@adapter("osm.gas", version="1.0.0", sources=OSM_SOURCES, needs=("territory_extent", "stg_gas_transmission_natran"),
         produces=("stg_gas_pipelines_osm", "stg_gas_facilities_osm"))
def osm_gas(ctx: AdapterContext) -> dict[str, Output]:
    """Gas pipelines meeting the territory buffer, and gas stations and LNG terminals inside it, from OSM."""
    t = ctx.territory
    core, grown = _extent(ctx)
    gas = "tags['substance'] IN ('gas', 'natural_gas')"
    station = f"tags['pipeline'] = 'substation' AND ({gas} OR tags['substance'] IS NULL)"
    way_where = (f"(tags['man_made'] = 'pipeline' AND {gas}) OR ({station}) OR "
                 f"(tags['landuse'] = 'industrial' AND regexp_matches(coalesce(tags['name'], ''), '{LNG_NAME}'))")
    parts, per_source = [], {}
    for sid in OSM_SOURCES:
        ways, nodes, counts = read_osm(ctx.path(sid), OSM_TAGS, way_where, station)
        per_source[sid] = counts
        is_pipe = ways["man_made"].eq("pipeline").to_numpy()
        ways["geometry"] = [way_geometry(a, b, not p) for a, b, p in zip(ways["lon"], ways["lat"], is_pipe, strict=True)]
        ways["osm_id"], ways["osm_type"] = "w" + ways["id"].astype(str), "way"
        nodes["geometry"] = shapely.points(nodes["lon"].to_numpy(float), nodes["lat"].to_numpy(float))
        nodes["osm_id"], nodes["osm_type"] = "n" + nodes["id"].astype(str), "node"
        ways["source"], nodes["source"] = sid, sid
        parts += [ways, nodes]
    df = pd.concat(parts, ignore_index=True)
    df = df[df["geometry"].notna()].drop_duplicates("osm_id")  # first extract wins for shared features
    gdf = gpd.GeoDataFrame(df, geometry="geometry", crs="EPSG:4326").to_crs(t.crs_metric)

    def text(col: str, frame: gpd.GeoDataFrame) -> np.ndarray:
        return frame[col].fillna("").astype(str).to_numpy()

    pipes = _lines_in(gdf[gdf["man_made"].eq("pipeline")], core, grown)
    pipelines = gpd.GeoDataFrame({
        "osm_id": pipes["osm_id"].to_numpy(), "operator": text("operator", pipes), "name": text("name", pipes),
        "ref": text("ref", pipes), "diameter": text("diameter", pipes), "pressure": text("pressure", pipes),
        "location": text("location", pipes), "in_core": pipes["in_core"].to_numpy(), "length_m": pipes["length_m"].to_numpy(),
        "source": pipes["source"].to_numpy(), "geometry": pipes.geometry.to_numpy(),
    }, geometry="geometry", crs=t.crs_metric).sort_values("osm_id").reset_index(drop=True)

    fac = gdf[~gdf["man_made"].eq("pipeline")].copy()
    # stations often carry landuse=industrial too: an LNG terminal is a named industrial area that is not a station
    lng = (fac["landuse"].eq("industrial") & ~fac["pipeline"].eq("substation")
           & fac["name"].fillna("").str.contains(LNG_NAME[4:], case=False, regex=True)).to_numpy()
    known_gas = fac["substance"].isin(GAS).to_numpy() | fac["operator"].fillna("").str.contains(GAS_OPERATORS).to_numpy()
    fac, lng = fac[lng | known_gas], lng[lng | known_gas]  # stations without substance need a gas operator
    footprint = np.where(fac.geometry.geom_type == "Polygon", fac.geometry.area, 0.0)
    pts = fac.geometry.representative_point()
    inside = shapely.contains_xy(grown, pts.x.to_numpy(), pts.y.to_numpy())
    fac, lng, pts, footprint = fac[inside], lng[inside], pts[inside], footprint[inside]
    sub = fac["substation"].fillna("").astype(str)
    kind = np.where(lng, "lng_terminal", np.where(sub.isin(("", "yes")), "station", sub).astype(str))
    facilities = gpd.GeoDataFrame({
        "osm_id": fac["osm_id"].to_numpy(), "osm_type": fac["osm_type"].to_numpy(), "kind": kind,
        "operator": text("operator", fac), "name": text("name", fac), "ref": text("ref", fac),
        "substance": text("substance", fac), "footprint_m2": footprint,
        "in_core": shapely.contains_xy(core, pts.x.to_numpy(), pts.y.to_numpy()),
        "source": fac["source"].to_numpy(), "geometry": pts.to_numpy(),
    }, geometry="geometry", crs=t.crs_metric).sort_values("osm_id").reset_index(drop=True)

    natran = ctx.read("stg_gas_transmission_natran")
    core_pipes = pipelines[pipelines["in_core"]]
    natran_km = _km(natran[natran["in_core"]])
    osm_natran_km = _km(core_pipes[core_pipes["operator"].str.contains(r"natran|grt ?gaz", case=False)])
    core_fac = facilities[facilities["in_core"]]
    qa_pipes = {"per_source": per_source, "pipelines": len(pipelines), "pipelines_core": len(core_pipes),
                "km_core": _km(core_pipes),
                "km_core_by_operator": {k: round(v / 1000, 1) for k, v in
                                        core_pipes.groupby("operator")["length_m"].sum().sort_values(ascending=False).head(6).items()},
                "km_core_natran_osm_vs_trace": [osm_natran_km, natran_km],
                "core_with_diameter": int((core_pipes["diameter"] != "").sum())}
    qa_fac = {"facilities": len(facilities), "facilities_core": len(core_fac),
              "core_by_kind": core_fac["kind"].value_counts().sort_index().to_dict(),
              "lng_terminals": facilities.loc[facilities["kind"] == "lng_terminal", "name"].tolist()}
    return {"stg_gas_pipelines_osm": Output(pipelines, "internal", qa_pipes),
            "stg_gas_facilities_osm": Output(facilities, "internal", qa_fac)}
