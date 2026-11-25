"""Proof-of-concept criteria: a risk and resilience lab for interdependent French infrastructures, on open data.

These check the real Bouches-du-Rhône release and runs, which live in this machine's data directory (never in git).
Until they exist the criteria report "not met" (imperative xfail); on GitHub CI they always do. They are measured
on the machine that builds the data, by ``tools/progress.py``. Nothing here writes to the data directory.
"""

from __future__ import annotations

import pytest

BDR_RELEASE = "bdr-2027.01.0"
FLOOD_EXPERIMENT_PREFIX = "bdr_flood"
BUDGET_S_PER_WORLD = 1.0  # blueprint §6 acceptance: median <= 1 s per simulated world
MAX_MC_REL_SE = 0.05  # blueprint §6 acceptance: Monte Carlo relative standard error < 5 %


def _real_release(root):
    from resilink.io.release import load_release

    try:
        return load_release(BDR_RELEASE, root)
    except FileNotFoundError:
        pytest.xfail(f"release {BDR_RELEASE} is not built on this machine yet")


@pytest.mark.acceptance("POC/AC1")
def test_poc_ac1_real_release_builds_from_open_data(real_data_root):
    """A real Bouches-du-Rhône release exists, validates, uses only declared systems, and passed its QA gates."""
    from resilink.domain.schemas import EDGE_SCHEMA, NODE_SCHEMA
    from resilink.registry import system_catalog

    rel = _real_release(real_data_root)
    nodes, edges = rel.read("nodes"), rel.read("edges")
    NODE_SCHEMA.validate(nodes.drop(columns=["bbox"], errors="ignore"))
    EDGE_SCHEMA.validate(edges.drop(columns=["bbox"], errors="ignore"))
    assert {"elec", "gas", "telecom"} <= set(nodes["layer"]), "all three systems must be present"
    assert system_catalog().problems(nodes, edges) == []
    assert rel.manifest.qa.get("status") == "pass", rel.manifest.qa
    assert "grid_cells" in rel.manifest.tables and "service_links" in rel.manifest.tables


@pytest.mark.acceptance("POC/AC2")
def test_poc_ac2_flood_risk_campaign_on_the_real_territory(real_data_root):
    """A flood risk campaign ran on the real release within the time budget, with converged estimates."""
    from resilink.io.results import ResultsStore

    _real_release(real_data_root)
    store = ResultsStore(real_data_root)
    runs = [r for r in store.list_runs()
            if r["release"] == BDR_RELEASE and r["experiment"].startswith(FLOOD_EXPERIMENT_PREFIX)]
    if not runs:
        pytest.xfail(f"no {FLOOD_EXPERIMENT_PREFIX}* campaign on {BDR_RELEASE} yet")
    m = store.manifest(runs[-1]["run_id"])
    assert m["elapsed_s"] / m["cascade_simulations"] <= BUDGET_S_PER_WORLD, "over the time budget per world"
    metrics = store.read(runs[-1]["run_id"], "metrics")
    eal = metrics[(metrics["metric"] == "eal") & (metrics["sector"] == "all")]
    assert len(eal) == 1 and eal["value"].iloc[0] > 0
    rel_se = metrics[(metrics["metric"] == "mc_rel_se") & (metrics["sector"] == "all")]["value"]
    assert (rel_se.dropna() <= MAX_MC_REL_SE).all(), "Monte Carlo error too large: add simulated worlds"


@pytest.mark.acceptance("POC/AC3")
def test_poc_ac3_results_compared_with_an_observed_event(real_data_root):
    """Model outputs on the real territory are scored against an observed event (proving ground)."""
    try:
        from resilink.benchmarks import list_scores  # proving ground, roadmap item P1e
    except ImportError:
        pytest.xfail("the proving ground is not built yet")
    _real_release(real_data_root)
    assert any(s.release_id == BDR_RELEASE for s in list_scores(real_data_root))
