"""Plugin API 0.1 contract (ADR-0008 step E1): cards, board fields, slot definitions, system packs."""

from __future__ import annotations

from typing import Protocol

import pytest
from pydantic import BaseModel, ValidationError

from resilink.plugin_api import (
    PLUGIN_API_VERSION,
    STANDARD,
    ColumnDecl,
    Execution,
    MetricDecl,
    ModelCard,
    ModelPlugin,
    On,
    Served,
    SlotDefinition,
    StateField,
    Subsystem,
    SystemPack,
    TableDecl,
    satisfies,
)


class _Proto(Protocol):
    def plan(self, damage, ctx): ...


def card(**overrides) -> ModelCard:
    base = dict(id="toy_repair", version="0.1.0", slot="restoration", authors=("A. Student",),
                summary="Toy repair model for tests: repairs every element after a fixed delay.",
                licence="Apache-2.0", maturity="experimental")
    return ModelCard(**(base | overrides))


def test_api_version_is_a_draft_and_ranges_work():
    assert PLUGIN_API_VERSION.startswith("0.")
    assert satisfies(PLUGIN_API_VERSION, ">=0.1,<0.2")
    assert not satisfies(PLUGIN_API_VERSION, ">=1.0")
    assert not satisfies("not-a-version", ">=0.1")


def test_standard_fields_have_units_and_defaults():
    assert {"hazard.intensity", "asset.age_y", "asset.condition", "damage.state", "damage.functionality",
            "repair.start_h", "repair.finish_h", "service.served", "service.level", "outage.hours"} == set(STANDARD)
    cond = STANDARD["asset.condition"]
    assert (cond.unit, cond.default, cond.dtype) == ("1", 1.0, "float32")


def test_valid_card_and_namespaced_outputs():
    table = TableDecl(name="toy_repair.crew_log", grain=("scenario", "outer", "inner"),
                      columns=(ColumnDecl(name="crew_hours", dtype="float64", unit="h", description="Crew time"),))
    metric = MetricDecl(id="toy_repair.crew_hours", source="toy_repair.crew_log.crew_hours", unit="h", better="lower")
    c = card(writes=(STANDARD["repair.finish_h"],
                     StateField(name="toy_repair.queue", grain=("node",), dtype="int32", unit="1",
                                default=0, description="Queue position")),
             tables=(table,), metrics=(metric,), streams=("delay",))
    assert c.owned_fields == {"repair.finish_h", "toy_repair.queue"}


@pytest.mark.parametrize("overrides, message", [
    ({"id": "Toy-Repair"}, "model id"),
    ({"version": "1.0"}, "SemVer"),
    ({"summary": "too short"}, "when to use"),
    ({"authors": ()}, "author"),
    ({"api": "not a range"}, "PEP 440"),
    ({"writes": (StateField(name="other.field", grain=("node",), dtype="int8", unit="1", default=0,
                            description="x"),)}, "own namespace"),
    ({"writes": (StateField(name="repair.finish_h", grain=("node",), dtype="float32", unit="days",
                            default=None, description="x"),)}, "standard field"),
    ({"tables": (TableDecl(name="other.log", grain=("inner",), columns=()),)}, "namespace"),
    ({"metrics": (MetricDecl(id="toy_repair.m", source="toy_repair.missing.col", unit="h", better="lower"),)},
     "not a declared table column"),
    ({"maturity": "validated"}, "citation"),
])
def test_card_rules(overrides, message):
    with pytest.raises(ValidationError, match=message):
        card(**overrides)


def test_worker_execution_needs_a_pinned_environment():
    with pytest.raises(ValidationError, match="environment"):
        Execution(mode="worker")
    assert Execution(mode="worker", environment="plugins/ac_opf", entry="julia run.jl").mode == "worker"


def test_model_plugin_requires_a_pydantic_params_model():
    class Params(BaseModel):
        delay_h: float = 4.0

    plugin = ModelPlugin(card=card(), params=Params, factory=lambda p, setup: object())
    assert plugin.id == "toy_repair" and "delay_h" in plugin.params_schema()["properties"]
    with pytest.raises(TypeError, match="pydantic"):
        ModelPlugin(card=card(), params=dict, factory=lambda p, setup: object())


def slot(**overrides) -> SlotDefinition:
    base = dict(id="restoration", version="0.1.0", protocol=_Proto,
                question="When is each damaged element repaired?",
                triggers=[On("damage.changed", 30), On("decision.epoch", 30)],
                owns=["repair.start_h", "repair.finish_h"], emits=["element.changed"],
                default_model="independent_repair")
    return SlotDefinition(**(base | overrides))


def test_slot_definition_rules():
    s = slot()
    assert s.events == {"damage.changed", "decision.epoch"} and isinstance(s.owns, tuple)
    assert slot(emits=["restoration.crew_dispatched"]).emits == ("restoration.crew_dispatched",)
    for overrides, message in [
        ({"owns": ["repair.eta"]}, "unknown standard fields"),
        ({"emits": ["other.event"]}, "may define new events only"),
        ({"cardinality": "several"}, "cardinality"),
        ({"version": "v1"}, "SemVer"),
        ({"protocol": "not a class"}, "protocol"),
    ]:
        with pytest.raises((ValueError, TypeError), match=message):
            slot(**overrides)
    with pytest.raises(ValueError, match="phase"):
        On("damage.changed", 101)


def pack(**overrides) -> SystemPack:
    base = dict(
        id="water", version="0.1.0", title="Water supply",
        subsystems={"water_network": Subsystem(nodes=("pump", "reservoir"), links=("main",))},
        served=Served(measure="pressure_available", population_supplier="reservoir"),
        attributes={"pump": {"rated_kw": {"unit": "kW"}}},
        repair_median_h={"pump": 24.0},
        actors={"water_utility": ("water_network",)},
    )
    return SystemPack(**(base | overrides))


def test_system_pack_rules(tmp_path):
    p = pack()
    assert p.node_classes == ("pump", "reservoir") and p.subsystem_of("main") == "water_network"
    for overrides, message in [
        ({"subsystems": {"network": Subsystem(nodes=("pump",))}}, "must be named"),
        ({"subsystems": {"water_a": Subsystem(nodes=("pump",)), "water_b": Subsystem(nodes=("pump",))}},
         "appears in both"),
        ({"served": Served(measure="x", population_supplier="main")}, "not a node class"),
        ({"actors": {"utility": ("water_other",)}}, "unknown subsystems"),
        ({"repair_median_h": {"valve": 3.0}}, "unknown classes"),
        ({"tables": {"pipes": {"diameter_mm": "mm"}}}, "must be named"),
        ({"subsystems": {}}, "at least one subsystem"),
    ]:
        with pytest.raises(ValidationError, match=message):
            pack(**overrides)
    path = tmp_path / "pack.yaml"
    path.write_text(
        "id: water\nversion: 0.1.0\ntitle: Water supply\n"
        "subsystems:\n  water_network: {nodes: [pump, reservoir], links: [main]}\n"
        "served: {measure: pressure_available, population_supplier: reservoir}\n", encoding="utf-8")
    assert SystemPack.from_yaml(path).served.population_supplier == "reservoir"
