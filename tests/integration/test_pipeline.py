"""Synthetic end-to-end: release determinism, run caching, reproducibility, worker-count invariance."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd

from resilink.experiments import compute_run_id, run_experiment
from resilink.experiments.runner import results_hash
from resilink.io.results import ResultsStore
from resilink.synthetic import ensure_synthetic_release


def test_release_rebuild_is_byte_identical(release):
    other = ensure_synthetic_release(Path(tempfile.mkdtemp()))
    assert other.manifest.content_hash() == release.manifest.content_hash()
    assert {k: v.sha256 for k, v in other.manifest.tables.items()} == {k: v.sha256 for k, v in release.manifest.tables.items()}


def test_release_tables_validate_against_schemas(release):
    from resilink.domain.schemas import EDGE_SCHEMA, GRID_SCHEMA, NODE_SCHEMA

    NODE_SCHEMA.validate(release.read("nodes").drop(columns=["bbox"], errors="ignore"))
    EDGE_SCHEMA.validate(release.read("edges").drop(columns=["bbox"], errors="ignore"))
    GRID_SCHEMA.validate(release.read("grid_cells").drop(columns=["bbox"], errors="ignore"))


def test_run_is_cached_and_content_addressed(small_cfg, small_run, release):
    assert small_run.run_id == compute_run_id(small_cfg, release)
    again = run_experiment(small_cfg)
    assert again.cached and again.run_id == small_run.run_id


def test_run_reproduces_bit_for_bit(small_cfg, small_run):
    scratch = ResultsStore(Path(tempfile.mkdtemp()))
    rerun = run_experiment(small_cfg, store=scratch, force=True)
    assert results_hash(scratch, rerun.run_id) == results_hash(ResultsStore(), small_run.run_id)


def test_worker_count_invariance(small_cfg):
    cfg2 = small_cfg.model_copy(update={"design": small_cfg.design.model_copy(update={"workers": 2})})
    a = run_experiment(small_cfg, store=ResultsStore(Path(tempfile.mkdtemp())), force=True)
    b = run_experiment(cfg2, store=ResultsStore(Path(tempfile.mkdtemp())), force=True)
    la = pd.read_parquet(a.path / "sample_losses.parquet")
    lb = pd.read_parquet(b.path / "sample_losses.parquet")
    pd.testing.assert_frame_equal(la, lb)


def test_metrics_are_sane(small_run):
    m = pd.read_parquet(small_run.path / "metrics.parquet")
    eal = m[(m.metric == "eal") & (m.sector == "all")].iloc[0]
    assert eal.value > 0 and eal.p05 <= eal.p50 <= eal.p95
    cv = m[m.metric.isin(["var_95", "cvar_95"]) & (m.sector == "all")].pivot(index="scenario_id", columns="metric", values="value")
    assert (cv["cvar_95"] >= cv["var_95"] - 1e-9).all()
    mean = m[(m.metric == "mean_loss") & (m.sector == "all")].set_index("scenario_id")["value"]
    assert mean["flood_rp10"] <= mean["flood_rp100"] <= mean["flood_rp1000"]  # bigger floods, bigger losses
