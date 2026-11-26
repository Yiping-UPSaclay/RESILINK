"""Enedis distribution network (Data Fair, WGS84) -> ``stg_elec_mv_lv_substations`` and ``stg_elec_mv_lines`` (D2).

Open data gives positions only: MV/LV (HTA/BT) substations as points and medium-voltage (HTA) lines as segments,
overhead or underground, without voltage, feeder or switch state. Segments are joined into a network in D6
(noding, chain contraction, attachment to substations; assumption A-TOPO-01 covers the missing switch states).
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from resilink.domain.ids import point_fingerprint
from resilink.etl.staging import AdapterContext, Output, adapter

SUBS, AERIAL, UNDERGROUND = "enedis.poste_electrique", "enedis.reseau_hta", "enedis.reseau_souterrain_hta"


def read_metric(ctx: AdapterContext, source_id: str) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(ctx.path(source_id))
    if gdf.crs is None:  # RFC 7946 GeoJSON is WGS84
        gdf = gdf.set_crs("EPSG:4326")
    return gdf.to_crs(ctx.territory.crs_metric)


@adapter("enedis.network", version="1.0.0", sources=(SUBS, AERIAL, UNDERGROUND), needs=("territory_extent",),
         produces=("stg_elec_mv_lv_substations", "stg_elec_mv_lines"))
def enedis_network(ctx: AdapterContext) -> dict[str, Output]:
    """MV/LV substations inside the territory buffer and MV line segments meeting it, in the metric CRS."""
    t = ctx.territory
    extent = ctx.read("territory_extent").set_index("kind")
    core, grown = extent.geometry["core"], extent.geometry["buffer"]
    shapely.prepare(core)
    shapely.prepare(grown)

    subs = read_metric(ctx, SUBS)
    n_window = len(subs)
    subs = subs[~subs.geometry.is_empty & subs.geometry.notna()]
    x, y = subs.geometry.x.to_numpy(), subs.geometry.y.to_numpy()
    subs = subs[shapely.contains_xy(grown, x, y)].copy()
    x, y = subs.geometry.x.to_numpy(), subs.geometry.y.to_numpy()
    fp = [point_fingerprint(a, b) for a, b in zip(x, y, strict=True)]
    codes = [str(c) for c in t.selector.get("departement", [])]
    by_code = int(subs["code_departement"].astype(str).isin(codes).sum())  # Enedis's own département attribute
    stations = gpd.GeoDataFrame({
        "rid": [f"elec.sub_mv_lv:fp:{f}" for f in fp],
        "enedis_id": subs["_id"].astype(str).to_numpy(),
        "code_commune": subs["code_commune"].astype(str).to_numpy(),
        "in_core": shapely.contains_xy(core, x, y),
        "geometry": subs.geometry.to_numpy(),
    }, geometry="geometry", crs=t.crs_metric)
    dup = int(stations["rid"].duplicated().sum())
    stations = stations.drop_duplicates("rid").sort_values("rid").reset_index(drop=True)

    parts = []
    n_seg_window = {}
    for sid, aerial in ((AERIAL, True), (UNDERGROUND, False)):
        seg = read_metric(ctx, sid)
        n_seg_window[sid] = len(seg)
        seg = seg[seg.geometry.notna() & ~seg.geometry.is_empty]
        seg = seg[shapely.intersects(seg.geometry.to_numpy(), grown)]
        parts.append(gpd.GeoDataFrame({
            "enedis_id": seg["_id"].astype(str).to_numpy(),
            "aerial": aerial,
            "code_commune": seg["code_commune"].astype(str).to_numpy(),
            "in_core": shapely.intersects(seg.geometry.to_numpy(), core),
            "length_m": seg.geometry.length.to_numpy(),
            "geometry": seg.geometry.to_numpy(),
        }, geometry="geometry", crs=t.crs_metric))
    lines = pd.concat(parts, ignore_index=True)
    lines = gpd.GeoDataFrame(lines, geometry="geometry", crs=t.crs_metric).sort_values(
        ["aerial", "enedis_id"], ascending=[False, True]).reset_index(drop=True)

    core_lines = lines[lines["in_core"]]
    km_aerial = float(core_lines.loc[core_lines["aerial"], "length_m"].sum()) / 1000
    km_under = float(core_lines.loc[~core_lines["aerial"], "length_m"].sum()) / 1000
    qa_subs = {"in_window": n_window, "substations": len(stations), "substations_core": int(stations["in_core"].sum()),
               "substations_with_core_departement_code": by_code, "duplicate_positions_dropped": dup}
    qa_lines = {
        "segments_in_window": n_seg_window, "segments": len(lines), "segments_core": int(lines["in_core"].sum()),
        "km_aerial_core": round(km_aerial, 1), "km_underground_core": round(km_under, 1),
        "aerial_share_core": round(km_aerial / (km_aerial + km_under), 4) if km_aerial + km_under else None,
        "geometry_types": lines.geometry.geom_type.value_counts().to_dict(),
        "median_segment_m": round(float(np.median(lines["length_m"])), 1) if len(lines) else None,
    }
    return {"stg_elec_mv_lv_substations": Output(stations, "internal", qa_subs),
            "stg_elec_mv_lines": Output(lines, "internal", qa_lines)}
