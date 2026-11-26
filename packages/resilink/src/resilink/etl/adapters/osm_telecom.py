"""OpenStreetMap telecom buildings -> ``stg_telecom_exchanges_osm`` (D3).

Telephone exchanges and fibre nodes (``telecom=exchange``, e.g. Orange central offices, operators' NRO), central
offices and data centres: the fixed side that mobile sites are backhauled through. Same Geofabrik extracts as the
power network (ODbL 1.0; tags are untrusted text, stored as data only).
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from resilink.etl.adapters.osm_power import SOURCES
from resilink.etl.osm import read_features, way_geometry
from resilink.etl.staging import AdapterContext, Output, adapter

TAGS = ("telecom", "operator", "name", "ref", "building")
TYPES = ("exchange", "central_office", "data_center")


@adapter("osm.telecom", version="1.0.0", sources=SOURCES, needs=("territory_extent",),
         produces=("stg_telecom_exchanges_osm",))
def osm_telecom(ctx: AdapterContext) -> dict[str, Output]:
    """Exchanges, central offices and data centres inside the territory buffer, as points with their footprint."""
    t = ctx.territory
    extent = ctx.read("territory_extent").set_index("kind")
    core, grown = extent.geometry["core"], extent.geometry["buffer"]
    shapely.prepare(core)
    shapely.prepare(grown)
    parts, per_source = [], {}
    for sid in SOURCES:
        ways, nodes, counts = read_features(ctx.path(sid), "telecom", TYPES, TYPES, TAGS)
        per_source[sid] = counts
        ways["geometry"] = [way_geometry(a, b, True) for a, b in zip(ways["lon"], ways["lat"], strict=True)]
        ways["osm_id"], ways["osm_type"] = "w" + ways["id"].astype(str), "way"
        nodes["geometry"] = shapely.points(nodes["lon"].to_numpy(float), nodes["lat"].to_numpy(float))
        nodes["osm_id"], nodes["osm_type"] = "n" + nodes["id"].astype(str), "node"
        ways["source"], nodes["source"] = sid, sid
        parts += [ways, nodes]
    df = pd.concat(parts, ignore_index=True)
    df = df[df["geometry"].notna()].drop_duplicates("osm_id")  # first extract wins for shared features
    gdf = gpd.GeoDataFrame(df, geometry="geometry", crs="EPSG:4326").to_crs(t.crs_metric)
    footprint = np.where(gdf.geometry.geom_type == "Polygon", gdf.geometry.area, 0.0)
    pts = gdf.geometry.representative_point()
    inside = shapely.contains_xy(grown, pts.x.to_numpy(), pts.y.to_numpy())
    gdf, pts, footprint = gdf[inside], pts[inside], footprint[inside]
    out = gpd.GeoDataFrame({
        "osm_id": gdf["osm_id"].to_numpy(), "osm_type": gdf["osm_type"].to_numpy(),
        "telecom": gdf["telecom"].fillna("").to_numpy(), "operator": gdf["operator"].fillna("").to_numpy(),
        "name": gdf["name"].fillna("").to_numpy(), "ref": gdf["ref"].fillna("").to_numpy(),
        "footprint_m2": footprint, "in_core": shapely.contains_xy(core, pts.x.to_numpy(), pts.y.to_numpy()),
        "source": gdf["source"].to_numpy(), "geometry": pts.to_numpy(),
    }, geometry="geometry", crs=t.crs_metric).sort_values("osm_id").reset_index(drop=True)
    core_rows = out[out["in_core"]]
    qa = {"per_source": per_source, "features": len(out), "features_core": len(core_rows),
          "core_by_type": core_rows["telecom"].value_counts().sort_index().to_dict(),
          "core_by_operator": core_rows.loc[core_rows["operator"] != "", "operator"].value_counts().head(8).to_dict(),
          "core_without_operator": int((core_rows["operator"] == "").sum())}
    return {"stg_telecom_exchanges_osm": Output(out, "internal", qa)}
