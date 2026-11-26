"""D1 territory and population: WFS and static-file connectors, drift gate on inspected files, and the
ADMIN EXPRESS / Filosofi adapters, run offline against a mock HTTP server and a small fixture territory."""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import geopandas as gpd
import httpx
import numpy as np
import pytest
import shapely
from pyproj import Transformer
from shapely.geometry import box, mapping

from resilink.domain.config import TerritoryConfig
from resilink.etl import connectors as conn_mod
from resilink.etl.adapters import ADAPTERS
from resilink.etl.adapters.filosofi import cell_box, departement_of, lcog_communes
from resilink.etl.connectors import _xsd_fields
from resilink.etl.contracts import load_sources
from resilink.etl.snapshot import DriftError, fetch_snapshot, raw_dir
from resilink.etl.staging import list_staged, ordered, stage

TERR = TerritoryConfig(id="tst", name="Test", selector={"departement": ["13"], "cog_year": 2026}, buffer_m=2000.0,
                       crs_metric="EPSG:2154", grid={"id": "insee200m", "crs": "EPSG:3035", "size_m": 200})
X0, Y0 = 890_000.0, 6_240_000.0  # Lambert-93, east of Marseille
DEPT = box(X0, Y0, X0 + 10_000, Y0 + 10_000)
COMMUNES = {  # code: (département, population, geometry)
    "13001": ("13", 1000, box(X0, Y0, X0 + 5_000, Y0 + 10_000)),
    "13002": ("13", 2000, box(X0 + 5_000, Y0, X0 + 10_000, Y0 + 10_000)),
    "84001": ("84", 500, box(X0 + 10_000, Y0, X0 + 15_000, Y0 + 10_000)),  # meets the 2 km buffer
    "84002": ("84", 700, box(X0 + 20_000, Y0, X0 + 25_000, Y0 + 10_000)),  # 10 km away: dropped
}
XSD = """<?xml version="1.0" encoding="UTF-8"?>
<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:gml="http://www.opengis.net/gml/3.2"
  xmlns:x="urn:x" targetNamespace="urn:x"><xsd:complexType name="communeType"><xsd:complexContent>
  <xsd:extension base="gml:AbstractFeatureType"><xsd:sequence>
  <xsd:element name="code_insee" nillable="true" type="xsd:string"/>
  <xsd:element name="population" nillable="true" type="xsd:int"/>
  <xsd:element name="geometrie" nillable="true" type="gml:MultiSurfacePropertyType"/>
  </xsd:sequence></xsd:extension></xsd:complexContent></xsd:complexType>
  <xsd:element name="commune" substitutionGroup="gml:AbstractFeature" type="x:communeType"/></xsd:schema>"""
