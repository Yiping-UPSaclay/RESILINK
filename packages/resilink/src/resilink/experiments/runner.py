"""Run experiments reproducibly (blueprint §6.13, §10.3; ADR-0008).

``run_id = run-<sha256(config without provenance, release content hash, the exact identity of every component
(model, version, distribution, code hash, parameters, hashes of the files it names), the system packs used, code)>``
so resubmitting identical inputs is a cache hit, and any change to a model, its parameters or its input files
yields a new run. Models are chosen by the experiment configuration and resolved through the plugin registry.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from resilink import __version__
from resilink.domain.config import ExperimentConfig
from resilink.engine.compose import Composition, compose, instantiate
from resilink.io.hashing import stable_hash
from resilink.io.paths import git_state
from resilink.io.release import Release, load_release
from resilink.io.results import ResultsStore
from resilink.network.compiled import compile_network
from resilink.plugin_api import PLUGIN_API_VERSION
from resilink.registry import get_registry
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


def _system_pins(release: Release) -> dict[str, dict[str, Any]]:
    """Pins of the system packs this release uses (packs installed but unused do not affect the run)."""
    registry = get_registry()
    present = sorted(set(release.read("nodes")["layer"]))
    return {s: {"version": registry.system(s).version, **asdict(registry.pin("systems", s))} for s in present}


def compute_run_id(cfg: ExperimentConfig, release: Release, composition: Composition | None = None) -> str:
    composition = composition or compose(cfg)
    payload = {
        # Components, orchestrator and design enter through `components` below, with their parameters validated
        # and defaults filled in, so equivalent spellings (72 vs 72.0, a default written or omitted) give the same
        # run, and the number of worker processes (which never changes results) does not enter at all.
        "config": cfg.model_dump(mode="json", exclude={"provenance", "components", "orchestrator", "design"}),
        "release": release.manifest.content_hash(),
        "components": composition.identity(),
        "systems": _system_pins(release),
        "plugin_api": PLUGIN_API_VERSION,
        "code": __version__,
    }
    return "run-" + stable_hash(payload, n=16)


def estimate(cfg: ExperimentConfig) -> dict[str, Any]:
    scenarios = cfg.components.hazard.params.get("scenarios")
    n_scen = len(scenarios) if scenarios else 3
    n = n_scen * cfg.design.outer * cfg.design.inner
    seconds = n * SECONDS_PER_SAMPLE_HINT / max(1, cfg.design.workers)
    return {"cascade_simulations": n, "estimated_seconds": round(seconds, 1)}


def run_experiment(
    cfg: ExperimentConfig, *, store: ResultsStore | None = None, data: Path | None = None, force: bool = False
) -> RunResult:
    store = store or ResultsStore(data)
    release = get_release(cfg.release, data)
    composition = compose(cfg)
    run_id = compute_run_id(cfg, release, composition)
    if store.exists(run_id) and not force:
        return RunResult(run_id, True, store.run_dir(run_id), store.manifest(run_id))

    started = datetime.now(UTC)
    t0 = time.perf_counter()
    net = compile_network(release)
    live = instantiate(composition, release, net, get_registry().system_catalog().systems)
    tables = live.orchestrator.run(live, workers=cfg.design.workers, keep_traces=cfg.outputs.keep_traces)
    tables["exposure"] = live.hazard.exposure_table()
    dependencies = live.dependency.table()
    if dependencies is not None:
        tables["dependencies"] = dependencies
    elapsed = time.perf_counter() - t0
    n_scenarios = len(live.hazard.scenarios())

    with store.writer(run_id) as w:
        for name, df in sorted(tables.items()):
            w.table(name, df)
        manifest = {
            "run_id": run_id,
            "config": cfg.model_dump(mode="json", by_alias=True),
            "release_id": release.release_id,
            "release_content_hash": release.manifest.content_hash(),
            "code": {"resilink": __version__, **git_state()},
            "plugin_api": PLUGIN_API_VERSION,
            "models": composition.identity(),
            "systems": _system_pins(release),
            "maturity": composition.lowest_maturity,
            "seed_entropy": np.random.SeedSequence(cfg.design.seed).entropy,
            "cascade_simulations": n_scenarios * cfg.design.outer * cfg.design.inner,
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
