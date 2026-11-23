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
