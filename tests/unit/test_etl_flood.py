"""D5 flood hazard: TRI archives (COVADIS shapefiles) staged as hazard scenarios and depth footprints - copies of a
TRI across département archives read once, invalid polygons repaired, the open depth class bounded, and extents
without depth classes filled with an explicit unknown band. Offline, against the mock portal."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest

from resilink.etl.adapters.georisques import OPEN_CLASS_DEPTH_M, UNKNOWN_DEPTH_M, scenario_id
from resilink.etl.contracts import load_sources
from resilink.etl.snapshot import fetch_snapshot
from resilink.etl.staging import stage
from tests.etl_fixtures import FLOOD_SOURCES, TERR, TERRITORY_SOURCES, fetch_all


def test_scenario_ids_follow_flood_type_and_return_period():
    assert scenario_id("01", "02moy") == "tri2020_river_rp100"
    assert scenario_id("03", "03mcc") == "tri2020_sea_rp100cc" and scenario_id("01", "04fai") == "tri2020_river_rp1000"


def test_archive_of_layers_is_inspected_as_a_list_of_layer_files(portal, tmp_path: Path):
    snap, _ = fetch_snapshot(load_sources()["georisques.tri2020_13"], TERR, root=tmp_path)
    names = [f.name for f in snap.metadata.fields]
    assert all(f.type == "member" for f in snap.metadata.fields)
    assert "tri_2020_sig_di_13/FRD_TRI_TEST/n_tri_test_iso_ht_01_02moy_s_013.shp" in names


def test_flood_adapter_stages_scenarios_and_depth_footprints(portal, tmp_path: Path):
    fetch_all(tmp_path, TERRITORY_SOURCES)
    stage("ign.admin_express", TERR, root=tmp_path)
    fetch_all(tmp_path, FLOOD_SOURCES)
    first = {r.table: r for r in stage("georisques.tri2020", TERR, root=tmp_path)}
    staging = tmp_path / "staging" / "tst"

    scen = pd.read_parquet(staging / "hazard_scenarios.parquet").set_index("scenario_id")
    assert sorted(scen.index) == ["tri2020_river_rp10", "tri2020_river_rp100", "tri2020_river_rp1000", "tri2020_sea_rp100"]
    assert scen.loc["tri2020_river_rp100", "return_period_y"] == 100.0 and scen.loc["tri2020_sea_rp100", "set_id"] == "tri2020_sea"
    assert scen.loc["tri2020_river_rp10", "annual_rate"] == pytest.approx(0.1)

    foot = gpd.read_parquet(staging / "hazard_footprints.parquet")
    by = foot.groupby("scenario_id")
    assert by.size().to_dict() == {"tri2020_river_rp10": 2, "tri2020_river_rp100": 2, "tri2020_river_rp1000": 2,
                                   "tri2020_sea_rp100": 1}  # the shared TRI counted once; the far TRI dropped
    assert foot.geometry.is_valid.all()  # the self-crossing polygon was repaired
    rp100 = foot[foot["scenario_id"] == "tri2020_river_rp100"].set_index("band")
    assert rp100.loc["2+", "im_max"] == OPEN_CLASS_DEPTH_M and rp100.loc["2+", "depth_open_ended"]
    assert rp100.loc["0-0.5", "im_max"] == 0.5
    rp1000 = foot[foot["scenario_id"] == "tri2020_river_rp1000"].set_index("band")
    assert rp1000.loc["unknown", "im_max"] == UNKNOWN_DEPTH_M and not rp1000.loc["unknown", "depth_known"]
    assert rp1000.loc["unknown"].geometry.area == pytest.approx(2_500 * 3_000, rel=1e-6)  # the half without depths

    qa = first["hazard_footprints"].qa
    # the shared TRI is in 2 archives (2 layers), the far TRI in 3 (1 layer): 2 + 2 copies skipped
    assert qa["tri_layer_copies_skipped"] == 4 and qa["geometries_repaired"] >= 1
    assert qa["unknown_depth_km2_added"] == {"tri2020_river_rp1000|FRD_TRI_TEST": 7.5}
    assert qa["depth_coverage_of_extent_in_buffer"]["tri2020_river_rp100|FRD_TRI_TEST"] == pytest.approx(1.0)
    per = gpd.read_parquet(staging / "stg_hazard_tri_perimeters.parquet")
    assert sorted(per["tri"]) == ["FRD_TRI_SHARED", "FRD_TRI_TEST"]  # rows are stored in Hilbert (spatial) order
    assert 0 < first["stg_hazard_tri_perimeters"].qa["core_share_in_tri_perimeters"] <= 1

    again = {r.table: r for r in stage("georisques.tri2020", TERR, root=tmp_path)}
    assert {t: r.sha256 for t, r in again.items()} == {t: r.sha256 for t, r in first.items()}  # byte-identical
