"""OpenStreetMap power network -> ``stg_elec_hv_lines_osm`` and ``stg_elec_substations_osm`` (D2).

RTE has published no geometry since 2026-06-16, so high-voltage lines and substations are located from
OpenStreetMap (Geofabrik extracts, read with DuckDB spatial ``ST_ReadOSM``). High voltage = 45 kV and above (French
HTB). ``power=line`` ways without a voltage tag are kept and flagged; cables and minor lines need a voltage tag.
Ways present in both extracts are kept once. ODbL 1.0: derived tables are share-alike and credit
"© OpenStreetMap contributors". Tags are untrusted text (invariant I5): they are stored as data only.
"""

from __future__ import annotations

import re
from pathlib import Path

import duckdb
import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from resilink.etl.staging import AdapterContext, Output, adapter
from resilink.io.duck import load_extension

SOURCES = ("osm.geofabrik.paca", "osm.geofabrik.languedoc_roussillon")
TAGS = ("power", "voltage", "circuits", "cables", "operator", "name", "ref", "ref:FR:RTE", "location", "substation")
HV_KV = 45.0
LINE_POWER = ("line", "cable", "minor_line")
KEEP_UNKNOWN_SUBSTATION = ("transmission", "distribution", "converter", "traction")
CLASSES_KV = (63.0, 90.0, 150.0, 225.0, 400.0)


def max_kv(voltage: object) -> float:
    """Highest voltage of an OSM ``voltage`` tag in kV ('225000;63000' -> 225.0); NaN when absent or not numeric."""
    nums = [float(p) for p in re.split(r"[;,]", str(voltage or "")) if re.fullmatch(r"\s*\d+(\.\d+)?\s*", p)]
    return max(nums) / 1000.0 if nums else float("nan")


def voltage_class(kv: float) -> str:
    if np.isnan(kv):
        return "unknown"
    if kv < HV_KV:
        return "<45"
    return f"{min(CLASSES_KV, key=lambda c: abs(c - kv)):.0f}"


def read_power(path: Path) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    """Power ways (with node coordinates in order) and power=substation nodes of one PBF file."""
    con = duckdb.connect()
    load_extension(con, "spatial")
    cols = ", ".join(f"tags['{t}'] AS \"{t}\"" for t in TAGS)
    con.execute(f"""CREATE TEMP TABLE w AS SELECT id, refs, {cols} FROM ST_ReadOSM(?)
                    WHERE kind = 'way' AND tags['power'] IN ('line', 'cable', 'minor_line', 'substation')""", [str(path)])
    con.execute("CREATE TEMP TABLE r AS SELECT id AS way, unnest(refs) AS ref, generate_subscripts(refs, 1) AS i FROM w")
    con.execute(f"""CREATE TEMP TABLE n AS SELECT kind, id, lat, lon, {cols} FROM ST_ReadOSM(?)
                    WHERE (kind = 'node' AND (id IN (SELECT ref FROM r) OR tags['power'] = 'substation'))
                       OR (kind = 'relation' AND tags['power'] = 'substation')""", [str(path)])
    coords = con.execute("""SELECT r.way AS id, list(n.lon ORDER BY r.i) FILTER (WHERE n.id IS NOT NULL) AS lon,
                                   list(n.lat ORDER BY r.i) FILTER (WHERE n.id IS NOT NULL) AS lat,
                                   count(*) FILTER (WHERE n.id IS NULL) AS missing
                            FROM r LEFT JOIN (SELECT id, lon, lat FROM n WHERE kind = 'node') n ON n.id = r.ref
                            GROUP BY r.way""").df()
    ways = con.execute("SELECT * EXCLUDE (refs) FROM w").df().merge(coords, on="id", how="left")
    nodes = con.execute("SELECT * EXCLUDE (kind) FROM n WHERE kind = 'node' AND power = 'substation'").df()
    counts = {"power_ways": len(ways), "substation_nodes": len(nodes),
              "substation_relations_skipped": int(con.execute("SELECT count(*) FROM n WHERE kind = 'relation'").fetchone()[0]),
              "missing_node_refs": int(coords["missing"].sum()) if len(coords) else 0}
    con.close()
    return ways, nodes, counts


def _way_geometry(lon: object, lat: object, closed_polygon: bool) -> shapely.Geometry | None:
    if lon is None or lat is None or len(lon) < 2:  # type: ignore[arg-type]
        return None
    xy = np.column_stack([np.asarray(lon, float), np.asarray(lat, float)])
    if closed_polygon and len(xy) >= 4 and np.array_equal(xy[0], xy[-1]):
        return shapely.Polygon(xy)
    return shapely.LineString(xy)


def _tags(df: pd.DataFrame) -> dict[str, object]:
    return {"operator": df["operator"].fillna("").to_numpy(), "name": df["name"].fillna("").to_numpy(),
            "ref": df["ref"].fillna("").to_numpy()}


@adapter("osm.power", version="1.0.0", sources=SOURCES, needs=("territory_extent",),
         produces=("stg_elec_hv_lines_osm", "stg_elec_substations_osm"))
