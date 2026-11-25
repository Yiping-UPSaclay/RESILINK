"""Built-in system packs (ADR-0008 step E2): registered like any plugin, and carrying Sprint 0's vocabulary unchanged."""

from __future__ import annotations

import pandas as pd
import pytest
import yaml

from resilink.dependencies.model import _class_mask
from resilink.network.compiled import compile_network
from resilink.plugin_api import Served, Subsystem, SystemPack
from resilink.registry import SystemCatalog, get_registry, system_catalog
from resilink.synthetic import ensure_synthetic_release
from resilink.systems import BUILTIN_SYSTEMS

# Frozen copy of Sprint 0's domain/vocab.py constants (before E2), to prove the move into packs changed nothing.
SPRINT0_NODES = {
    "elec": {"sub_tx", "sub_primary", "sub_mv_lv", "generator"},
    "gas": {"junction", "compressor", "delivery_station", "lng_terminal", "storage", "commune_distribution"},
    "telecom": {"mobile_site", "exchange", "aggregation_logical"},
    "facility": {"health", "emergency"},
}
SPRINT0_EDGES = {"elec": {"line_hv", "line_mv_aerial", "cable_mv"}, "gas": {"pipeline_tx"},
                 "telecom": {"backhaul_logical"}, "facility": set()}
SPRINT0_SUPPLIERS = (("elec", "sub_mv_lv"), ("gas", "delivery_station"), ("telecom", "mobile_site"))
SPRINT0_REPAIR_H = {
    "sub_tx": 48.0, "sub_primary": 36.0, "sub_mv_lv": 12.0, "generator": 72.0, "junction": 24.0, "compressor": 48.0,
    "delivery_station": 24.0, "lng_terminal": 96.0, "storage": 96.0, "commune_distribution": 24.0,
    "mobile_site": 8.0, "exchange": 24.0, "aggregation_logical": 12.0, "line_hv": 48.0, "line_mv_aerial": 10.0,
    "cable_mv": 24.0, "pipeline_tx": 72.0, "backhaul_logical": 12.0,
    "health": 24.0, "emergency": 24.0,  # Sprint 0's fallback for classes without a value, now explicit
}


def _water_pack() -> SystemPack:
    return SystemPack(id="water", version="0.1.0", title="Water supply",
                      subsystems={"water_network": Subsystem(nodes=("pump", "reservoir"), links=("main",))},
                      served=Served(measure="pressure_available", population_supplier="reservoir"),
                      repair_median_h={"pump": 24.0, "reservoir": 48.0, "main": 12.0})


def test_builtin_packs_are_registered_through_entry_points():
    reg = get_registry()
    assert set(reg.systems) == {"elec", "facility", "gas", "telecom"}
    assert reg.unavailable == {}
    pin = reg.pin("systems", "elec")
    assert pin.distribution == "resilink" and pin.value == "resilink.systems:ELEC" and pin.code_hash != "unknown"
    assert set(reg.system("elec").subsystems) == {"elec_transmission", "elec_distribution"}


def test_packs_carry_exactly_the_sprint0_vocabulary():
    cat = system_catalog()
    for system, classes in SPRINT0_NODES.items():
        assert set(cat.pack(system).node_classes) == classes, system
        assert set(cat.pack(system).link_classes) == SPRINT0_EDGES[system], system
    assert cat.population_suppliers() == SPRINT0_SUPPLIERS
    for system in SPRINT0_NODES:
        pack = cat.pack(system)
        for cls in (*pack.node_classes, *pack.link_classes):
            assert cat.repair_median_h(system, cls) == SPRINT0_REPAIR_H[cls], (system, cls)


def test_catalog_reports_unknown_systems_classes_and_missing_repair_times():
    cat = system_catalog()
    nodes = pd.DataFrame({"layer": ["elec", "water", "gas"], "class": ["sub_tx", "pump", "pipeline_tx"]})
    problems = cat.problems(nodes)
    assert any("'water', which has no installed system pack" in p for p in problems)
    assert any("node class 'pipeline_tx' is not declared by system pack 'gas'" in p for p in problems)
    assert cat.problems(pd.DataFrame({"layer": ["elec"], "class": ["sub_tx"]})) == []
    with pytest.raises(KeyError, match="no system pack 'water' is installed"):
        cat.pack("water")


def test_compiled_network_takes_systems_from_the_packs(net, release):
    assert net.layer_names == ("elec", "gas", "telecom")
    assert net.sectors == ("elec", "gas", "telecom")
    nodes = release.read("nodes").sort_values("k")
    assert [net.layer_names[c] for c in net.layer] == list(nodes["layer"])
    assert list(net.repair_median_h) == [SPRINT0_REPAIR_H[c] for c in net.cls]
    assert list(net.edge_repair_median_h) == [SPRINT0_REPAIR_H[c] for c in net.edge_cls]


def test_a_release_never_depends_on_other_installed_packs(release, tmp_path):
    """Installing a new system (e.g. a thesis' water pack) must not change an existing release or how it compiles."""
    with_water = SystemCatalog((*BUILTIN_SYSTEMS, _water_pack()))
    rebuilt = ensure_synthetic_release(tmp_path, catalog=with_water)
    assert rebuilt.manifest.content_hash() == release.manifest.content_hash()
    net = compile_network(release, with_water)
    assert net.layer_names == ("elec", "gas", "telecom") and net.sectors == ("elec", "gas", "telecom")


def test_compiling_a_release_with_an_unknown_system_fails_clearly(release):
    without_gas = SystemCatalog(tuple(p for p in BUILTIN_SYSTEMS if p.id != "gas"))
    with pytest.raises(ValueError, match="'gas', which has no installed system pack"):
        compile_network(release, without_gas)


def test_dependency_rules_match_present_systems_and_reject_unknown_ones(net):
    known = system_catalog().systems
    assert _class_mask(net, "telecom.mobile_site", known).sum() == 16
    assert not _class_mask(net, "facility.health", known).any()  # installed system, absent from this release
    with pytest.raises(ValueError, match="'watr'"):
        _class_mask(net, "watr.pump", known)


def test_ontology_classes_are_declared_by_the_packs():
    from resilink.io.paths import repo_root

    ontology = yaml.safe_load((repo_root() / "knowledge" / "ontology.yaml").read_text(encoding="utf-8"))
    cat = system_catalog()
    for system, classes in ontology["classes"].items():
        pack = cat.pack(system)
        missing = set(classes) - set(pack.node_classes) - set(pack.link_classes)
        assert not missing, f"ontology classes {sorted(missing)} are not declared by the {system!r} pack"
