"""Run experiments reproducibly (blueprint §6.4, §10.3).

``run_id = run-<sha256(config without provenance, release content hash, rules file, fragility file, code)>``
so resubmitting identical inputs is a cache hit, and any change to an input file yields a new run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from resilink import __version__
from resilink.dependencies.model import infer_proximity_softmax, load_rules
from resilink.domain.config import ExperimentConfig
from resilink.fragility.lognormal import load_library
from resilink.hazards.exposure import compute_exposure, exposure_arrays
from resilink.io.hashing import sha256_file, stable_hash
from resilink.io.paths import git_state, resolve
from resilink.io.release import Release, load_release
from resilink.io.results import ResultsStore
from resilink.network.compiled import compile_network
from resilink.risk.montecarlo import CampaignInput, ScenarioInput, run_campaign
from resilink.synthetic.territory import SYNTHETIC_RELEASE_ID, ensure_synthetic_release

SECONDS_PER_SAMPLE_HINT = 0.025  # measured on the synthetic territory (Sprint 0); refined per release in P1


@dataclass(frozen=True)
class RunResult:
    run_id: str
    cached: bool
    path: Path
    manifest: dict[str, Any]


def get_release(release_id: str, root: Path | None = None) -> Release:
    if release_id == SYNTHETIC_RELEASE_ID:
        return ensure_synthetic_release(root)
    return load_release(release_id, root)


def _input_hashes(cfg: ExperimentConfig) -> dict[str, str]:
    return {
        "rules": sha256_file(resolve(cfg.dependencies.rules)),
        "fragility": sha256_file(resolve(cfg.fragility.library)),
    }


def compute_run_id(cfg: ExperimentConfig, release: Release) -> str:
    payload = {
        "config": cfg.model_dump(mode="json", exclude={"provenance"}),
        "release": release.manifest.content_hash(),
        "inputs": _input_hashes(cfg),
        "code": __version__,
    }
    return "run-" + stable_hash(payload, n=16)


def estimate(cfg: ExperimentConfig) -> dict[str, Any]:
    n_scen = len(cfg.hazard.scenarios) if cfg.hazard.scenarios else 3
    n = n_scen * cfg.monte_carlo.outer * cfg.monte_carlo.inner
    seconds = n * SECONDS_PER_SAMPLE_HINT / max(1, cfg.monte_carlo.workers)
    return {"cascade_simulations": n, "estimated_seconds": round(seconds, 1)}


def run_experiment(
    cfg: ExperimentConfig, *, store: ResultsStore | None = None, data: Path | None = None, force: bool = False
) -> RunResult:
    store = store or ResultsStore(data)
    release = get_release(cfg.release, data)
    run_id = compute_run_id(cfg, release)
    if store.exists(run_id) and not force:
        return RunResult(run_id, True, store.run_dir(run_id), store.manifest(run_id))

    started = datetime.now(UTC)
    t0 = time.perf_counter()
    net = compile_network(release)
    rules = load_rules(cfg.dependencies.rules)
    depset = infer_proximity_softmax(net, rules) if cfg.dependencies.enabled else None
    library = load_library(cfg.fragility.library)
    scen_table = release.read("hazard_scenarios").sort_values("return_period_y")
    if cfg.hazard.scenarios:
        scen_table = scen_table[scen_table["scenario_id"].isin(cfg.hazard.scenarios)]
    exposure = compute_exposure(release, scen_table["scenario_id"].tolist())
    scenarios = []
    for row in scen_table.itertuples():
        node_im, edge_im = exposure_arrays(exposure, net, row.scenario_id)
        scenarios.append(ScenarioInput(row.scenario_id, float(row.return_period_y), node_im, edge_im))
    tables = run_campaign(CampaignInput(net, depset, scenarios, library, cfg))
    tables["exposure"] = exposure
    if depset is not None:
        tables["dependencies"] = depset.to_frame(net, cfg.dependencies.lambda_scale.central())
    elapsed = time.perf_counter() - t0

    with store.writer(run_id) as w:
        for name, df in sorted(tables.items()):
            w.table(name, df)
        manifest = {
            "run_id": run_id,
            "config": cfg.model_dump(mode="json"),
            "release_id": release.release_id,
            "release_content_hash": release.manifest.content_hash(),
            "inputs": _input_hashes(cfg),
            "code": {"resilink": __version__, **git_state()},
            "models": {
                "dependencies": cfg.dependencies.model if cfg.dependencies.enabled else "none",
                "rules_version": rules.version,
                "cascade": "topological_temporal@1.0.0",
            },
            "seed_entropy": np.random.SeedSequence(cfg.monte_carlo.seed).entropy,
            "cascade_simulations": len(scenarios) * cfg.monte_carlo.outer * cfg.monte_carlo.inner,
            "elapsed_s": round(elapsed, 2),
            "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "tables": w.tables,
            "tier": cfg.outputs.tier,
        }
        w.json("manifest", manifest)
    return RunResult(run_id, False, store.run_dir(run_id), manifest)


def results_hash(store: ResultsStore, run_id: str) -> str:
    """Hash of result table contents (for `resilink reproduce`): independent of timings and git state."""
    m = store.manifest(run_id)
    return stable_hash({k: v["sha256"] for k, v in sorted(m["tables"].items())}, n=64)
