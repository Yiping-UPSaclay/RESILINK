"""`resilink` CLI — a generated projection of the Operation Registry plus pipeline commands (invariant I7:
everything an agent can do, a human can do here, with AI switched off)."""

from __future__ import annotations

import getpass
import json
import shutil
import tempfile
from pathlib import Path

import typer

from resilink.domain.vocab import Tier
from resilink.experiments.runner import results_hash, run_experiment
from resilink.io.paths import load_experiment
from resilink.io.results import ResultsStore
from resilink.knowledge.store import KnowledgeStore
from resilink.ops import REGISTRY, SideEffect, call, load_builtin_ops
from resilink.ops.recording import make_context

app = typer.Typer(no_args_is_help=True, help="RESILINK digital lab")
ops_app = typer.Typer(no_args_is_help=True, help="Operation Registry (same ops as MCP / agents / UI)")
kn_app = typer.Typer(no_args_is_help=True, help="Knowledge plane")
etl_app = typer.Typer(no_args_is_help=True, help="Open-data connectors, raw snapshots, drift detection")
plugins_app = typer.Typer(no_args_is_help=True, help="Registered system packs, slots and models (ADR-0008)")
app.add_typer(ops_app, name="ops")
app.add_typer(kn_app, name="knowledge")
app.add_typer(etl_app, name="etl")
app.add_typer(plugins_app, name="plugins")


@plugins_app.command("list")
def plugins_list() -> None:
    """Show installed system packs, slots and models, their pinned code, and any plugin refused (with why)."""
    from resilink.registry import get_registry

    typer.echo(json.dumps(get_registry().describe(), indent=2, ensure_ascii=False))


@etl_app.command("describe")
def etl_describe(source_id: str) -> None:
    """Show live portal metadata (fields, geometry, licence, last modification) for a registered source."""
    from resilink.etl.connectors import CONNECTORS
    from resilink.etl.contracts import load_sources

    spec = load_sources()[source_id]
    meta = CONNECTORS[spec.connector].describe(spec)
    typer.echo(json.dumps(meta.model_dump(exclude={"raw"}), ensure_ascii=False, indent=2))


@etl_app.command("fetch")
def etl_fetch(source_id: str, territory: str = "configs/territories/bdr.yaml", limit: int | None = None,
              accept_drift: bool = False) -> None:
    """Fetch a territory-filtered raw snapshot (immutable; fails on breaking schema drift). Then `dvc add` it."""
    from resilink.etl.contracts import load_sources
    from resilink.etl.snapshot import DriftError, fetch_snapshot
    from resilink.io.paths import load_territory

    try:
        snap, report = fetch_snapshot(load_sources()[source_id], load_territory(territory), limit=limit,
                                      accept_drift=accept_drift)
    except DriftError as exc:
        typer.echo(f"DRIFT — human decision required:\n{exc.report.model_dump_json(indent=2)}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(json.dumps({"snapshot_id": snap.snapshot_id, "path": snap.path, "rows": snap.rows,
                           "drift": report.model_dump() if report else None}, ensure_ascii=False, indent=2))


@etl_app.command("adapters")
def etl_adapters(territory: str = "configs/territories/bdr.yaml") -> None:
    """List adapters (sources -> canonical tables) in run order, with what is staged for the territory."""
    from resilink.etl.adapters import ADAPTERS
    from resilink.etl.staging import list_staged, ordered
    from resilink.io.paths import load_territory

    staged = {s.table: s for s in list_staged(load_territory(territory).id)}
    for a in ordered(ADAPTERS):
        state = ", ".join(f"{t}: {staged[t].rows} rows ({staged[t].adapter})" if t in staged else f"{t}: not staged"
                          for t in a.produces)
        typer.echo(f"{a.id}@{a.version}  sources={list(a.sources)}  needs={list(a.needs)}\n    {state}")


@etl_app.command("stage")
def etl_stage(adapter_id: str = typer.Argument(None, help="Adapter id; all adapters in run order if omitted"),
              territory: str = "configs/territories/bdr.yaml") -> None:
    """Build canonical tables for the territory from the latest raw snapshots (data/staging/<territory>/)."""
    from resilink.etl.adapters import ADAPTERS
    from resilink.etl.staging import ordered, stage
    from resilink.io.paths import load_territory

    terr = load_territory(territory)
    for a in [ADAPTERS[adapter_id]] if adapter_id else ordered(ADAPTERS):
        for rec in stage(a.id, terr):
            typer.echo(json.dumps(rec.model_dump(mode="json", exclude={"staged_at"}), ensure_ascii=False, indent=2))


