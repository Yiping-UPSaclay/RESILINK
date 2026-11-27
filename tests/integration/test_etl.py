"""ETL: drift gate (pure) and live-portal contracts replayed from recorded cassettes (pytest-recording)."""

from __future__ import annotations

from pathlib import Path

import pytest

from resilink.etl import snapshot as snapmod
from resilink.etl.connectors import DataFairConnector, OpendatasoftConnector
from resilink.etl.contracts import FieldInfo, SourceMetadata, load_sources
from resilink.etl.snapshot import DriftError, diff, fetch_snapshot

WITH_GEOM = SourceMetadata(title="Postes RTE", licence="Licence Ouverte 2.0", has_geometry=True, fields=[
    FieldInfo(name="code_poste", type="text"), FieldInfo(name="nom_poste", type="text"),
    FieldInfo(name="geo_point_2d", type="geo_point_2d"), FieldInfo(name="departement", type="text")])
WITHOUT_GEOM = SourceMetadata(title="Postes RTE", licence="Licence Ouverte 2.0", has_geometry=False, fields=[
    FieldInfo(name="code_poste", type="text"), FieldInfo(name="nom_poste", type="text"),
    FieldInfo(name="departement", type="text"), FieldInfo(name="fonction", type="text")])


def test_drift_detects_geometry_withdrawal_like_rte_june_2026():
    rep = diff("rte.postes", WITH_GEOM, WITHOUT_GEOM)
    assert rep.breaking and rep.geometry_removed
    assert rep.removed_fields == ["geo_point_2d"] and rep.added_fields == ["fonction"]


def test_added_fields_only_is_not_breaking():
    rep = diff("x", WITHOUT_GEOM.model_copy(update={"fields": WITHOUT_GEOM.fields[:3]}), WITHOUT_GEOM)
    assert not rep.breaking and rep.added_fields == ["fonction"]


def test_fetch_gate_blocks_breaking_drift(tmp_path: Path, monkeypatch):
    spec = load_sources()["rte.postes"]

    class Fake:
        def __init__(self, meta: SourceMetadata) -> None:
            self.meta = meta

        def describe(self, spec):
            return self.meta

        def fetch(self, spec, territory, dest, limit=None):
            dest.write_text("code_poste;nom_poste\nA;B\n", encoding="utf-8")
            return "https://example.invalid/export", 1

    monkeypatch.setitem(snapmod.CONNECTORS, "opendatasoft", Fake(WITH_GEOM))
    first, report = fetch_snapshot(spec, root=tmp_path)
    assert report is None and Path(first.path).exists()
    monkeypatch.setitem(snapmod.CONNECTORS, "opendatasoft", Fake(WITHOUT_GEOM))
    with pytest.raises(DriftError) as exc:
        fetch_snapshot(spec, root=tmp_path)
    assert exc.value.report.geometry_removed
    second, report = fetch_snapshot(spec, root=tmp_path, accept_drift=True)  # explicit human decision
    assert report is not None and report.breaking and second.path != first.path


@pytest.mark.network
@pytest.mark.vcr
def test_odre_rte_postes_contract():
    meta = OpendatasoftConnector().describe(load_sources()["rte.postes"])
    assert not meta.has_geometry  # withdrawn for public-safety reasons (verified 2026-11-16)
    assert {f.name for f in meta.fields} >= {"code_poste", "nom_poste", "fonction", "tension", "departement"}


@pytest.mark.network
@pytest.mark.vcr
def test_enedis_datafair_contract():
    meta = DataFairConnector().describe(load_sources()["enedis.poste_electrique"])
    assert meta.has_geometry and meta.count and meta.count > 900_000
    assert {"code_departement", "code_commune", "geometry"} <= {f.name for f in meta.fields}


@pytest.mark.network
@pytest.mark.vcr
def test_geoplateforme_wfs_admin_express_contract():
    from resilink.etl.connectors import WfsConnector

    meta = WfsConnector().describe(load_sources()["ign.admin_express.commune"])
    assert meta.has_geometry and meta.count and meta.count > 34_000  # COG 2026: 34,877 communes (2026-11-26)
    assert {"code_insee", "nom_officiel", "population", "date_du_recensement", "code_insee_du_departement",
            "geometrie"} <= {f.name for f in meta.fields}


