"""D1 territory and population: WFS and static-file connectors, drift gate on inspected files, and the
ADMIN EXPRESS / Filosofi adapters, run offline against a mock portal and a small fixture territory."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest

from resilink.etl.adapters.filosofi import cell_box, departement_of, lcog_communes
from resilink.etl.connectors import _xsd_fields
from resilink.etl.contracts import load_sources
from resilink.etl.snapshot import DriftError, fetch_snapshot, raw_dir
from resilink.etl.staging import list_staged, stage
from tests.etl_fixtures import DEPT, TERR, TERRITORY_SOURCES, XSD, cells_zip, fetch_all

TERRITORY_ADAPTERS = ("ign.admin_express", "insee.filosofi2021")


# ------------------------------------------------------------------------------------------ pure helpers
def test_inspire_cell_ids_give_the_south_west_corner():
    assert cell_box("CRS3035RES200mN2029400E4259000") == (3035, 4259000.0, 2029400.0, 4259200.0, 2029600.0)
    with pytest.raises(ValueError):
        cell_box("N2029400E4259000")


def test_lcog_codes_map_city_arrondissements_to_their_commune():
    assert lcog_communes("13211,13001") == {"13055", "13001"}
    assert lcog_communes("75120") == {"75056"} and lcog_communes("69381") == {"69123"}
    assert lcog_communes("13077") == {"13077"} and lcog_communes("") == set()
    assert departement_of("2A041") == "2A" and departement_of("97412") == "974"


def test_wfs_schema_comes_from_describe_feature_type():
    fields = _xsd_fields(XSD.encode())
    assert [(f.name, f.type) for f in fields] == [
        ("code_insee", "xsd:string"), ("population", "xsd:int"), ("geometrie", "gml:MultiSurfacePropertyType")]


# ---------------------------------------------------------------------------------- connectors, offline
def test_wfs_fetch_pages_and_windows_areas_on_the_buffered_departement(portal, tmp_path: Path):
    fetch_all(tmp_path, TERRITORY_SOURCES)
    window = "888000,6238000,902000,6252000,urn:ogc:def:crs:EPSG::2154"
    data_calls = [q for q in portal.seen if q.get("REQUEST") == "GetFeature" and "RESULTTYPE" not in q
                  and "BBOX" in q]
    assert {q["BBOX"] for q in data_calls} == {window}
    commune_calls = [q for q in data_calls if q["TYPENAMES"].endswith(":commune")]
    assert [q["STARTINDEX"] for q in commune_calls] == ["0", "2"]  # 3 communes in the window, pages of 2
    assert all(q["SORTBY"] == "code_insee" and q["SRSNAME"] == "EPSG:2154" for q in data_calls)
    territory_calls = [q for q in portal.seen if q.get("CQL_FILTER")]
    assert territory_calls and all(q["CQL_FILTER"] == "code_insee IN ('13')" for q in territory_calls)


def test_static_file_is_inspected_and_its_drift_blocks_publication(portal, tmp_path: Path):
    spec = load_sources()["insee.filosofi2021_200m"]
    snap, report = fetch_snapshot(spec, TERR, root=tmp_path)
    assert report is None and snap.metadata.raw["inspected"]
    assert "carreaux_200m_met.csv/idcar_200m" in {f.name for f in snap.metadata.fields}
    assert snap.rows == snap.metadata.count > 1000
    portal.zip = cells_zip(drop_column="i_est_200")  # the publisher drops a column from one of the three files
    with pytest.raises(DriftError) as exc:
        fetch_snapshot(spec, TERR, root=tmp_path)
    assert exc.value.report.removed_fields == ["carreaux_200m_met.csv/i_est_200"]
    entries = sorted(p.name for p in raw_dir(spec.id, tmp_path).iterdir())
    assert entries == [Path(snap.path).parent.name]  # nothing published, no temporary directory left


# -------------------------------------------------------------------------------------- adapters, offline
def test_adapters_build_the_territory_tables_reproducibly(portal, tmp_path: Path):
    fetch_all(tmp_path, TERRITORY_SOURCES)
    first = {r.table: r for a in TERRITORY_ADAPTERS for r in stage(a, TERR, root=tmp_path)}
    staging = tmp_path / "staging" / "tst"

    extent = gpd.read_parquet(staging / "territory_extent.parquet").set_index("kind")
    assert extent.geometry["core"].area == pytest.approx(100e6) and extent.loc["buffer", "buffer_m"] == 2000.0

    admin = gpd.read_parquet(staging / "admin_areas.parquet").set_index("admin_id")
    assert sorted(admin.index) == ["admin.commune:cog2026:13001", "admin.commune:cog2026:13002",
                                   "admin.commune:cog2026:84001", "admin.departement:cog2026:13",
                                   "admin.departement:cog2026:84"]
    assert admin.loc["admin.departement:cog2026:13", "population"] == 3000
    assert admin.loc["admin.departement:cog2026:84", "name"] == "Vaucluse"
    assert not admin.loc["admin.departement:cog2026:84", "in_core"]
    assert admin["population"].isna().sum() == 1  # buffer départements carry no population
    assert not admin.loc["admin.commune:cog2026:84001", "in_core"]
    qa_admin = first["admin_areas"].qa
    assert qa_admin["communes_core"] == 2 and qa_admin["communes_in_wfs_window"] == 3
    assert qa_admin["departements_buffer_only"] == ["84"]

    grid = gpd.read_parquet(staging / "grid_cells.parquet")
    assert grid.crs.to_epsg() == 2154 and grid["cell_id"].is_unique
    assert sorted(grid["k"]) == list(range(len(grid)))
    centres = grid.geometry.centroid
    assert extent.geometry["buffer"].buffer(1.0).contains(centres).all()  # centre inside the buffer
    assert (grid["in_core"] == DEPT.contains(centres)).all()
    inside = grid[grid["lcog_geo_2021"] != "84002"]
    assert (inside["admin_commune"] == inside["lcog_geo_2021"]).all()
    qa = first["grid_cells"].qa
    assert qa["source_member"] == "carreaux_200m_met.csv"
    assert qa["pop_core"] == pytest.approx(float(grid["in_core"].sum()))
    assert qa["pop_core_vs_insee"] == pytest.approx(1.0, abs=0.02)
    assert 0 < qa["imputed_cells_share"] < 1
    # 200 m squares after reprojection: Lambert-93 is conformal, scale ~1.0007 here, so areas read ~0.13 % larger
    assert np.isclose(grid.geometry.area, 40_000, rtol=3e-3).all()

    again = {r.table: r for a in TERRITORY_ADAPTERS for r in stage(a, TERR, root=tmp_path)}
    assert {t: r.sha256 for t, r in again.items()} == {t: r.sha256 for t, r in first.items()}  # byte-identical
    assert {s.table for s in list_staged("tst", tmp_path)} == {"admin_areas", "territory_extent", "grid_cells"}


def test_adapter_refuses_a_cog_edition_other_than_the_territory_s(portal, tmp_path: Path):
    fetch_all(tmp_path, TERRITORY_SOURCES)
    other = TERR.model_copy(update={"selector": {"departement": ["13"], "cog_year": 2025}})
    with pytest.raises(ValueError, match="expects COG 2025"):
        stage("ign.admin_express", other, root=tmp_path)