CRS_2154 = {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::2154"}}
HEADER = "idcar_200m,idcar_1km,idcar_nat,i_est_200,i_est_1km,lcog_geo,ind,men,ind_80p"


def _features() -> tuple[list[dict], list[dict]]:
    deps = [{"type": "Feature", "geometry": mapping(DEPT), "properties": {
        "code_insee": "13", "nom_officiel": "Bouches-du-Rhône", "code_insee_de_la_region": "93"}}]
    communes = [{"type": "Feature", "geometry": mapping(geom), "properties": {
        "code_insee": code, "nom_officiel": f"Commune {code}", "statut": "Commune simple", "population": pop,
        "date_du_recensement": "2023-01-01Z", "code_insee_du_departement": dep}}
        for code, (dep, pop, geom) in COMMUNES.items()]
    return deps, communes


def _cells_csv(drop_column: str | None = None) -> str:
    """Inhabited 200 m cells (EPSG:3035) over the fixture area; lcog_geo = commune holding the centre."""
    to3035 = Transformer.from_crs(2154, 3035, always_xy=True)
    to2154 = Transformer.from_crs(3035, 2154, always_xy=True)
    xs, ys = to3035.transform([X0 - 3_000, X0 + 16_000], [Y0 - 3_000, Y0 + 13_000])
    rows = []
    for e in range(int(min(xs)) // 200 * 200, int(max(xs)), 200):
        for n in range(int(min(ys)) // 200 * 200, int(max(ys)), 200):
            cx, cy = to2154.transform(e + 100, n + 100)
            code = next((c for c, (_, _, g) in COMMUNES.items() if g.contains(shapely.Point(cx, cy))), "84002")
            rows.append([f"CRS3035RES200mN{n}E{e}", f"CRS3035RES1000mN{n // 1000 * 1000}E{e // 1000 * 1000}",
                         f"CRS3035RES2000mN{n // 2000 * 2000}E{e // 2000 * 2000}", str((e // 200) % 2), "0", code,
                         "1", "0.4", "0.1"])
    cols = HEADER.split(",")
    keep = [i for i, c in enumerate(cols) if c != drop_column]
    return "\n".join([",".join(cols[i] for i in keep)] + [",".join(r[i] for i in keep) for r in rows]) + "\n"


def _zip(drop_column: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("carreaux_200m_met.csv", _cells_csv(drop_column))
        z.writestr("carreaux_200m_mart.csv", HEADER + "\nCRS5490RES200mN1592600E728800,x,x,1,1,97226,1,0.6,0\n")
        z.writestr("carreaux_200m_reun.csv", HEADER + "\nCRS2975RES200mN7634200E359400,x,x,1,1,97412,19,6.9,0\n")
    return buf.getvalue()


@pytest.fixture
def portal(monkeypatch):
    """Mock Géoplateforme WFS + insee.fr behind the connectors' HTTP client; records the query parameters."""
    deps, communes = _features()
    state = {"seen": [], "zip": _zip()}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".zip"):
            return httpx.Response(200, content=state["zip"], headers={"Content-Type": "application/zip"})
        q = dict(request.url.params)
        state["seen"].append(q)
        if q["REQUEST"] == "DescribeFeatureType":
            return httpx.Response(200, content=XSD.encode())
        feats = deps if q["TYPENAMES"].endswith(":departement") else communes
        if q.get("RESULTTYPE") == "hits":
            return httpx.Response(200, text=f'<wfs:FeatureCollection numberMatched="{len(feats)}"/>')
        if "CQL_FILTER" in q:
            wanted = set(re.findall(r"'(\w+)'", q["CQL_FILTER"]))
            feats = [f for f in feats if f["properties"]["code_insee"] in wanted]
        start, count = int(q["STARTINDEX"]), int(q["COUNT"])
        return httpx.Response(200, json={"type": "FeatureCollection", "crs": CRS_2154,
                                         "features": feats[start:start + count]})

    monkeypatch.setattr(conn_mod, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(conn_mod.WfsConnector, "PAGE", 2)  # exercise paging
    return state


def _fetch_all(root: Path) -> None:
    specs = load_sources()
    for sid in ("ign.admin_express.departement", "ign.admin_express.commune", "insee.filosofi2021_200m"):
        fetch_snapshot(specs[sid], TERR, root=root)


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
def test_wfs_fetch_pages_and_windows_communes_on_the_buffered_departement(portal, tmp_path: Path):
    _fetch_all(tmp_path)
    commune_calls = [q for q in portal["seen"] if q.get("TYPENAMES", "").endswith(":commune")
                     and q.get("REQUEST") == "GetFeature" and "RESULTTYPE" not in q]
    assert {q["BBOX"] for q in commune_calls} == {"888000,6238000,902000,6252000,urn:ogc:def:crs:EPSG::2154"}
    assert [q["STARTINDEX"] for q in commune_calls] == ["0", "2", "4"]  # 4 communes, pages of 2
    assert all(q["SORTBY"] == "code_insee" and q["SRSNAME"] == "EPSG:2154" for q in commune_calls)
    dep_calls = [q for q in portal["seen"] if q.get("CQL_FILTER")]
    assert dep_calls and all(q["CQL_FILTER"] == "code_insee IN ('13')" for q in dep_calls)


def test_static_file_is_inspected_and_its_drift_blocks_publication(portal, tmp_path: Path):
    spec = load_sources()["insee.filosofi2021_200m"]
    snap, report = fetch_snapshot(spec, TERR, root=tmp_path)
    assert report is None and snap.metadata.raw["inspected"]
    assert "carreaux_200m_met.csv/idcar_200m" in {f.name for f in snap.metadata.fields}
    assert snap.rows == snap.metadata.count > 1000
    portal["zip"] = _zip(drop_column="i_est_200")  # the publisher drops a column from one of the three files
    with pytest.raises(DriftError) as exc:
        fetch_snapshot(spec, TERR, root=tmp_path)
    assert exc.value.report.removed_fields == ["carreaux_200m_met.csv/i_est_200"]
    entries = sorted(p.name for p in raw_dir(spec.id, tmp_path).iterdir())
    assert entries == [Path(snap.path).parent.name]  # nothing published, no temporary directory left


# -------------------------------------------------------------------------------------- adapters, offline
def test_adapters_build_the_territory_tables_reproducibly(portal, tmp_path: Path):
    _fetch_all(tmp_path)
    assert [a.id for a in ordered(ADAPTERS)] == ["ign.admin_express", "insee.filosofi2021"]
    first = {r.table: r for a in ordered(ADAPTERS) for r in stage(a.id, TERR, root=tmp_path)}
    staging = tmp_path / "staging" / "tst"

    extent = gpd.read_parquet(staging / "territory_extent.parquet").set_index("kind")
    assert extent.geometry["core"].area == pytest.approx(100e6) and extent.loc["buffer", "buffer_m"] == 2000.0

    admin = gpd.read_parquet(staging / "admin_areas.parquet").set_index("admin_id")
    assert sorted(admin.index) == ["admin.commune:cog2026:13001", "admin.commune:cog2026:13002",
                                   "admin.commune:cog2026:84001", "admin.departement:cog2026:13"]
    assert admin.loc["admin.departement:cog2026:13", "population"] == 3000
    assert not admin.loc["admin.commune:cog2026:84001", "in_core"]
    assert first["admin_areas"].qa["communes_core"] == 2 and first["admin_areas"].qa["communes_in_wfs_window"] == 4

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

    again = {r.table: r for a in ordered(ADAPTERS) for r in stage(a.id, TERR, root=tmp_path)}
    assert {t: r.sha256 for t, r in again.items()} == {t: r.sha256 for t, r in first.items()}  # byte-identical
    assert {s.table for s in list_staged("tst", tmp_path)} == {"admin_areas", "territory_extent", "grid_cells"}


def test_adapter_refuses_a_cog_edition_other_than_the_territory_s(portal, tmp_path: Path):
    _fetch_all(tmp_path)
    other = TERR.model_copy(update={"selector": {"departement": ["13"], "cog_year": 2025}})
    with pytest.raises(ValueError, match="expects COG 2025"):
        stage("ign.admin_express", other, root=tmp_path)
