"""D3 telecom: ARCEP sites file repaired (mixed encoding, stripped zeros, decimal commas, damaged site numbers),
static CSV inspection, and the ARCEP and OSM telecom adapters, run offline against the mock portal."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pytest

from resilink.etl.adapters import ADAPTERS
from resilink.etl.adapters.arcep import SPREADSHEET_NUMBER, decode_mixed, pad
from resilink.etl.contracts import load_sources
from resilink.etl.snapshot import fetch_snapshot
from resilink.etl.staging import ordered, stage
from tests.etl_fixtures import TELECOM_SOURCES, TERR, TERRITORY_SOURCES, fetch_all


def test_mixed_encoding_and_stripped_codes_are_repaired():
    text, repaired = decode_mixed("Rhône;".encode() + "Rémens".encode("cp1252") + ";Prévessin-Moëns".encode("cp1252"))
    assert text == "Rhône;Rémens;Prévessin-Moëns" and repaired == 3  # é and ë of Prévessin-Moëns are two runs
    assert [pad("4", 2), pad("4112", 5), pad("132290004", 10), pad("2A", 2), pad("02A2290003", 10), pad(None, 10)] == [
        "04", "04112", "0132290004", "2A", "02A2290003", ""]
    assert SPREADSHEET_NUMBER.match("1,03E+05") and not SPREADSHEET_NUMBER.match("00000064H3")


def test_static_csv_is_inspected_with_its_delimiter(portal, tmp_path: Path):
    snap, _ = fetch_snapshot(load_sources()["arcep.sites"], TERR, root=tmp_path)
    names = [f.name for f in snap.metadata.fields]
    assert names[:5] == ["code_op", "nom_op", "num_site", "id_site_partage", "id_station_anfr"] and len(names) == 29
    assert snap.rows == 7


def test_every_adapter_has_its_sources_registered_and_runs_in_order():
    sources = load_sources()
    assert all(s in sources for a in ADAPTERS.values() for s in a.sources)
    order = [a.id for a in ordered(ADAPTERS)]
    assert order.index("ign.admin_express") < order.index("arcep.sites")
    assert order.index("osm.power") < order.index("rte.sites")


def test_telecom_adapters_stage_sites_and_exchanges(portal, tmp_path: Path):
    fetch_all(tmp_path, TERRITORY_SOURCES)
    stage("ign.admin_express", TERR, root=tmp_path)
    fetch_all(tmp_path, TELECOM_SOURCES)
    first = {r.table: r for a in ("arcep.sites", "osm.telecom") for r in stage(a, TERR, root=tmp_path)}
    staging = tmp_path / "staging" / "tst"

    sites = gpd.read_parquet(staging / "stg_telecom_mobile_sites.parquet").set_index("rid")
    assert len(sites) == 6  # the site 40 km away is dropped
    orange = sites.loc["telecom.mobile_site:anfr:0132290004-20801"]  # ANFR id zero-padded to 10 characters
    assert orange["in_core"] and orange["site_5g"] and orange["insee_com"] == "13001"
    assert orange.geometry.x == pytest.approx(X0_PLUS_2000 + 0.4)  # decimal-comma x read as 892000.4
    sfr = sites.loc["telecom.mobile_site:anfr:0132290005-20810"]
    assert sfr["support_id"] != orange["support_id"] or orange.geometry.distance(sfr.geometry) < 1.0
    assert sfr["id_site_partage"] == "MUT1"
    bouygues = sites.loc["telecom.mobile_site:anfr:0042290001-20820"]
    assert (bouygues["insee_dep"], bouygues["insee_com"]) == ("04", "04112") and not bouygues["in_core"]
    assert any(r.startswith("telecom.mobile_site:fp:p") and r.endswith("-20815") for r in sites.index)  # no ANFR id
    assert sites["num_site_spreadsheet_damaged"].sum() == 1
    qa = first["stg_telecom_mobile_sites"].qa
    assert qa["missing_anfr_id"] == 1 and qa["codes_zero_padded"] == 1 and qa["anfr_departement_differs"] == 0
    assert qa["text_runs_repaired_from_cp1252_national"] == 7 and qa["xy_vs_latlon_max_m"] < 1.0
    assert qa["core_with_technology"]["5g"] == 4 and qa["core_sites_with_shared_site_id"] == 1

    ex = gpd.read_parquet(staging / "stg_telecom_exchanges_osm.parquet").set_index("osm_id")
    assert sorted(ex.index) == ["n40", "w41"]  # the connection point and the far exchange are not kept
    assert ex.loc["n40", "telecom"] == "exchange" and ex.loc["n40", "operator"] == "Orange" and ex.loc["n40", "in_core"]
    assert ex.loc["w41", "telecom"] == "data_center" and ex.loc["w41", "footprint_m2"] == pytest.approx(3000, rel=5e-3)
    assert first["stg_telecom_exchanges_osm"].licences == {s: "ODbL 1.0" for s in ADAPTERS["osm.telecom"].sources}

    again = {r.table: r for a in ("arcep.sites", "osm.telecom") for r in stage(a, TERR, root=tmp_path)}
    assert {t: r.sha256 for t, r in again.items()} == {t: r.sha256 for t, r in first.items()}  # byte-identical


X0_PLUS_2000 = 892_000.0