@app.command()
def demo(config: str = "configs/experiments/synthetic_demo.yaml") -> None:
    """Build the synthetic release (if needed), run the demo experiment and print headline metrics."""
    cfg = load_experiment(config)
    res = run_experiment(cfg)
    m = ResultsStore().read(res.run_id, "metrics")
    eal = m[(m["metric"] == "eal") & (m["sector"] == "all")].iloc[0]
    typer.echo(f"run_id={res.run_id} cached={res.cached} simulations={res.manifest['cascade_simulations']} "
               f"elapsed_s={res.manifest['elapsed_s']}")
    typer.echo(f"EAL (all sectors) = {eal['value']:.0f} people-hours/year  [p05 {eal['p05']:.0f}, p95 {eal['p95']:.0f}]")
    amp = m[(m["metric"] == "amplification_ratio") & (m["sector"] == "all")]
    for row in amp.itertuples():
        typer.echo(f"amplification ratio {row.scenario_id}: {row.value:.2f}")


@app.command()
def run(config: str, force: bool = False) -> None:
    """Run an experiment config (cache hit if identical inputs were already run)."""
    res = run_experiment(load_experiment(config), force=force)
    typer.echo(json.dumps({"run_id": res.run_id, "cached": res.cached, "path": str(res.path)}))


@app.command()
def reproduce(run_id: str) -> None:
    """Re-execute a run in a scratch store and compare result-table hashes (no LLM involved, invariant I3)."""
    from resilink.domain.config import ExperimentConfig

    store = ResultsStore()
    cfg = ExperimentConfig.model_validate(store.manifest(run_id)["config"])
    scratch = Path(tempfile.mkdtemp(prefix="resilink-repro-"))
    try:
        res = run_experiment(cfg, store=ResultsStore(scratch), force=True)
        a, b = results_hash(store, run_id), results_hash(ResultsStore(scratch), res.run_id)
        typer.echo(f"original {a[:16]}  reproduced {b[:16]}  -> {'IDENTICAL' if a == b else 'DIFFERENT'}")
        raise typer.Exit(0 if a == b else 1)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


@ops_app.command("list")
def ops_list() -> None:
    """List operations with side-effect class and minimum tier."""
    load_builtin_ops()
    for s in REGISTRY.all():
        typer.echo(f"{s.name:32s} {s.side_effect.label:8s} {s.min_tier.label:10s} {s.cost_hint:4s} {s.summary.splitlines()[0][:90]}")


@ops_app.command("schema")
def ops_schema(name: str) -> None:
    """Print the strict JSON input schema of an op (what agents and MCP clients see)."""
    load_builtin_ops()
    typer.echo(json.dumps(REGISTRY.get(name).input_json_schema(), indent=2))


@ops_app.command("call")
def ops_call(
    name: str,
    payload: str = typer.Argument("{}", help="JSON input for the op"),
    tier: str = "internal",
    autonomy: str = "compute",
) -> None:
    """Call an op as a human principal, e.g. resilink ops call hazards.list_scenarios '{}'."""
    load_builtin_ops()
    principal = f"human:{getpass.getuser()}"
    ctx = make_context(principal, tier=Tier.parse(tier), autonomy=SideEffect[autonomy.upper()])
    res = call(name, json.loads(payload), ctx)
    typer.echo(res.to_json())


@kn_app.command("refresh")
def knowledge_refresh() -> None:
    """Re-index curated knowledge (rules, assumptions, ontology, fragility) for BM25 search."""
    typer.echo(f"indexed {KnowledgeStore().refresh_curated()} entities")


@kn_app.command("search")
def knowledge_search(query: str, limit: int = 6) -> None:
    ks = KnowledgeStore()
    if ks.entity_count() == 0:
        ks.refresh_curated()
    typer.echo(ks.search(query, limit)[["kid", "label", "score"]].to_string(index=False))


if __name__ == "__main__":
    app()
