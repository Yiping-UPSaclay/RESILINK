"""D2 electricity: Data Fair paging and territory window, Geofabrik checksum, OSM PBF header, and the Enedis, OSM
and RTE adapters, run offline against the mock portal of ``tests.etl_fixtures``."""

from __future__ import annotations

import math
from pathlib import Path

import geopandas as gpd
import httpx
import pandas as pd
import pytest

from resilink.etl import connectors as conn_mod
from resilink.etl.adapters import ADAPTERS
from resilink.etl.adapters.osm_power import max_kv, voltage_class
from resilink.etl.adapters.rte import name_key, tension_kv
from resilink.etl.contracts import load_sources
from resilink.etl.pbf import read_header
from resilink.etl.snapshot import extent_window, fetch_snapshot, raw_dir
from resilink.etl.staging import stage
from tests.etl_fixtures import ELECTRICITY_SOURCES, TERR, TERRITORY_SOURCES, fetch_all
from tests.osm_pbf_writer import write_pbf

ELECTRICITY_ADAPTERS = ("ign.admin_express", "insee.filosofi2021", "enedis.network", "osm.power", "rte.sites")


# ------------------------------------------------------------------------------------------ pure helpers
def test_voltage_parsing():
    assert max_kv("225000;63000") == 225.0 and max_kv("400000") == 400.0
    assert math.isnan(max_kv("")) and math.isnan(max_kv("medium")) and math.isnan(max_kv(None))
    assert [voltage_class(v) for v in (63.0, 90.0, 225.0, 380.0, 20.0, float("nan"))] == [
        "63", "90", "225", "400", "<45", "unknown"]
    assert tension_kv("225kV") == 225.0 and tension_kv("<45kV") is None and tension_kv("COURANT CONTINU") is None
    assert name_key("Bouches-du-Rhône") == name_key("BOUCHES DU RHONE") == "bouches du rhone"


def test_pbf_header_gives_required_features_and_timestamp(tmp_path: Path):
    p = write_pbf(tmp_path / "x.pbf", [(1, 43.3, 5.4, {})], [], timestamp=1_795_000_000)
    h = read_header(p)
    assert h["required_features"] == ["OsmSchema-V0.6", "DenseNodes"]
    assert h["replication_timestamp"] == "2026-11-18T11:06:40+00:00" and h["writingprogram"] == "resilink-tests"


# ---------------------------------------------------------------------------------- connectors, offline
def test_datafair_pages_through_link_headers_within_the_territory_window(portal, tmp_path: Path):
    fetch_all(tmp_path, TERRITORY_SOURCES)
    stage("ign.admin_express", TERR, root=tmp_path)
    snap, _ = fetch_snapshot(load_sources()["enedis.poste_electrique"], TERR, root=tmp_path)
    assert snap.rows == 4  # all pages followed (page size 1 in the fixture)
    calls = [q for q in portal.seen if q["path"].endswith("/poste-electrique/lines")]
    expected = ",".join(f"{v:.5f}" for v in extent_window(TERR, tmp_path))
    assert len(calls) == 4 and {q["bbox"] for q in calls} == {expected}
    assert [q.get("after") for q in calls] == [None, "1", "2", "3"]


def test_datafair_refuses_a_truncated_download(portal, tmp_path: Path, monkeypatch):
    fetch_all(tmp_path, TERRITORY_SOURCES)
    stage("ign.admin_express", TERR, root=tmp_path)
    inner = portal.handler

    def no_links(request: httpx.Request) -> httpx.Response:
        r = inner(request)
        r.headers.pop("Link", None)
        return r

    monkeypatch.setattr(conn_mod, "_client", lambda: httpx.Client(transport=httpx.MockTransport(no_links)))
    with pytest.raises(ValueError, match="received 1 features, the portal announced 4"):
        fetch_snapshot(load_sources()["enedis.poste_electrique"], TERR, root=tmp_path)


def test_geofabrik_download_is_checked_against_its_md5(portal, tmp_path: Path):
    spec = load_sources()["osm.geofabrik.paca"]
    snap, _ = fetch_snapshot(spec, TERR, root=tmp_path)
    assert [f.name for f in snap.metadata.fields] == ["OsmSchema-V0.6", "DenseNodes"]
    assert snap.metadata.modified == "2026-11-18T11:06:40+00:00"
    portal.md5_override = "0" * 32
    with pytest.raises(ValueError, match="does not match the publisher"):
        fetch_snapshot(spec, TERR, root=tmp_path)
    assert len(list(raw_dir(spec.id, tmp_path).iterdir())) == 1  # the corrupt download was not kept


