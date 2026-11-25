"""Slots and built-in models as plugins, composed from the experiment config (ADR-0008 step E3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from resilink.domain.config import ExperimentConfig
from resilink.engine.compose import CompositionError, compose
from resilink.experiments.runner import compute_run_id
from resilink.io.results import ResultsStore
from resilink.registry import get_registry

ROOT = Path(__file__).resolve().parents[2]
SLOTS = {"hazard", "fragility", "dependency", "service", "propagation", "restoration", "consequence",
         "orchestrator", "sampler"}
MODELS = {"flood_footprints": "hazard", "lognormal_library": "fragility", "proximity_softmax": "dependency",
          "no_dependencies": "dependency", "connectivity": "service", "topological_temporal": "propagation",
          "independent_repair": "restoration", "people_hours": "consequence", "episode": "orchestrator",
          "nested_mc": "sampler"}


def test_builtin_slots_and_models_are_registered_like_any_plugin():
    reg = get_registry()
    assert reg.unavailable == {} and reg.warnings == []
    assert set(reg.slots) == SLOTS
    assert {m: reg.model(m).card.slot for m in MODELS} == MODELS
    for m in MODELS:
        pin = reg.pin("models", m)
        assert pin.distribution == "resilink" and pin.code_hash != "unknown"
    assert reg.slot("service").cardinality == "per_subsystem" and reg.slot("orchestrator").engine_level


def test_schema_1_config_and_worker_count_give_the_same_run(small_cfg, release):
    legacy = dict(experiment=small_cfg.experiment, release=small_cfg.release,
                  dependencies={"lambda_scale": {"prior": "lognormal", "median": 1.0, "gsd": 1.5},
                                "backup_scale": {"prior": "lognormal", "median": 1.0, "gsd": 1.4}},
                  monte_carlo={"outer": 3, "inner": 8, "seed": 7, "workers": 4},
                  outputs={"keep_traces": 4, "tier": "internal"})
    upgraded = ExperimentConfig.model_validate(legacy)
    assert upgraded.schema_version == 2 and upgraded.design.workers == 4
    assert upgraded.components.dependency.model == "proximity_softmax"
    assert compute_run_id(upgraded, release) == compute_run_id(small_cfg, release)


def test_disabled_dependencies_become_the_no_dependencies_model(small_cfg):
    cfg = ExperimentConfig.model_validate({"experiment": "x", "release": small_cfg.release,
                                           "dependencies": {"enabled": False}})
    assert cfg.components.dependency.model == "no_dependencies"


@pytest.mark.parametrize("component, message", [
    ({"hazard": {"model": "no_such_model"}}, "no model 'no_such_model'"),
    ({"hazard": {"model": "connectivity"}}, "answers the 'service' slot, not 'hazard'"),
    ({"fragility": {"model": "lognormal_library", "version": "==9.9"}}, "installed version is 1.0.0"),
    ({"consequence": [{"model": "people_hours", "params": {"disruption_threshold_h": -1}}]}, "invalid parameters"),
    ({"fragility": {"model": "lognormal_library", "params": {"library": "configs/missing.yaml"}}}, "does not exist"),
])
def test_composition_errors_are_explicit_and_happen_before_compute(small_cfg, component, message):
    data = small_cfg.model_dump(mode="json", by_alias=True)
    data["components"] = {**data["components"], **component}
    with pytest.raises(CompositionError, match=message):
        compose(data)


def test_per_subsystem_service_models_wait_for_the_event_kernel(small_cfg):
    data = small_cfg.model_dump(mode="json", by_alias=True)
    data["components"]["service"]["elec_transmission"] = {"model": "connectivity"}
    with pytest.raises(ValidationError, match="event kernel"):
        ExperimentConfig.model_validate(data)


def test_run_manifest_pins_every_component(small_run):
    m = ResultsStore().manifest(small_run.run_id)
    assert set(m["models"]) == {"hazard", "fragility", "dependency", "service.default", "propagation",
                                "restoration", "consequence.0", "orchestrator", "sampler"}
    dep = m["models"]["dependency"]
    assert dep["model"] == "proximity_softmax" and dep["version"] == "1.0.0" and len(dep["files"]["rules"]) == 64
    assert m["maturity"] == "experimental" and m["config"]["schema"] == 2
    assert set(m["systems"]) == {"elec", "gas", "telecom"}


def test_e3_reproduces_sprint0_numbers_exactly(small_run):
    """Models moved into plugins and the loop became an orchestrator; the numbers must not move at all."""
    baseline = json.loads((ROOT / "tests/acceptance/baselines/sprint0_small_metrics.json").read_text(encoding="utf-8"))
    metrics = ResultsStore().read(small_run.run_id, "metrics")
    now = {(r.scenario_id, r.sector, r.metric): float(r.value) for r in metrics.itertuples()}
    checked = 0
    for key, values in baseline["metrics"].items():
        scenario, sector = key.split("|")
        for metric, value in values.items():
            assert now[(scenario, sector, metric)] == pytest.approx(value, abs=1e-6), (key, metric)
            checked += 1
    assert checked == 28  # 3 scenarios x 4 sectors x (mean loss, Monte Carlo error) + 4 annual EAL values
