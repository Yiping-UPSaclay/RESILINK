"""Registries for systems, slots and models (ADR-0008 step E1): discovery, refusals with reasons, pinning."""

from __future__ import annotations

import importlib
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Protocol

import pytest
from pydantic import BaseModel

from resilink.engine import PluginPolicy, PluginRegistry, code_hash, load_policy
from resilink.plugin_api import (
    PLUGIN_API_VERSION,
    ModelCard,
    ModelPlugin,
    On,
    Served,
    SlotDefinition,
    Subsystem,
    SystemPack,
)


@dataclass
class FakeEP:
    """Stands in for importlib.metadata.EntryPoint (name, group, value, module, dist, load)."""

    name: str
    group: str
    obj: Any = None
    error: Exception | None = None
    module: str = __name__
    dist: Any = None
    calls: list[int] = field(default_factory=list)

    @property
    def value(self) -> str:
        return f"{self.module}:{self.name.upper()}"

    def load(self) -> Any:
        self.calls.append(1)
        if self.error:
            raise self.error
        return self.obj


class _ServiceProto(Protocol):
    def evaluate(self, subsystem, up_nodes, up_edges, ctx): ...


class _Params(BaseModel):
    topology_mode: str = "meshed_optimistic"


def make_pack(pid: str = "toy") -> SystemPack:
    return SystemPack(id=pid, version="0.1.0", title="Toy system",
                      subsystems={f"{pid}_net": Subsystem(nodes=(f"{pid}_node",), links=(f"{pid}_link",))},
                      served=Served(measure="served_fraction", population_supplier=f"{pid}_node"),
                      actors={"operator": (f"{pid}_net",)})


def make_slot(version: str = "0.1.0") -> SlotDefinition:
    return SlotDefinition(id="service", version=version, protocol=_ServiceProto, cardinality="per_subsystem",
                          question="Within one subsystem, which nodes are served?",
                          triggers=[On("element.changed", 40)], default_model="toy_service")


def make_model(mid: str = "toy_service", **card_overrides: Any) -> ModelPlugin:
    base = dict(id=mid, version="0.1.0", slot="service", subsystems=("toy_net",), authors=("A. Student",),
                summary="Toy service model for tests: every node connected to a source is served.",
                licence="Apache-2.0", maturity="experimental")
    return ModelPlugin(card=ModelCard(**(base | card_overrides)), params=_Params, factory=lambda p, s: object())


def build(eps: list[FakeEP], policy: PluginPolicy | None = None) -> PluginRegistry:
    by_group: dict[str, list[FakeEP]] = defaultdict(list)
    for ep in eps:
        by_group[ep.group].append(ep)
    return PluginRegistry(entry_points=lambda g: by_group.get(g, []), policy=policy).load()


def good_set() -> list[FakeEP]:
    return [FakeEP("toy", "resilink.systems", make_pack()),
            FakeEP("service", "resilink.slots", make_slot()),
            FakeEP("toy_service", "resilink.models", make_model())]


def test_valid_plugins_register_and_are_pinned():
    reg = build(good_set())
    assert reg.unavailable == {} and reg.warnings == []
    assert reg.system("toy").title == "Toy system"
    assert reg.slot("service").cardinality == "per_subsystem"
    assert reg.model("toy_service").card.maturity == "experimental"
    pin = reg.pin("models", "toy_service")
    assert pin.value.endswith(":TOY_SERVICE") and len(pin.code_hash) == 16 and pin.code_hash != "unknown"
    summary = reg.describe()
    assert summary["plugin_api"] == PLUGIN_API_VERSION
    assert [m["id"] for m in summary["models"]] == ["toy_service"]


def test_a_broken_plugin_is_reported_and_the_others_still_load():
    eps = [*good_set(), FakeEP("broken", "resilink.models", error=ImportError("missing dependency 'julia'"))]
    reg = build(eps)
    assert "toy_service" in reg.models
    assert reg.unavailable[("resilink.models", "broken")].startswith("failed to load: ImportError")