# -------------------------------------------------------------------------------------- adapters, offline
def test_electricity_adapters_stage_the_territory_networks(portal, tmp_path: Path):
    fetch_all(tmp_path, TERRITORY_SOURCES)
    stage("ign.admin_express", TERR, root=tmp_path)
    fetch_all(tmp_path, ELECTRICITY_SOURCES)
    first = {r.table: r for a in ELECTRICITY_ADAPTERS for r in stage(a, TERR, root=tmp_path)}
    staging = tmp_path / "staging" / "tst"

    subs = gpd.read_parquet(staging / "stg_elec_mv_lv_substations.parquet").set_index("enedis_id")
    assert sorted(subs.index) == ["s-buffer", "s-core"]  # duplicate position merged, outside point dropped
    assert subs.loc["s-core", "in_core"] and not subs.loc["s-buffer", "in_core"]
    assert subs.loc["s-core", "rid"].startswith("elec.sub_mv_lv:fp:p")
    assert first["stg_elec_mv_lv_substations"].qa["duplicate_positions_dropped"] == 1
    lines = gpd.read_parquet(staging / "stg_elec_mv_lines.parquet").set_index("enedis_id")
    assert sorted(lines.index) == ["a-core", "u-core"] and lines.loc["a-core", "aerial"] and not lines.loc["u-core", "aerial"]
    assert lines.loc["a-core", "length_m"] == pytest.approx(500, rel=2e-3)
    assert first["stg_elec_mv_lines"].qa["aerial_share_core"] == pytest.approx(500 / 900, rel=5e-3)

    hv = gpd.read_parquet(staging / "stg_elec_hv_lines_osm.parquet").set_index("osm_id")
    assert sorted(hv.index) == ["w10", "w12"]  # 20 kV line and the line outside the buffer dropped; w10 once
    assert hv.loc["w10", "voltage_class"] == "225" and hv.loc["w12", "voltage_class"] == "unknown"
    assert hv.loc["w10", "source"] == "osm.geofabrik.paca" and hv.loc["w10", "length_m"] == pytest.approx(8000, rel=2e-3)
    assert first["stg_elec_hv_lines_osm"].qa["lines_below_45kv_dropped"] == 1
    assert first["stg_elec_hv_lines_osm"].licences == {s: "ODbL 1.0" for s in ADAPTERS["osm.power"].sources}
    hs = gpd.read_parquet(staging / "stg_elec_substations_osm.parquet").set_index("osm_id")
    assert sorted(hs.index) == ["n7", "w13"]  # the minor distribution substation is dropped
    assert hs.loc["n7", "ref_fr_rte"] == "AAAAA" and hs.loc["n7", "in_core"] and hs.loc["n7", "voltage_kv"] == 225.0
    assert hs.loc["w13", "osm_type"] == "way" and hs.loc["w13", "footprint_m2"] == pytest.approx(10_000, rel=5e-3)

    rte = pd.read_parquet(staging / "stg_elec_rte_sites.parquet").set_index("code_poste")
    assert sorted(rte.index) == ["AAAAA", "BBBBB", "DDDDD", "EEEEE"]  # Savoie dropped; names matched loosely
    assert rte.loc["DDDDD", "departement_code"] == "13" and rte.loc["BBBBB", "departement_code"] == "84"
    assert rte.loc["AAAAA", "osm_ids_by_ref"] == "n7" and rte.loc["BBBBB", "osm_ids_by_ref"] == "w13"
    qa = first["stg_elec_rte_sites"].qa
    assert (qa["core_substations_in_service"], qa["core_substations_matched_by_osm_ref"]) == (2, 1)
    assert qa["core_substations_unmatched"] == ["EEEEE"]

    again = {r.table: r for a in ELECTRICITY_ADAPTERS for r in stage(a, TERR, root=tmp_path)}
    assert {t: r.sha256 for t, r in again.items()} == {t: r.sha256 for t, r in first.items()}  # byte-identical