@pytest.mark.network
@pytest.mark.vcr
def test_insee_filosofi_page_still_offers_the_registered_file():
    import httpx

    spec = load_sources()["insee.filosofi2021_200m"]
    r = httpx.get(spec.docs_url, follow_redirects=True, timeout=60,
                  headers={"User-Agent": "resilink-lab/0.1 (research; open-data client)"})
    assert r.status_code == 200 and f"/{spec.dataset}" in r.text  # link to the registered download


UA = {"User-Agent": "resilink-lab/0.1 (research; open-data client)"}


@pytest.mark.network
@pytest.mark.vcr
def test_enedis_hta_lines_contract_and_geojson_paging():
    import httpx

    spec = load_sources()["enedis.reseau_hta"]
    meta = DataFairConnector().describe(spec)
    assert meta.has_geometry and meta.count and meta.count > 1_000_000  # national overhead HTA segments
    r = httpx.get(f"{spec.base_url}/data-fair/api/v1/datasets/{spec.dataset}/lines", headers=UA, timeout=60,
                  params={"format": "geojson", "size": 1, "bbox": "5.30000,43.20000,5.50000,43.40000"})
    assert r.json()["total"] > 1 and "next" in r.links  # GeoJSON pages link to the next one in the Link header


@pytest.mark.network
@pytest.mark.vcr
def test_geofabrik_latest_redirects_to_a_dated_extract_with_md5():
    import re

    import httpx

    spec = load_sources()["osm.geofabrik.paca"]
    r = httpx.head(f"{spec.base_url}/{spec.dataset}", headers=UA, follow_redirects=True, timeout=60)
    assert r.status_code == 200 and re.search(r"-\d{6}\.osm\.pbf$", str(r.url))
    md5 = httpx.get(f"{r.url}.md5", headers=UA, timeout=60)
    assert re.fullmatch(r"[0-9a-f]{32}\s+\S+\.osm\.pbf\s*", md5.text)


@pytest.mark.network
@pytest.mark.vcr
def test_arcep_sites_documentation_and_pinned_quarter():
    import httpx

    spec = load_sources()["arcep.sites"]
    doc = httpx.get(spec.docs_url, headers=UA, follow_redirects=True, timeout=60).text
    for field in ("code_op", "id_station_anfr", "insee_com", "site_4g", "site_5g", "id_site_partage"):
        assert f"`{field}`" in doc
    quarter = spec.dataset.rsplit("/", 1)[0]
    index = httpx.get(f"{spec.base_url}/{quarter}/index.html", headers=UA, follow_redirects=True, timeout=60).text
    assert spec.dataset.rsplit("/", 1)[1] in index  # the pinned file is still published


@pytest.mark.network
@pytest.mark.vcr
def test_gas_sources_contracts():
    sources, ods = load_sources(), OpendatasoftConnector()
    natran = ods.describe(sources["natran.trace250"])
    assert natran.has_geometry and natran.count and natran.count > 10_000
    assert {"object_id", "departement"} <= {f.name for f in natran.fields}
    grdf = ods.describe(sources["grdf.network"])
    assert grdf.has_geometry and grdf.count and grdf.count > 3_000_000  # national distribution pipes
    assert {"code_insee_administratif", "etat_serv", "propane", "geo_point_2d"} <= {f.name for f in grdf.fields}
    plants = ods.describe(sources["odre.gas_power_plants"])
    assert {"site", "annee_de_reference", "puissance_installee_mw", "latitude_site", "longitude_site"} <= {
        f.name for f in plants.fields}


@pytest.mark.network
@pytest.mark.vcr
def test_georisques_tri_archives_are_still_offered():
    import httpx

    listing = httpx.get("https://www.georisques.gouv.fr/webappReport/ws/telechargement/tri_2020", headers=UA,
                        params={"anneemin": 2003}, timeout=60).json()["departemental"]
    links = {k[:2]: (v or {}).get("lien") for k, v in listing.items()}
    sources = load_sources()
    for dep in ("13", "30", "34", "83", "84"):
        spec = sources[f"georisques.tri2020_{dep}"]
        assert links[dep] == f"{spec.base_url}/{spec.dataset}"  # the registered archive is the one Géorisques offers
    assert links["04"] is None  # Alpes-de-Haute-Provence has no TRI
