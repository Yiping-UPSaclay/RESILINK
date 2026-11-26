"""D4 gas: Opendatasoft exports windowed with in_bbox and read from Parquet (WKB), and the NaTran, GRDF,
gas-fired power station and OSM gas adapters, run offline against the mock portal."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pytest

from resilink.etl.snapshot import extent_window
from resilink.etl.staging import stage
from tests.etl_fixtures import GAS_SOURCES, TERR, TERRITORY_SOURCES, fetch_all

GAS_ADAPTERS = ("natran.network", "grdf.network", "odre.gas_power_plants", "osm.gas")


def _stage_gas(tmp_path: Path) -> dict:
    fetch_all(tmp_path, TERRITORY_SOURCES)
    stage("ign.admin_express", TERR, root=tmp_path)
    fetch_all(tmp_path, GAS_SOURCES)
    return {r.table: r for a in GAS_ADAPTERS for r in stage(a, TERR, root=tmp_path)}


def test_opendatasoft_export_is_windowed_on_the_territory(portal, tmp_path: Path):
    _stage_gas(tmp_path)
    calls = [q for q in portal.seen if q["path"].endswith("/cartographie-du-reseau-grdf-en-service/exports/parquet")]
    lon0, lat0, lon1, lat1 = extent_window(TERR, tmp_path)
    assert [q.get("where") for q in calls] == [f"in_bbox(geo_point_2d, {lat0:.5f}, {lon0:.5f}, {lat1:.5f}, {lon1:.5f})"]
    natran = [q for q in portal.seen if q["path"].endswith("/trace-du-reseau-grt-250/exports/parquet")]
    assert natran and "where" not in natran[0]  # national file, clipped by the adapter


def test_gas_adapters_stage_networks_stations_and_plants(portal, tmp_path: Path):
    first = _stage_gas(tmp_path)
    staging = tmp_path / "staging" / "tst"

    natran = gpd.read_parquet(staging / "stg_gas_transmission_natran.parquet")
    assert natran["natran_object_id"].tolist() == ["1"] and natran["in_core"].all()
    assert natran["length_m"].iloc[0] == pytest.approx(8000, rel=2e-3) and natran["pos_uncertainty_m"].iloc[0] == 250.0

    grdf = gpd.read_parquet(staging / "stg_gas_distribution_lines.parquet")
    assert sorted(grdf["code_commune"]) == ["13001", "84001"] and not grdf["propane"].any()
    qa = first["stg_gas_distribution_lines"].qa
    assert (qa["core_communes_reached"], qa["core_communes"], qa["core_communes_not_reached"]) == (1, 2, ["13002"])
    assert qa["core_census_population_in_reached_communes"] == pytest.approx(1000 / 3000, abs=1e-4)

    plants = gpd.read_parquet(staging / "stg_gas_power_plants.parquet")
    assert plants["site"].tolist() == ["Plant A"] and plants["power_mw"].iloc[0] == 430.0  # latest year kept
    assert first["stg_gas_power_plants"].qa["reference_years"] == [2024, 2025]

    pipes = gpd.read_parquet(staging / "stg_gas_pipelines_osm.parquet").set_index("osm_id")
    assert pipes.index.tolist() == ["w65"] and pipes.loc["w65", "operator"] == "NaTran"
    assert pipes.loc["w65", "diameter"] == "600" and pipes.loc["w65", "in_core"]
    fac = gpd.read_parquet(staging / "stg_gas_facilities_osm.parquet").set_index("osm_id")
    assert sorted(fac.index) == ["n62", "n64", "w74", "w84"]  # the ethylene station is not gas
    assert fac.loc["w74", "kind"] == "distribution"  # a station mapped as an industrial area stays a station
    assert fac.loc["w84", "kind"] == "lng_terminal" and fac.loc["n62", "kind"] == "station"
    assert fac.loc["w84", "footprint_m2"] == pytest.approx(150_000, rel=5e-3)
    qa_pipes, qa_fac = first["stg_gas_pipelines_osm"].qa, first["stg_gas_facilities_osm"].qa
    assert qa_fac["lng_terminals"] == ["Terminal méthanier de Test"]
    assert qa_pipes["km_core_natran_osm_vs_trace"] == pytest.approx([8.0, 8.0], abs=0.1)

    again = {r.table: r for a in GAS_ADAPTERS for r in stage(a, TERR, root=tmp_path)}
    assert {t: r.sha256 for t, r in again.items()} == {t: r.sha256 for t, r in first.items()}  # byte-identical