def osm_power(ctx: AdapterContext) -> dict[str, Output]:
    """High-voltage lines meeting the territory buffer and high-voltage substations inside it, from OSM."""
    t = ctx.territory
    extent = ctx.read("territory_extent").set_index("kind")
    core, grown = extent.geometry["core"], extent.geometry["buffer"]
    shapely.prepare(core)
    shapely.prepare(grown)

    line_parts, sub_parts, per_source = [], [], {}
    for sid in SOURCES:
        ways, nodes, counts = read_power(ctx.path(sid))
        per_source[sid] = counts
        ways["source"], nodes["source"] = sid, sid
        lw = ways[ways["power"].isin(LINE_POWER)].copy()
        lw["geometry"] = [_way_geometry(a, b, False) for a, b in zip(lw["lon"], lw["lat"], strict=True)]
        lw["osm_id"] = "w" + lw["id"].astype(str)
        line_parts.append(lw)
        sw = ways[ways["power"] == "substation"].copy()
        sw["geometry"] = [_way_geometry(a, b, True) for a, b in zip(sw["lon"], sw["lat"], strict=True)]
        sw["osm_id"], sw["osm_type"] = "w" + sw["id"].astype(str), "way"
        nodes["geometry"] = shapely.points(nodes["lon"].to_numpy(float), nodes["lat"].to_numpy(float))
        nodes["osm_id"], nodes["osm_type"] = "n" + nodes["id"].astype(str), "node"
        sub_parts += [sw, nodes]

    def frame(parts: list[pd.DataFrame]) -> gpd.GeoDataFrame:
        df = pd.concat(parts, ignore_index=True)
        df = df[df["geometry"].notna()].drop_duplicates("osm_id")  # first extract wins for shared ways
        return gpd.GeoDataFrame(df, geometry="geometry", crs="EPSG:4326").to_crs(t.crs_metric)

    lines = frame(line_parts)
    lines["voltage_kv"] = lines["voltage"].map(max_kv)
    hv = (lines["voltage_kv"] >= HV_KV) | ((lines["power"] == "line") & lines["voltage_kv"].isna())
    n_low = int((~hv).sum())
    lines = lines[hv & shapely.intersects(lines.geometry.to_numpy(), grown)]
    hv_lines = gpd.GeoDataFrame({
        "osm_id": lines["osm_id"].to_numpy(), "power": lines["power"].to_numpy(),
        "voltage_kv": lines["voltage_kv"].to_numpy(float), "voltages": lines["voltage"].fillna("").to_numpy(),
        "voltage_class": lines["voltage_kv"].map(voltage_class).to_numpy(),
        "circuits": lines["circuits"].fillna("").to_numpy(), "cables": lines["cables"].fillna("").to_numpy(),
        **_tags(lines), "location": lines["location"].fillna("").to_numpy(),
        "in_core": shapely.intersects(lines.geometry.to_numpy(), core),
        "length_m": lines.geometry.length.to_numpy(), "source": lines["source"].to_numpy(),
        "geometry": lines.geometry.to_numpy(),
    }, geometry="geometry", crs=t.crs_metric).sort_values("osm_id").reset_index(drop=True)

    subs = frame(sub_parts)
    subs["voltage_kv"] = subs["voltage"].map(max_kv)
    kind = subs["substation"].fillna("")
    keep = (subs["voltage_kv"] >= HV_KV) | (subs["voltage_kv"].isna() & kind.isin(KEEP_UNKNOWN_SUBSTATION))
    subs = subs[keep]
    footprint = np.where(subs.geometry.geom_type == "Polygon", subs.geometry.area, 0.0)
    pts = subs.geometry.representative_point()
    inside = shapely.contains_xy(grown, pts.x.to_numpy(), pts.y.to_numpy())
    subs, pts, footprint = subs[inside], pts[inside], footprint[inside]
    substations = gpd.GeoDataFrame({
        "osm_id": subs["osm_id"].to_numpy(), "osm_type": subs["osm_type"].to_numpy(),
        "substation": subs["substation"].fillna("").to_numpy(),
        "voltage_kv": subs["voltage_kv"].to_numpy(float), "voltages": subs["voltage"].fillna("").to_numpy(),
        "voltage_class": subs["voltage_kv"].map(voltage_class).to_numpy(),
        **_tags(subs), "ref_fr_rte": subs["ref:FR:RTE"].fillna("").to_numpy(),
        "footprint_m2": footprint, "in_core": shapely.contains_xy(core, pts.x.to_numpy(), pts.y.to_numpy()),
        "source": subs["source"].to_numpy(), "geometry": pts.to_numpy(),
    }, geometry="geometry", crs=t.crs_metric).sort_values("osm_id").reset_index(drop=True)

    core_lines = hv_lines[hv_lines["in_core"]]
    core_subs = substations[substations["in_core"]]
    qa_lines = {
        "per_source": per_source, "lines": len(hv_lines), "lines_core": len(core_lines),
        "lines_below_45kv_dropped": n_low,
        "km_core_by_class": {k: round(v / 1000, 1) for k, v in
                             core_lines.groupby("voltage_class")["length_m"].sum().sort_index().items()},
        "lines_unknown_voltage": int((hv_lines["voltage_class"] == "unknown").sum()),
    }
    qa_subs = {
        "substations": len(substations), "substations_core": len(core_subs),
        "core_by_class": core_subs["voltage_class"].value_counts().sort_index().to_dict(),
        "core_with_ref_fr_rte": int((core_subs["ref_fr_rte"] != "").sum()),
        "core_with_name": int((core_subs["name"] != "").sum()),
        "core_polygons": int((core_subs["osm_type"] == "way").sum()),
    }
    return {"stg_elec_hv_lines_osm": Output(hv_lines, "internal", qa_lines),
            "stg_elec_substations_osm": Output(substations, "internal", qa_subs)}
