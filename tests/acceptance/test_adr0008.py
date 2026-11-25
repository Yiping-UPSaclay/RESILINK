"""Acceptance criteria of ADR-0008 revision 2 (extension architecture), as executable tests.

Progress is measured, not reported: a criterion counts as met only when its test passes. Criteria that are not
built yet are ``xfail(strict=True)``; when one starts passing the suite fails, and its ``xfail`` must be removed.
AC2 is a guard: it already passes and must keep passing throughout the refactor.
"""

from __future__ import annotations

import json
import tempfile
from importlib.metadata import entry_points
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
BASELINE = json.loads((ROOT / "tests/acceptance/baselines/sprint0_small_metrics.json").read_text(encoding="utf-8"))
NOT_BUILT = "ADR-0008 refactor not implemented yet"


def _names(group: str) -> set[str]:
    return {ep.name for ep in entry_points(group=group)}


def _schema2(cfg, components: dict) -> dict:
    return cfg.model_dump(mode="json") | {"schema": 2, "components": components}


@pytest.mark.acceptance("ADR-0008/AC1")
@pytest.mark.xfail(strict=True, reason=f"{NOT_BUILT} (E1–E3)")
def test_ac1_builtins_registered_and_vocab_freed():
    import resilink.domain.vocab as vocab

    assert {"elec", "gas", "telecom", "facility"} <= _names("resilink.systems")
    assert {"hazard", "fragility", "dependency", "service", "propagation", "restoration", "consequence"} <= _names(
        "resilink.slots")
    assert {"flood_footprints", "lognormal_library", "proximity_softmax", "connectivity", "topological_temporal",
            "independent_repair", "people_hours", "episode", "nested_mc"} <= _names("resilink.models")
    for const in ("LAYERS", "NODE_CLASSES", "EDGE_CLASSES", "SECTOR_SUPPLIER_CLASS", "REPAIR_MEDIAN_H"):
        assert not hasattr(vocab, const), f"{const} is still hard-coded in domain/vocab.py"


@pytest.mark.acceptance("ADR-0008/AC2")
def test_ac2_guard_results_stay_consistent_with_sprint0(small_cfg, small_run):
    """Guard: mean losses within 3 combined Monte Carlo standard errors of Sprint 0, and bit-for-bit reruns."""
    from resilink.experiments import run_experiment
    from resilink.experiments.runner import results_hash
    from resilink.io.results import ResultsStore

    metrics = ResultsStore().read(small_run.run_id, "metrics")
    now = {(r.scenario_id, r.sector, r.metric): float(r.value) for r in metrics.itertuples()}
    checked = 0
    for key, base in BASELINE["metrics"].items():
        if "mean_loss" not in base:
            continue
        scenario, sector = key.split("|")
        m_new, m_old = now[(scenario, sector, "mean_loss")], base["mean_loss"]
        se_new = m_new * (now.get((scenario, sector, "mc_rel_se")) or 0.0)
        se_old = m_old * (base.get("mc_rel_se") or 0.0)
        assert abs(m_new - m_old) <= 3.0 * np.hypot(se_new, se_old) + 1e-6, (key, m_new, m_old)
        checked += 1
    assert checked >= 9

    scratch = ResultsStore(Path(tempfile.mkdtemp(prefix="resilink-ac2-")))
    rerun = run_experiment(small_cfg, store=scratch, force=True)
    assert results_hash(scratch, rerun.run_id) == results_hash(ResultsStore(), small_run.run_id)


@pytest.mark.acceptance("ADR-0008/AC3")
@pytest.mark.xfail(strict=True, reason=f"{NOT_BUILT} (E9)")
def test_ac3_new_slot_and_new_system_come_from_separate_packages():
    """The 'no file under packages/resilink/ changed' half of AC3 is a CI diff check on the example PRs."""
    eps = {g: {ep.name: ep for ep in entry_points(group=g)}
           for g in ("resilink.systems", "resilink.slots", "resilink.models")}
    assert "water" in eps["resilink.systems"]
    assert "degradation" in eps["resilink.slots"]
    assert {"gamma_condition", "condition_lognormal"} <= set(eps["resilink.models"])
    for group, name in (("resilink.systems", "water"), ("resilink.slots", "degradation"),
                        ("resilink.models", "gamma_condition")):
        assert eps[group][name].dist.name != "resilink", f"{name} must come from a separate distribution"


