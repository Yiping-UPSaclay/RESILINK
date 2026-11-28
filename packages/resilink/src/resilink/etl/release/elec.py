"""Electricity network of a release (D6, blueprint §5.4-5.5).

* **High-voltage substations** are OpenStreetMap substations. Those carrying an RTE code (``ref:FR:RTE``) take the
  RTE identity (``elec.<class>:rte:<code>``); several OSM footprints with one code are one substation. RTE publishes
  no positions since 2026-06-16, so its list is the completeness reference (QA gate: >= 90 % of the core's
  substations in service found in OSM).
* **Primary substations** (postes sources) are the high-voltage substations where at least three medium-voltage
  lines end (Enedis publishes no list of them); the others are transmission substations. Railway, industrial and
  power-plant substations never feed the public MV network. Cable-to-line transition points are junctions.
* **High-voltage lines** are OpenStreetMap power lines and cables (>= 45 kV), noded and contracted by voltage.
  Where a line leaves the territory buffer, the cut point is a source: the national grid beyond the buffer is not
  modelled (assumption A-ELEC-02). Gas-fired plants are sources linked to the nearest substation within 3 km.
* **Medium voltage**: Enedis segments noded at 2 m and contracted by kind (overhead or underground); MV/LV
  substations attach line ends within 5 m (assumption A-ELEC-03 on their count), primary substations within
  150 m of their footprint. Without switch states every tie counts as closed (assumption A-TOPO-01).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from resilink.domain.ids import make_rid
from resilink.etl.adapters.rte import name_key
from resilink.etl.release.frames import (
    CRS,
    Extent,
    Staged,
    anchors_from,
    edge_frame,
    network_frames,
    node_frame,
)
from resilink.etl.topology import Network, build_network

HV_MARGIN_M = 50.0  # OSM lines end at a portal inside or next to the substation footprint
PRIMARY_MARGIN_M = 150.0  # blueprint §5.5: feeders attach to a primary substation within 150 m
MV_LV_RADIUS_M = 5.0  # Enedis places substations ~1.5 m from their cable ends (99 % within 3.5 m, measured 2026-11-28)
MV_SNAP_M, MV_SPLIT_M = 2.0, 1.0  # blueprint §5.5: node at shared ends and T-junctions within 2 m
HV_SNAP_M, HV_SPLIT_M = 1.0, 0.5  # OSM ways share nodes exactly
MIN_FEEDERS = 3  # MV line ends that make a high-voltage substation a primary substation
GENERATOR_LINK_M = 3000.0
NO_PUBLIC_MV = {"traction", "industrial", "generation"}  # OSM substation=* values that never feed the public network
UNDERGROUND = {"underground", "underwater", "indoor"}


@dataclass(frozen=True)
class SystemBuild:
    nodes: gpd.GeoDataFrame
    edges: gpd.GeoDataFrame
    xref: pd.DataFrame
    qa: dict[str, Any] = field(default_factory=dict)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name_key(text)).strip("_")


def hv_substations(osm: gpd.GeoDataFrame, rte: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """One row per substation (key ``rte:<code>`` or ``osm:<id>``) and one anchor row per OSM footprint."""
    code_of: dict[str, str] = {}
    for code, ids in zip(rte["code_poste"], rte["osm_ids_by_ref"].fillna(""), strict=True):
        for osm_id in filter(None, ids.split(";")):
            code_of.setdefault(osm_id, code)
    osm = osm.assign(key=[f"rte:{code_of[i]}" if i in code_of else f"osm:{i}" for i in osm["osm_id"]],
                     radius=osm["reach_m"].to_numpy(dtype=float))
    rte_by_code = rte.set_index("code_poste")
    rows = []
    for key, grp in osm.sort_values(["key", "footprint_m2", "osm_id"], ascending=[True, False, True]).groupby("key"):
        main = grp.iloc[0]
        types = set(grp["substation"].fillna(""))
        code = key[4:] if key.startswith("rte:") else None
        site = rte_by_code.loc[code] if code else None
        voltage = np.nanmax(np.r_[grp["voltage_kv"].to_numpy(dtype=float), np.nan if site is None else site["tension_kv"]]) \
            if grp["voltage_kv"].notna().any() or (site is not None and pd.notna(site["tension_kv"])) else np.nan
        rows.append({
            "key": key, "osm_ids": ";".join(grp["osm_id"]), "code_poste": code,
            "name": str(site["nom_poste"]) if site is not None else str(main["name"] or ""),
            "operator": "RTE" if site is not None else str(main["operator"] or ""),
            "status": "planned" if site is not None and site["etat"] != "EN EXPLOITATION" else "in_service",
            "voltage_kv": voltage, "transition": types == {"transition"},
            "may_feed_mv": bool(types - NO_PUBLIC_MV - {"transition"}),
            "pos_uncertainty_m": max(10.0, float(main["radius"])),
            "confidence": 0.95 if site is not None else 0.8,
            "primary_source": "rte.postes+osm" if site is not None else "osm",
            "geometry": main.geometry,
        })
    subs = pd.DataFrame(rows)
    anchors = anchors_from(osm, "key", osm["radius"].to_numpy() + HV_MARGIN_M)
    core = rte[rte["in_core"] & (rte["fonction"] == "Poste de transformation") & (rte["etat"] == "EN EXPLOITATION")]
    matched = core["code_poste"].isin({k[4:] for k in subs["key"] if k.startswith("rte:")})
    names = {i: f" {name_key(n)} " for i, n in zip(osm["osm_id"], osm["name"].fillna(""), strict=True)}
    unmatched = core.loc[~matched, ["code_poste", "nom_poste"]]
    name_hits = {c: [i for i, n in names.items() if f" {name_key(nm)} " in n and i not in code_of]
                 for c, nm in zip(unmatched["code_poste"], unmatched["nom_poste"], strict=True)}
    qa = {"core_substations_in_service": len(core), "core_matched_by_rte_code": int(matched.sum()),
          "core_match_share": round(float(matched.mean()), 4) if len(core) else None,
          "core_unmatched": unmatched["code_poste"].tolist(),
          "core_unmatched_with_osm_name_hit": {c: h for c, h in name_hits.items() if h},  # residue for review
          "osm_footprints_merged": int(len(osm) - len(subs))}
    return subs, anchors, qa


def _mv_network(lines: gpd.GeoDataFrame, mvlv: gpd.GeoDataFrame, primaries: pd.DataFrame, extent: Extent) -> Network:
    anchors = pd.concat([anchors_from(mvlv, "rid", MV_LV_RADIUS_M), primaries], ignore_index=True)
    return build_network(lines, anchors, snap_m=MV_SNAP_M, split_m=MV_SPLIT_M, boundary=extent.buffer,
                         same=("class",), weighted=("aerial_share",))


def build_elec(st: Staged, extent: Extent) -> SystemBuild:
    osm = st.read("stg_elec_substations_osm")
    rte = st.read("stg_elec_rte_sites")
    subs, hv_anchors, qa_match = hv_substations(osm, rte)

    # --- medium voltage: find the primary substations, then build with them only ---------------------------
    mv = st.read("stg_elec_mv_lines")
    mv = mv.assign(**{"class": np.where(mv["aerial"], "line_mv_aerial", "cable_mv"),
                      "aerial_share": mv["aerial"].astype(float)})
    mvlv = st.read("stg_elec_mv_lv_substations")
    candidates = set(subs.loc[subs["may_feed_mv"], "key"])
    cand_anchors = hv_anchors[hv_anchors["rid"].isin(candidates)].assign(
        radius_m=lambda a: a["radius_m"] - HV_MARGIN_M + PRIMARY_MARGIN_M, split=False)
    probe = _mv_network(mv, mvlv, cand_anchors, extent)
    feeders = probe.vertices.dropna(subset=["anchor"]).set_index("anchor")["degree"]
    primary_keys = {k for k in candidates if feeders.get(k, 0) >= MIN_FEEDERS}
    mv_net = _mv_network(mv, mvlv, cand_anchors[cand_anchors["rid"].isin(primary_keys)], extent)

    def cls_of(row: pd.Series) -> str:
        return "junction_hv" if row["transition"] else "sub_primary" if row["key"] in primary_keys else "sub_tx"

    subs["class"] = subs.apply(cls_of, axis=1)
    subs["rid"] = [make_rid("elec", c, *k.split(":", 1)) for c, k in zip(subs["class"], subs["key"], strict=True)]
    rid_of = dict(zip(subs["key"], subs["rid"], strict=True))
    rid_of.update({r: r for r in mvlv["rid"]})

    mv_j, mv_e = network_frames(mv_net, "elec", junction_class="junction_mv", edge_class=mv_net.edges["class"],
                                rid_of_anchor=rid_of, source="enedis", confidence=0.95, pos_uncertainty_m=MV_SNAP_M)
    mv_e = mv_e.assign(name="", voltage_kv=np.nan)

    # --- high voltage -------------------------------------------------------------------------------------
    hv = st.read("stg_elec_hv_lines_osm")
    hv = hv.assign(**{"class": "line_hv", "voltage_key": hv["voltage_kv"].fillna(-1.0).astype(float),
                      "aerial_share": ((hv["power"] == "line") & ~hv["location"].fillna("").isin(UNDERGROUND))
                      .astype(float)})
    hv_net = build_network(hv, hv_anchors, snap_m=HV_SNAP_M, split_m=HV_SPLIT_M, boundary=extent.buffer,
                           same=("voltage_key",), weighted=("aerial_share",))
    hv_j, hv_e = network_frames(hv_net, "elec", junction_class="junction_hv", edge_class="line_hv",
                                rid_of_anchor=rid_of, source="osm", confidence=0.8, pos_uncertainty_m=HV_SNAP_M)
    hv_e = hv_e.assign(voltage_kv=hv_e["voltage_key"].where(hv_e["voltage_key"] > 0), name="").drop(columns="voltage_key")

    # --- gas-fired plants: sources linked to the nearest substation ----------------------------------------
    plants = st.read("stg_gas_power_plants")
    gens = pd.DataFrame({
        "rid": [make_rid("elec", "generator", "odre", _slug(s)) for s in plants["site"]], "class": "generator",
        "name": plants["site"].to_numpy(), "operator": plants["operator"].to_numpy(), "is_source": True,
        "status": "in_service", "pos_uncertainty_m": 100.0, "confidence": 0.9, "primary_source": "odre.gas_power_plants",
        "geometry": plants.geometry.to_numpy()})
    grid_subs = subs[subs["class"].isin(["sub_tx", "sub_primary"])]
    links = []
    if len(gens) and len(grid_subs):
        tree = shapely.STRtree(grid_subs.geometry.to_numpy())
        gi, si = tree.query_nearest(gens.geometry.to_numpy(), max_distance=GENERATOR_LINK_M, all_matches=False)
        for g, s in zip(gi, si, strict=True):
            a, b = gens.geometry.iat[g], grid_subs.geometry.iat[s]
            links.append({"rid": make_rid("elec", "line_hv", "odre", gens["rid"].iat[g].split(":")[2]), "class": "line_hv",
                          "name": "Plant connection (derived)", "src_rid": gens["rid"].iat[g],
                          "dst_rid": grid_subs["rid"].iat[s], "length_m": float(a.distance(b)),
                          "voltage_kv": grid_subs["voltage_kv"].iat[s], "confidence": 0.5,
                          "primary_source": "derived", "geometry": shapely.LineString([a, b])})

    mvlv_nodes = mvlv.assign(**{"class": "sub_mv_lv", "operator": "Enedis", "pos_uncertainty_m": MV_LV_RADIUS_M,
                                "confidence": 0.95, "primary_source": "enedis.poste_electrique"})
    sub_nodes = subs.drop(columns=["key", "osm_ids", "code_poste", "transition", "may_feed_mv"])
    nodes = node_frame(pd.concat([sub_nodes, gens, mvlv_nodes.drop(columns=["enedis_id", "code_commune", "in_core"]),
                                  hv_j, mv_j], ignore_index=True).assign(layer="elec"), extent)
    edges = edge_frame(pd.concat([hv_e.drop(columns=["n_segments"]), mv_e.drop(columns=["n_segments"]),
                                  pd.DataFrame(links)], ignore_index=True).assign(layer="elec"), extent)

    xref = pd.concat([
        pd.DataFrame({"rid": subs["rid"], "source_id": "osm", "source_key": subs["osm_ids"]}).assign(
            source_key=lambda d: d["source_key"].str.split(";")).explode("source_key"),
        pd.DataFrame({"rid": subs.loc[subs["code_poste"].notna(), "rid"], "source_id": "rte.postes",
                      "source_key": subs.loc[subs["code_poste"].notna(), "code_poste"]}),
        pd.DataFrame({"rid": mvlv["rid"], "source_id": "enedis.poste_electrique", "source_key": mvlv["enedis_id"]}),
        pd.DataFrame({"rid": gens["rid"], "source_id": "odre.gas_power_plants", "source_key": plants["site"].to_numpy()}),
    ], ignore_index=True)

    qa = _qa(st, extent, mvlv, mv_net, hv_net, subs, primary_keys, feeders, qa_match, len(links), len(gens))
    return SystemBuild(nodes=nodes, edges=edges, xref=xref, qa=qa)


def _qa(st: Staged, extent: Extent, mvlv: gpd.GeoDataFrame, mv_net: Network, hv_net: Network, subs: pd.DataFrame,
        primary_keys: set[str], feeders: pd.Series, qa_match: dict[str, Any], n_links: int, n_gens: int) -> dict[str, Any]:
    from scipy.sparse import coo_array
    from scipy.sparse.csgraph import connected_components

    v, e = mv_net.vertices, mv_net.edges
    n = len(v)
    _, lab = connected_components(coo_array((np.ones(len(e)), (e["u"], e["v"])), shape=(n, n)), directed=False)
    is_primary = v["anchor"].isin(primary_keys).to_numpy()
    fed = np.zeros(lab.max() + 1 if n else 0, dtype=bool)
    fed[lab[is_primary]] = True
    reach = dict(zip(v["anchor"], fed[lab], strict=True))
    core = mvlv[mvlv["in_core"]]
    attached = core["rid"].isin(set(v["anchor"].dropna()))
    reachable = np.array([bool(reach.get(r, False)) for r in core["rid"]], dtype=bool)
    mid = shapely.line_interpolate_point(e.geometry.to_numpy(), 0.5, normalized=True)
    core_e = e[shapely.contains_xy(extent.core, shapely.get_x(mid), shapely.get_y(mid))]  # edges by their midpoint
    totals = st.read("stg_elec_enedis_totals")
    latest = totals.sort_values("year").groupby("departement").tail(1)
    km_core = {c: round(float(core_e.loc[core_e["class"] == c, "length_m"].sum()) / 1000, 1)
               for c in ("line_mv_aerial", "cable_mv")}
    ref_km = float(latest["km_total"].sum())
    ref_subs = int(latest["mv_lv_substations"].sum())
    return {
        "rte_matching": qa_match,
        "hv": {**hv_net.stats, "substations_by_class": subs["class"].value_counts().sort_index().to_dict(),
               "generators": n_gens, "generators_linked": n_links},
        "mv": {**mv_net.stats, "primary_substations": len(primary_keys),
               "feeders_per_primary_median": float(np.median([feeders.get(k, 0) for k in primary_keys]))
               if primary_keys else None,
               "core_mv_lv_substations": len(core), "core_attached_share": round(float(attached.mean()), 4),
               "core_reachable_from_primary_share": round(float(reachable.mean()), 4),
               "core_km_by_class": km_core,
               "core_km": round(sum(km_core.values()), 1),
               "core_km_vs_enedis_total": round(sum(km_core.values()) / ref_km, 4) if ref_km else None,
               "enedis_reference": {"year": latest["year"].tolist(), "km_total": round(ref_km, 1),
                                    "mv_lv_substations": ref_subs},
               "core_mv_lv_substations_vs_enedis_count": round(len(core) / ref_subs, 4) if ref_subs else None},
    }



__all__ = ["CRS", "SystemBuild", "build_elec", "hv_substations"]
