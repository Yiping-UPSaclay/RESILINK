"""Release build (D6): a small staged territory with connected networks of all three systems -> one release.

Layout (Lambert-93 offsets from X0, Y0; core 10 km x 10 km, buffer 2 km):

* electricity: a 225 kV line enters from the west (cut at the buffer: a source) to transmission substation A (RTE
  code AAAAA), a 63 kV line to substation B where three MV feeders end (B is a primary substation); feeder S3-S4
  runs through a plain vertex (contracted). S5 (in the buffer) sits on an MV line connected to nothing (dropped). Plant G links to B.
* gas: NaTran's trace enters from the west (cut: a source) to the LNG terminal L; delivery station D lies 100 m
  off the trace (a derived connector); one GRDF pipe crosses cell C1.
* telecom: exchange E (a source); Orange and SFR sites near S1 are linked to E.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import shapely
from shapely.geometry import LineString, Point, box

from resilink.domain.config import TerritoryConfig
from resilink.domain.schemas import EDGE_SCHEMA, NODE_SCHEMA
from resilink.etl.release import build_release, publish
from resilink.etl.staging import StagedTable
from resilink.io import geoparquet as gpq
from resilink.io.hashing import canonical_json, sha256_file
from resilink.io.release import load_release
from resilink.registry import system_catalog

X0, Y0 = 900_000.0, 6_250_000.0
TERR = TerritoryConfig(id="mini", name="Mini", selector={"departement": ["13"]}, buffer_m=2000.0)
CELLS = {"C1": (5000, 1000), "C2": (6000, 6000), "C3": (6800, 5000), "C4": (6000, 3200)}  # cell south-west corners


def pt(x: float, y: float) -> Point:
    return Point(X0 + x, Y0 + y)


def ln(*xy: tuple[float, float]) -> LineString:
    return LineString([(X0 + x, Y0 + y) for x, y in xy])


def gdf(rows: list[dict]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:2154")


def staged_tables(unmatched_rte: bool = False) -> dict[str, gpd.GeoDataFrame | pd.DataFrame]:
    core, grown = box(X0, Y0, X0 + 10_000, Y0 + 10_000), box(X0 - 2_000, Y0 - 2_000, X0 + 12_000, Y0 + 12_000)
    sub = lambda osm_id, kind, kv, x, y, ref="": {  # noqa: E731
        "osm_id": osm_id, "osm_type": "way", "substation": kind, "voltage_kv": kv, "voltages": str(int(kv * 1000)),
        "voltage_class": str(int(kv)), "operator": "RTE", "name": "", "ref": "", "ref_fr_rte": ref,
        "footprint_m2": 2500.0, "reach_m": 35.0, "in_core": True, "source": "osm", "geometry": pt(x, y)}
    mvlv = {f"S{i}": xy for i, xy in enumerate([(6000, 6000), (7000, 5000), (6000, 4000), (6000, 3000),
                                                 (11000, 9000)], start=1)}
    rte = [{"code_poste": "AAAAA", "nom_poste": "ALPHA", "fonction": "Poste de transformation", "etat": "EN EXPLOITATION",
            "tension": "225kV", "tension_kv": 225.0, "departement": "Bouches-du-Rhône", "departement_code": "13",
            "in_core": True, "osm_ids_by_ref": "wA"}]
    if unmatched_rte:
        rte.append({**rte[0], "code_poste": "ZZZZZ", "nom_poste": "ZULU", "osm_ids_by_ref": ""})
    return {
        "territory_extent": gdf([{"extent_id": "mini:core", "kind": "core", "buffer_m": 0.0, "geometry": core},
                                 {"extent_id": "mini:buffer", "kind": "buffer", "buffer_m": 2000.0, "geometry": grown}]),
        "admin_areas": gdf([{"admin_id": "admin.departement:cog2026:13", "level": "departement", "code": "13",
                             "name": "Mini", "cog_year": 2026, "in_core": True, "geometry": core}]),
        "grid_cells": gdf([{"cell_id": c, "k": np.uint32(i), "pop": 100.0, "admin_commune": "13001", "in_core": True,
                            "geometry": box(X0 + x, Y0 + y, X0 + x + 200, Y0 + y + 200)}
                           for i, (c, (x, y)) in enumerate(CELLS.items())]
                          + [{"cell_id": "OUT", "k": np.uint32(9), "pop": 50.0, "admin_commune": "84001", "in_core": False,
                              "geometry": box(X0 + 11_000, Y0, X0 + 11_200, Y0 + 200)}]),
        "stg_elec_substations_osm": gdf([sub("wA", "transmission", 225.0, 2000, 5000, "AAAAA"),
                                         sub("wB", "distribution", 63.0, 6000, 5000)]),
        "stg_elec_rte_sites": pd.DataFrame(rte),
        "stg_elec_hv_lines_osm": gdf([
            {"osm_id": "w1", "power": "line", "voltage_kv": 225.0, "location": "", "geometry": ln((-3000, 5000), (1980, 5000))},
            {"osm_id": "w2", "power": "line", "voltage_kv": 63.0, "location": "", "geometry": ln((2020, 5000), (5980, 5000))}]),
        "stg_elec_mv_lines": gdf([
            {"aerial": True, "geometry": ln((6000, 5030), (6000, 5998.5))},  # feeder to S1
            {"aerial": False, "geometry": ln((6030, 5000), (6998.5, 5000))},  # feeder to S2
            {"aerial": False, "geometry": ln((6000, 4970), (6000, 4001.5))},  # feeder to S3
            {"aerial": False, "geometry": ln((6000, 3998.5), (6000, 3500))},  # S3 - plain vertex - S4
            {"aerial": False, "geometry": ln((6000, 3500.5), (6000, 3001.5))},
            {"aerial": True, "geometry": ln((10500, 9000), (10998.5, 9000))}]),  # S5 (buffer): its line reaches nothing
        "stg_elec_mv_lv_substations": gdf([{"rid": f"elec.sub_mv_lv:fp:p{int(X0 + x)}_{int(Y0 + y)}", "enedis_id": k,
                                            "code_commune": "13001", "in_core": x <= 10_000, "geometry": pt(x, y)}
                                           for k, (x, y) in mvlv.items()]),
        "stg_elec_enedis_totals": pd.DataFrame([{"departement": "13", "year": 2025, "km_total": 4.0,
                                                 "mv_lv_substations": 5}]),
        "stg_gas_power_plants": gdf([{"site": "Plant G", "operator": "EDF", "geometry": pt(6100, 5200)}]),
        "stg_gas_transmission_natran": gdf([{"natran_object_id": 1, "geometry": ln((-3000, 1000), (8000, 1000))}]),
        "stg_gas_facilities_osm": gdf([
            {"osm_id": "wD", "kind": "station", "name": "Poste D", "operator": "NaTran", "footprint_m2": 400.0,
             "reach_m": 15.0, "geometry": pt(5000, 1100)},
            {"osm_id": "wL", "kind": "lng_terminal", "name": "Terminal L", "operator": "Elengy",
             "footprint_m2": 90_000.0, "reach_m": 200.0, "geometry": pt(8150, 1000)}]),
        "stg_gas_pipelines_osm": gdf([{"osm_id": "w9", "geometry": ln((0, 1010), (8000, 1010))}]),
        "stg_gas_distribution_lines": gdf([{"propane": False, "geometry": ln((5050, 900), (5050, 1300))},
                                           {"propane": True, "geometry": ln((6050, 5900), (6050, 6300))}]),
        "stg_telecom_mobile_sites": gdf([
            {"rid": "telecom.mobile_site:anfr:0130000001-orange", "operator": "Orange", "num_site": "O1",
             "id_station_anfr": "0130000001", "geometry": pt(6200, 6200)},
            {"rid": "telecom.mobile_site:anfr:0130000002-sfr", "operator": "SFR", "num_site": "S1",
             "id_station_anfr": "0130000002", "geometry": pt(6300, 6000)}]),
        "stg_telecom_exchanges_osm": gdf([
            {"osm_id": "wE", "telecom": "exchange", "name": "Central E", "operator": "Orange", "geometry": pt(5000, 6000)},
            {"osm_id": "wX", "telecom": "data_center", "name": "DC", "operator": "", "geometry": pt(5500, 6000)}]),
        "hazard_scenarios": pd.DataFrame([{"scenario_id": "s_rp100", "hazard": "flood", "set_id": "s",
                                           "return_period_y": 100.0, "annual_rate": 0.01, "im_type": "depth_m"}]),
        "hazard_footprints": gdf([{"scenario_id": "s_rp100", "band": "1-2", "im_min": 1.0, "im_max": 2.0,
                                   "geometry": box(X0 + 5800, Y0 + 3800, X0 + 6200, Y0 + 4200)}]),
    }


def stage_mini(root: Path, **kw) -> Path:
    d = root / "staging" / TERR.id
    d.mkdir(parents=True, exist_ok=True)
    for table, frame in staged_tables(**kw).items():
        path = d / f"{table}.parquet"
        if isinstance(frame, gpd.GeoDataFrame):
            gpq.write_canonical(frame, path, table=table, release_id="staging:mini", schema_version="1.0.0")
        else:
            gpq.write_table(frame, path)
        qa = {"insee_pop_core_by_dominant_commune": 400.0} if table == "grid_cells" else {}
        rec = StagedTable(table=table, territory=TERR.id, path=path.name, rows=len(frame), sha256=sha256_file(path),
                          sensitivity="internal", adapter="test@1", snapshots=[f"src.{table}@20261128T000000Z#abc"],
                          licences={f"src.{table}": "Licence Ouverte 2.0"}, qa=qa, staged_at=datetime.now(UTC))
        (d / f"{table}.json").write_text(canonical_json(rec), encoding="utf-8")
    return d


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("mini")
    stage_mini(root)
    return root, build_release(TERR, "2027.01.0", root=root)


def test_networks_are_matched_noded_and_pruned(built):
    _, b = built
    nodes, edges = b.tables["nodes"][0].set_index("rid"), b.tables["edges"][0]
    assert nodes.loc["elec.sub_tx:rte:AAAAA", "name"] == "ALPHA"  # RTE identity through ref:FR:RTE
    assert nodes.loc["elec.sub_primary:osm:wB", "class"] == "sub_primary"  # three MV feeders end there
    boundary = nodes[(nodes["class"] == "junction_hv") & nodes["is_source"]]
    assert len(boundary) == 1 and boundary.geometry.iloc[0].x == pytest.approx(X0 - 2000)
    assert nodes.loc["elec.generator:odre:plant_g", "is_source"]
    s3, s4 = f"elec.sub_mv_lv:fp:p{int(X0 + 6000)}_{int(Y0 + 4000)}", f"elec.sub_mv_lv:fp:p{int(X0 + 6000)}_{int(Y0 + 3000)}"
    chain = edges[(edges["src_rid"].isin([s3, s4])) & (edges["dst_rid"].isin([s3, s4]))]
    assert len(chain) == 1 and chain["length_m"].iat[0] == pytest.approx(997.0, abs=1)  # contracted through 6000,3500
    assert f"elec.sub_mv_lv:fp:p{int(X0 + 11000)}_{int(Y0 + 9000)}" not in nodes.index  # cut off: dropped
    assert b.qa["report"]["baseline"]["nodes_dropped"] == {"elec.junction_mv": 1, "elec.sub_mv_lv": 1}
    assert {"gas.lng_terminal:osm:wL", "gas.delivery_station:osm:wD"} <= set(nodes.index)
    connector = edges[edges["primary_source"] == "derived"]
    assert {"Station connector (derived)", "Plant connection (derived)", "Backhaul to nearest exchange (logical)"} \
        == set(connector["name"])
    assert "telecom.exchange:osm:wX" not in nodes.index  # data centres are not exchanges
    NODE_SCHEMA.validate(b.tables["nodes"][0])
    EDGE_SCHEMA.validate(b.tables["edges"][0])
    assert system_catalog().problems(b.tables["nodes"][0], b.tables["edges"][0]) == []


def test_service_links_follow_the_proxies(built):
    _, b = built
    links = b.tables["service_links"][0]
    assert set(b.tables["grid_cells"][0]["cell_id"]) == set(CELLS)  # core cells only, dense k
    assert list(b.tables["grid_cells"][0]["k"]) == [0, 1, 2, 3]
    elec = links[links["sector"] == "elec"].set_index("cell_id")["supplier_rid"]
    assert elec["C2"] == f"elec.sub_mv_lv:fp:p{int(X0 + 6000)}_{int(Y0 + 6000)}"
    gas = links[links["sector"] == "gas"]
    assert list(gas["cell_id"]) == ["C1"] and gas["supplier_rid"].iat[0] == "gas.delivery_station:osm:wD"
    tel = links[links["sector"] == "telecom"]
    assert len(tel) == 2 * len(CELLS) and set(tel["share"]) == {0.5}  # two operators, equal shares


def test_gates_pass_and_the_release_publishes_and_reproduces(built, tmp_path):
    root, b = built
    assert b.passed, [g for g in b.qa["gates"] if not g["pass"]]
    rel = publish(b, root=root)
    loaded = load_release("mini-2027.01.0", root)
    assert loaded.manifest.qa["status"] == "pass"
    assert {"nodes", "edges", "grid_cells", "service_links", "entity_xref", "hazard_footprints"} <= set(loaded.manifest.tables)
    assert {s["source"] for s in loaded.manifest.snapshots} >= {"src.stg_elec_mv_lines", "src.grid_cells"}
    again = build_release(TERR, "2027.01.0", root=root)
    rel2 = publish(again, root=tmp_path)
    assert rel2.manifest.content_hash() == rel.manifest.content_hash()  # byte-identical tables
    with pytest.raises(FileExistsError):
        publish(again, root=root)  # releases are immutable


def test_a_failed_gate_blocks_publication(tmp_path):
    stage_mini(tmp_path, unmatched_rte=True)
    b = build_release(TERR, "2027.01.0", root=tmp_path)
    gate = {g["id"]: g for g in b.qa["gates"]}["rte_substations_in_osm"]
    assert gate["value"] == 0.5 and not gate["pass"] and b.qa["status"] == "fail"
    with pytest.raises(ValueError, match="QA gates failed: rte_substations_in_osm"):
        publish(b, root=tmp_path)
    assert not (tmp_path / "releases" / "mini").exists()


def test_dependency_candidates_are_reported_per_rule(built):
    _, b = built
    cand = b.qa["report"]["dependency_candidates"]
    assert cand["R-TEL-01"]["with_candidate_share"] == 1.0 and cand["R-TEL-03"] == {"core_dependents": 0}
    assert shapely.equals(b.tables["territory_extent"][0].geometry.iloc[0], box(X0, Y0, X0 + 10_000, Y0 + 10_000))


def test_every_adapter_feeds_the_build_from_the_mock_portal(portal, tmp_path):
    """Offline end to end: fetch -> stage (all adapters) -> build. The fixture networks are not coherent (its lines
    meet nothing), so gates fail and publication is refused, but every staged table matches what the build reads."""
    from resilink.etl.adapters import ADAPTERS
    from resilink.etl.staging import ordered, stage
    from tests.etl_fixtures import (
        ELECTRICITY_SOURCES,
        FLOOD_SOURCES,
        GAS_SOURCES,
        TELECOM_SOURCES,
        TERRITORY_SOURCES,
        fetch_all,
    )
    from tests.etl_fixtures import TERR as PORTAL_TERR

    fetch_all(tmp_path, TERRITORY_SOURCES)
    stage("ign.admin_express", PORTAL_TERR, root=tmp_path)
    fetch_all(tmp_path, tuple(dict.fromkeys(ELECTRICITY_SOURCES + TELECOM_SOURCES + GAS_SOURCES + FLOOD_SOURCES)))
    for a in ordered(ADAPTERS):
        stage(a.id, PORTAL_TERR, root=tmp_path)
    b = build_release(PORTAL_TERR, "2027.01.0", root=tmp_path)
    assert set(b.tables) >= {"nodes", "edges", "grid_cells", "service_links", "hazard_footprints"}
    assert b.qa["report"]["elec"]["mv"]["enedis_reference"]["mv_lv_substations"] == 3  # 2025 row of département 13
    assert b.qa["status"] == "fail"
    with pytest.raises(ValueError, match="QA gates failed"):
        publish(b, root=tmp_path)