def test_wrong_type_name_mismatch_and_duplicates_are_refused():
    eps = [*good_set(),
           FakeEP("not_a_pack", "resilink.systems", obj={"id": "not_a_pack"}),
           FakeEP("alias", "resilink.systems", obj=make_pack("water")),
           FakeEP("toy", "resilink.systems", obj=make_pack("toy"), module="other_pkg")]
    reg = build(eps)
    why = reg.unavailable
    assert "expected a SystemPack" in why[("resilink.systems", "not_a_pack")]
    assert "differs from the declared id" in why[("resilink.systems", "alias")]
    assert "duplicate id 'toy'" in why[("resilink.systems", "toy")]
    assert reg.system("toy").subsystems  # the first provider is kept


def test_incompatible_plugin_api_and_licence_are_refused():
    eps = [*good_set(),
           FakeEP("future_model", "resilink.models", make_model("future_model", api=">=1.0,<2")),
           FakeEP("gpl_model", "resilink.models", make_model("gpl_model", licence="GPL-3.0-only")),
           FakeEP("dual_model", "resilink.models", make_model("dual_model", licence="MIT OR GPL-3.0-only"))]
    reg = build(eps, policy=load_policy())
    assert PLUGIN_API_VERSION in reg.unavailable[("resilink.models", "future_model")]
    assert "needs review" in reg.unavailable[("resilink.models", "gpl_model")]
    assert "dual_model" in reg.models


def test_models_need_a_registered_compatible_slot_and_known_subsystems():
    eps = [FakeEP("toy", "resilink.systems", make_pack()),
           FakeEP("service", "resilink.slots", make_slot(version="0.2.0")),
           FakeEP("toy_service", "resilink.models", make_model()),
           FakeEP("orphan", "resilink.models", make_model("orphan", slot="fragility", subsystems=())),
           FakeEP("elsewhere", "resilink.models", make_model("elsewhere", slot_version=">=0.2,<0.3",
                                                             subsystems=("gas_net",)))]
    reg = build(eps)
    why = reg.unavailable
    assert "registered version is 0.2.0" in why[("resilink.models", "toy_service")]
    assert "slot 'fragility' is not registered" in why[("resilink.models", "orphan")]
    assert "unknown subsystems ['gas_net']" in why[("resilink.models", "elsewhere")]
    assert any("default model 'toy_service'" in w for w in reg.warnings)


def test_disabled_plugins_are_not_loaded_and_lookups_explain_why():
    eps = good_set()
    reg = build(eps, policy=PluginPolicy(disabled=frozenset({"toy_service"})))
    assert eps[2].calls == []  # never imported
    with pytest.raises(LookupError, match=r"disabled in configs/plugins\.yaml"):
        reg.model("toy_service")
    with pytest.raises(LookupError, match="registered: service"):
        reg.slot("hazard")


def test_installed_environment_loads_without_refusals():
    """Guard: every plugin installed in this environment (built-ins included, once ported) must load cleanly."""
    reg = PluginRegistry(policy=load_policy()).load()
    assert reg.unavailable == {}, reg.unavailable


def test_code_hash_tracks_file_contents(tmp_path, monkeypatch):
    pkg = tmp_path / "rl_hash_probe"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("X = 1\n", encoding="utf-8")
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "junk.pyc").write_bytes(b"ignored")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    first = code_hash("rl_hash_probe")
    (pkg / "__pycache__" / "junk.pyc").write_bytes(b"changed but ignored")
    assert code_hash("rl_hash_probe") == first
    (pkg / "model.py").write_text("Y = 2\n", encoding="utf-8")
    assert code_hash("rl_hash_probe") != first
    assert code_hash("no_such_module_xyz") == "unknown"
    sys.modules.pop("rl_hash_probe", None)


def test_repository_policy_accepts_permissive_licences_only():
    policy = load_policy()
    assert policy.licence_allowed("Apache-2.0") and policy.licence_allowed("MIT")
    assert not policy.licence_allowed("GPL-3.0-only")
    assert policy.licence_allowed("(MIT OR GPL-3.0-only)")
    assert not policy.licence_allowed("MIT AND GPL-3.0-only")