@pytest.mark.acceptance("ADR-0008/AC4")
@pytest.mark.xfail(strict=True, reason=f"{NOT_BUILT} (E5–E6)")
def test_ac4_ensemble_reports_model_uncertainty(small_cfg):
    from resilink.domain.config import ExperimentConfig
    from resilink.experiments import run_experiment
    from resilink.io.results import ResultsStore

    cfg = _schema2(small_cfg, {"fragility": {"alternatives": [
        {"model": "lognormal_library", "weight": 0.5}, {"model": "condition_lognormal", "weight": 0.5}]}})
    run = run_experiment(ExperimentConfig.model_validate(cfg))
    breakdown = ResultsStore().read(run.run_id, "model_uncertainty")
    assert set(breakdown["alternative"]) == {"lognormal_library", "condition_lognormal"}


@pytest.mark.acceptance("ADR-0008/AC5")
@pytest.mark.xfail(strict=True, reason=f"{NOT_BUILT} (E5)")
def test_ac5_missing_data_fails_early_and_defaults_are_reported(small_cfg):
    from resilink.engine.compose import CompositionError, compose

    with pytest.raises(CompositionError, match="q_max_mvar"):
        compose(_schema2(small_cfg, {"service": {"elec_transmission": {"model": "fixture_needs_reactive_limits"}}}))
    report = compose(_schema2(small_cfg, {"fragility": {"model": "condition_lognormal"}})).report
    assert "asset.condition" in report.defaulted


@pytest.mark.acceptance("ADR-0008/AC6")
@pytest.mark.xfail(strict=True, reason=f"{NOT_BUILT} (E7)")
def test_ac6_persistent_worker_is_deterministic():
    from resilink.engine.conformance import check_distribution

    report = check_distribution("resilink-fixture-worker-service")
    assert report.passed and report.execution_mode == "worker" and report.check("determinism").passed


@pytest.mark.acceptance("ADR-0008/AC7")
@pytest.mark.xfail(strict=True, reason=f"{NOT_BUILT} (E8)")
def test_ac7_conformance_kit_accepts_builtins_and_rejects_broken_fixtures():
    from resilink.engine.conformance import check_distribution

    assert check_distribution("resilink").passed
    broken = {"resilink-fixture-nondeterministic": "determinism", "resilink-fixture-undeclared-write": "board",
              "resilink-fixture-bad-output": "outputs", "resilink-fixture-forbidden-import": "hygiene"}
    for dist, check in broken.items():
        report = check_distribution(dist)
        assert not report.passed and not report.check(check).passed, dist


@pytest.mark.acceptance("ADR-0008/AC8")
@pytest.mark.xfail(strict=True, reason=f"{NOT_BUILT} (E9)")
def test_ac8_discovery_ops_and_eval_task():
    from resilink.ops import REGISTRY, load_builtin_ops

    load_builtin_ops()
    names = {spec.name for spec in REGISTRY.all()}
    assert {"systems.list", "slots.list", "models.list", "models.describe"} <= names
    tasks = (ROOT / "evals/golden/tasks.yaml").read_text(encoding="utf-8")
    assert "models.describe" in tasks, "a golden eval task must exercise model discovery"


@pytest.mark.acceptance("ADR-0008/AC9")
@pytest.mark.xfail(strict=True, reason=f"{NOT_BUILT}: plugin_api exists (E1); the plugin import contract comes "
                                       "with the ported built-ins (E3). Runtime budget: tools/progress.py")
def test_ac9_plugin_api_and_import_contract():
    import resilink.plugin_api as api

    assert api.PLUGIN_API_VERSION.startswith("0.")
    contracts = (ROOT / ".importlinter").read_text(encoding="utf-8")
    assert "[importlinter:contract:plugins-use-only-plugin-api]" in contracts, (
        "plugins (built-in models and plugins/*) must be limited to resilink.plugin_api by an import contract")
