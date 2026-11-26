"""Built-in operations (Sprint 0 catalogue). Each op = typed input/output + side-effect class + tier.

Descriptions state WHEN to call the op (they are the agent-facing tool descriptions).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field

from resilink.cascade.explain import explain
from resilink.dependencies.model import DependencySet, infer_proximity_softmax, load_rules
from resilink.domain.config import ExperimentConfig
from resilink.domain.vocab import Tier
from resilink.engine.compose import CompositionError, compose
from resilink.etl.staging import list_staged
from resilink.experiments.runner import compute_run_id, estimate, get_release, run_experiment
from resilink.hazards.exposure import compute_exposure
from resilink.io.hashing import stable_hash
from resilink.io.paths import load_yaml, resolve
from resilink.io.release import list_releases
from resilink.io.results import ResultsStore
from resilink.knowledge.store import KnowledgeStore, ValueRef
from resilink.network.compiled import CompiledNetwork, compile_network
from resilink.ops.registry import OpContext, OpDenied, SideEffect, op
from resilink.registry import system_catalog
from resilink.views import ViewSpec, required_tier, validate_view

SYNTHETIC_TERRITORIES = {"synthetic"}


# ------------------------------------------------------------------------------------------- helpers
@lru_cache(maxsize=8)
def _network(release_id: str, data: str | None) -> CompiledNetwork:
    return compile_network(get_release(release_id, Path(data) if data else None))


@lru_cache(maxsize=8)
def _depset(release_id: str, data: str | None) -> DependencySet:
    rules = load_rules(resolve("knowledge/dependency_rules.yaml"))
    return infer_proximity_softmax(_network(release_id, data), rules, known_systems=system_catalog().systems)


def _data(ctx: OpContext) -> str | None:
    return str(ctx.data) if ctx.data else None


def _store(ctx: OpContext) -> ResultsStore:
    return ResultsStore(ctx.data)


def _knowledge(ctx: OpContext) -> KnowledgeStore:
    ks = ctx.extras.get("knowledge")
    if ks is None:
        ks = KnowledgeStore(ctx.data / "knowledge" / "knowledge.duckdb" if ctx.data else None)
        ctx.extras["knowledge"] = ks
    return ks


def _require_tier(ctx: OpContext, release_id: str, needed_real: Tier, what: str) -> None:
    """Data-dependent tier check: synthetic data is non-sensitive; real asset-level outputs keep their tier."""
    territory = release_id.split("-", 1)[0]
    needed = Tier.PUBLIC if territory in SYNTHETIC_TERRITORIES else needed_real
    if ctx.tier < needed:
        raise OpDenied(f"{what} on {release_id} requires '{needed.label}' clearance (principal has '{ctx.tier.label}')")


def _records(df: pd.DataFrame, limit: int | None = None) -> list[dict[str, Any]]:
    if limit is not None:
        df = df.head(limit)
    return df.astype(object).where(pd.notna(df), None).to_dict(orient="records")


# ------------------------------------------------------------------------------------------- catalog
class Empty(BaseModel):
    pass


class ReleaseInfo(BaseModel):
    release_id: str
    territory: str
    tables: dict[str, int]


class ReleaseList(BaseModel):
    releases: list[ReleaseInfo]


@op("catalog.list_releases", side_effect=SideEffect.READ, min_tier="public", cost_hint="ms")
def list_releases_op(inp: Empty, ctx: OpContext) -> ReleaseList:
    """List published data releases (territory + table row counts). Call first when the user asks what data
    the lab has, or before choosing a release for an experiment."""
    get_release("synthetic-2026.11.0", ctx.data)  # ensure the demo release exists
    out = [ReleaseInfo(release_id=m.release_id, territory=str(m.territory.get("id", "")),
                       tables={k: v.rows for k, v in m.tables.items()}) for m in list_releases(ctx.data)]
    return ReleaseList(releases=out)


class DescribeTableIn(BaseModel):
    table: str = Field(description="Table name, e.g. nodes, metrics, cell_stats, node_stats, exposure")


class TableDescription(BaseModel):
    table: str
    tier: str
    description: str
    columns: dict[str, str]


@op("catalog.describe_table", side_effect=SideEffect.READ, min_tier="public", cost_hint="ms")
def describe_table(inp: DescribeTableIn, ctx: OpContext) -> TableDescription:
    """Return the meaning, unit and sensitivity tier of each column of a table. Call before interpreting any
    column you are not sure about — never guess column semantics."""
    spec = load_yaml("configs/layers.yaml")["tables"].get(inp.table)
    if spec is None:
        raise ValueError(f"unknown table {inp.table!r}")
    return TableDescription(table=inp.table, tier=spec["tier"], description=spec["description"], columns=spec["columns"])


class StagedIn(BaseModel):
    territory: str = Field("bdr", description="Territory id, e.g. 'bdr' (Bouches-du-Rhône)")


class StagedTableInfo(BaseModel):
    table: str
    rows: int
    sensitivity: str
    adapter: str
    snapshots: list[str]
    licences: dict[str, str]
    qa: dict[str, Any]
    staged_at: str


class StagedOut(BaseModel):
    territory: str
    tables: list[StagedTableInfo]
    withheld: int = Field(description="Staged tables above the caller's clearance (not listed)")


@op("etl.staged_tables", side_effect=SideEffect.READ, min_tier="public", cost_hint="ms")
def staged_tables(inp: StagedIn, ctx: OpContext) -> StagedOut:
    """List the canonical tables built for a real territory from open-data snapshots but not yet published as a
    release: rows, source snapshot ids, adapter version, licences and QA figures (e.g. population checked against
    INSEE). Call when the user asks what real data has been ingested, or before judging a data-quality question."""
    recs = list_staged(inp.territory, ctx.data)
    visible = [r for r in recs if Tier.parse(r.sensitivity) <= ctx.tier]
    return StagedOut(territory=inp.territory, withheld=len(recs) - len(visible), tables=[
        StagedTableInfo(table=r.table, rows=r.rows, sensitivity=r.sensitivity, adapter=r.adapter,
                        snapshots=r.snapshots, licences=r.licences, qa=r.qa, staged_at=r.staged_at.isoformat())
        for r in visible])


# ------------------------------------------------------------------------------------------- data
class AssetIn(BaseModel):
    release: str = "synthetic-2026.11.0"
    rid: str


class AssetOut(BaseModel):
    rid: str
    layer: str
    asset_class: str
    name: str
    is_source: bool
    x: float
    y: float


@op("data.get_asset", side_effect=SideEffect.READ, min_tier="internal", cost_hint="ms")
def get_asset(inp: AssetIn, ctx: OpContext) -> AssetOut:
    """Get the attributes of one infrastructure asset by rid. Call when the user names or selects an asset."""
    rel = get_release(inp.release, ctx.data)
    nodes = rel.read("nodes")
    row = nodes[nodes["rid"] == inp.rid]
    if row.empty:
        raise ValueError(f"asset {inp.rid} not found in {inp.release}")
    r = row.iloc[0]
    return AssetOut(rid=r["rid"], layer=r["layer"], asset_class=r["class"], name=str(r["name"]),
                    is_source=bool(r["is_source"]), x=float(r.geometry.x), y=float(r.geometry.y))


class AggregateIn(BaseModel):
    release: str = "synthetic-2026.11.0"
    what: Literal["assets_by_class", "population_by_commune"] = "assets_by_class"


class AggregateOut(BaseModel):
    rows: list[dict[str, Any]]


@op("data.aggregate", side_effect=SideEffect.READ, min_tier="internal", cost_hint="ms")
def aggregate(inp: AggregateIn, ctx: OpContext) -> AggregateOut:
    """Aggregate a release: asset counts by layer/class, or population by commune. Call for inventory questions."""
    rel = get_release(inp.release, ctx.data)
    if inp.what == "assets_by_class":
        df = rel.read("nodes").groupby(["layer", "class"], as_index=False).size().rename(columns={"size": "count"})
    else:
        df = rel.read("grid_cells").groupby("admin_commune", as_index=False)["pop"].sum().round(1)
    return AggregateOut(rows=_records(df))


# ------------------------------------------------------------------------------------------- network
class DependenciesIn(BaseModel):
    release: str = "synthetic-2026.11.0"
    rid: str
    direction: Literal["supporters", "dependents"] = "supporters"


class DependencyEdge(BaseModel):
    dependent_rid: str
    supporter_rid: str
    p: float
    distance_m: float
    mechanism: str
    rule_id: str
    group_mode: str


class DependenciesOut(BaseModel):
    rid: str
    direction: str
    model: str
    edges: list[DependencyEdge]


@op("network.dependencies_of", side_effect=SideEffect.READ, min_tier="internal", cost_hint="ms")
def dependencies_of(inp: DependenciesIn, ctx: OpContext) -> DependenciesOut:
    """Return the inferred cross-infrastructure dependencies of an asset with probabilities: its supporters
    (what it relies on) or its dependents (what relies on it). Call when the user asks what an asset depends on
    or what would be affected if it failed."""
    net = _network(inp.release, _data(ctx))
    df = _depset(inp.release, _data(ctx)).to_frame(net)
    key = "dependent_rid" if inp.direction == "supporters" else "supporter_rid"
    sel = df[df[key] == inp.rid].sort_values("p", ascending=False)
    edges = [DependencyEdge(**{k: r[k] for k in DependencyEdge.model_fields}) for r in _records(sel)]
    return DependenciesOut(rid=inp.rid, direction=inp.direction, model="proximity_softmax@1.0.0", edges=edges)


# ------------------------------------------------------------------------------------------- hazards
class ReleaseIn(BaseModel):
    release: str = "synthetic-2026.11.0"


class ScenarioList(BaseModel):
    scenarios: list[dict[str, Any]]


@op("hazards.list_scenarios", side_effect=SideEffect.READ, min_tier="public", cost_hint="ms")
def list_scenarios(inp: ReleaseIn, ctx: OpContext) -> ScenarioList:
    """List hazard scenarios of a release with return periods. Call before designing or explaining an experiment."""
    df = get_release(inp.release, ctx.data).read("hazard_scenarios")
    return ScenarioList(scenarios=_records(df[["scenario_id", "hazard", "return_period_y", "im_type", "im_unit"]]))


class ExposureIn(BaseModel):
    release: str = "synthetic-2026.11.0"
    scenario_id: str


class ExposureOut(BaseModel):
    scenario_id: str
    by_class: list[dict[str, Any]]
    exposed_assets: list[dict[str, Any]]


@op("hazards.exposure_summary", side_effect=SideEffect.READ, min_tier="internal", cost_hint="s")
def exposure_summary(inp: ExposureIn, ctx: OpContext) -> ExposureOut:
    """Which assets a hazard scenario reaches and with what intensity (e.g. water depth), grouped by class.
    Call when the user asks what is exposed to a flood scenario (exposure, not failure probability)."""
    rel = get_release(inp.release, ctx.data)
    ex = compute_exposure(rel, [inp.scenario_id])
    nodes = rel.read("nodes")[["rid", "class", "name"]]
    edges = rel.read("edges")[["rid", "class", "name"]]
    meta = pd.concat([nodes, edges], ignore_index=True).rename(columns={"rid": "asset_rid"})
    ex = ex.merge(meta, on="asset_rid", how="left")
    by = ex.groupby(["asset_kind", "class"], as_index=False).agg(count=("asset_rid", "size"), max_im=("im_value", "max"))
    return ExposureOut(scenario_id=inp.scenario_id, by_class=_records(by),
                       exposed_assets=_records(ex[["asset_rid", "class", "name", "im_value"]].sort_values("im_value", ascending=False)))


# ------------------------------------------------------------------------------------------- experiments
class ExperimentIn(BaseModel):
    config: ExperimentConfig = Field(description="Experiment configuration (see configs/experiments/*.yaml)")


class ValidateOut(BaseModel):
    valid: bool
    run_id: str | None
    already_computed: bool
    cascade_simulations: int
    estimated_seconds: float
    within_quota: bool
    errors: list[str]


@op("experiments.validate", side_effect=SideEffect.READ, min_tier="public", cost_hint="s")
def validate_experiment(inp: ExperimentIn, ctx: OpContext) -> ValidateOut:
    """Validate an experiment config and estimate its cost without running it. Call before proposing or
    submitting any experiment; show the estimate to the user."""
    cfg = inp.config
    try:
        release = get_release(cfg.release, ctx.data)
        composition = compose(cfg)  # every model resolves, versions match, parameters and files are valid
    except (FileNotFoundError, CompositionError) as exc:
        return ValidateOut(valid=False, run_id=None, already_computed=False, cascade_simulations=0,
                           estimated_seconds=0, within_quota=False, errors=[str(exc)])
    est = estimate(cfg)
    rid = compute_run_id(cfg, release, composition)
    return ValidateOut(valid=True, run_id=rid, already_computed=_store(ctx).exists(rid),
                       cascade_simulations=est["cascade_simulations"], estimated_seconds=est["estimated_seconds"],
                       within_quota=est["cascade_simulations"] <= ctx.max_cascade_simulations, errors=[])


class SubmitOut(BaseModel):
    run_id: str
    cached: bool
    elapsed_s: float
    cascade_simulations: int


@op("experiments.submit", side_effect=SideEffect.COMPUTE, min_tier="internal", cost_hint="min")
def submit_experiment(inp: ExperimentIn, ctx: OpContext) -> SubmitOut:
    """Run (or fetch from cache) a Monte Carlo experiment. Deterministic and content-addressed: identical
    configs return the same run_id. Call only after experiments.validate and within quota."""
    cfg = inp.config
    est = estimate(cfg)
    if est["cascade_simulations"] > ctx.max_cascade_simulations:
        raise OpDenied(f"{est['cascade_simulations']} simulations exceed quota {ctx.max_cascade_simulations}")
    res = run_experiment(cfg, data=ctx.data)
    return SubmitOut(run_id=res.run_id, cached=res.cached, elapsed_s=float(res.manifest.get("elapsed_s", 0.0)),
                     cascade_simulations=int(res.manifest["cascade_simulations"]))


class RunList(BaseModel):
    runs: list[dict[str, Any]]


@op("experiments.list_runs", side_effect=SideEffect.READ, min_tier="public", cost_hint="ms")
def list_runs(inp: Empty, ctx: OpContext) -> RunList:
    """List completed runs (run_id, experiment, release). Call to find existing results before running anything."""
    return RunList(runs=_store(ctx).list_runs())


class MetricsIn(BaseModel):
    run_id: str
    metric: str | None = Field(None, description="e.g. eal, mean_loss, var_95, cvar_95, amplification_ratio")
    scenario_id: str | None = None
    sector: str | None = Field(None, description="elec | gas | telecom | all")


class MetricsOut(BaseModel):
    run_id: str
    rows: list[dict[str, Any]]


@op("experiments.metrics", side_effect=SideEffect.READ, min_tier="internal", cost_hint="ms")
def metrics(inp: MetricsIn, ctx: OpContext) -> MetricsOut:
    """Risk metrics of a run (EAL, mean loss, VaR/CVaR 95 %, amplification ratio; values in people-hours with
    5/50/95 % epistemic bands). Call to answer any quantitative risk question; quote values exactly."""
    df = _store(ctx).read(inp.run_id, "metrics")
    for col in ("metric", "scenario_id", "sector"):
        v = getattr(inp, col)
        if v is not None:
            df = df[df[col] == v]
    return MetricsOut(run_id=inp.run_id, rows=_records(df))


class TopAssetsIn(BaseModel):
    run_id: str
    scenario_id: str
    layer: Literal["elec", "gas", "telecom"] | None = None
    limit: int = Field(10, ge=1, le=50)


class TopAssetsOut(BaseModel):
    run_id: str
    scenario_id: str
    assets: list[dict[str, Any]]


@op("experiments.top_assets", side_effect=SideEffect.READ, min_tier="public", cost_hint="ms")
def top_assets(inp: TopAssetsIn, ctx: OpContext) -> TopAssetsOut:
    """Assets ranked by probability of being down (and mean downtime) in a scenario. Asset-level vulnerability:
    restricted tier for real territories. Call when the user asks which assets are most likely to fail."""
    store = _store(ctx)
    release_id = store.manifest(inp.run_id)["release_id"]
    _require_tier(ctx, release_id, Tier.RESTRICTED, "asset-level vulnerability")
    ns = store.read(inp.run_id, "node_stats")
    ns = ns[ns["scenario_id"] == inp.scenario_id]
    nodes = get_release(release_id, ctx.data).read("nodes")[["rid", "layer", "class", "name"]]
    ns = ns.merge(nodes, on="rid", how="left")
    if inp.layer:
        ns = ns[ns["layer"] == inp.layer]
    ns = ns.sort_values(["p_down", "mean_downtime_h"], ascending=False)
    ns[["p_down", "mean_downtime_h"]] = ns[["p_down", "mean_downtime_h"]].round(4)
    return TopAssetsOut(run_id=inp.run_id, scenario_id=inp.scenario_id,
                        assets=_records(ns[["rid", "layer", "class", "name", "p_down", "mean_downtime_h"]], inp.limit))


class ExplainIn(BaseModel):
    run_id: str
    scenario_id: str
    rid: str
    sample: str | None = Field(None, description="Kept sample id like 'o0-i1'; default = first sample where the asset fails")


class ExplainOut(BaseModel):
    run_id: str
    scenario_id: str
    sample: str | None
    steps: list[dict[str, Any]]


@op("experiments.trace_explain", side_effect=SideEffect.READ, min_tier="public", cost_hint="ms")
def trace_explain(inp: ExplainIn, ctx: OpContext) -> ExplainOut:
    """Deterministic causal chain explaining why an asset failed in a stored cascade sample
    (asset <- cause <- ... <- hazard damage). Call when the user asks WHY something failed; narrate the steps."""
    store = _store(ctx)
    release_id = store.manifest(inp.run_id)["release_id"]
    _require_tier(ctx, release_id, Tier.RESTRICTED, "asset-level cascade explanation")
    tr = store.read(inp.run_id, "traces")
    dmg = store.read(inp.run_id, "trace_damage")
    tr = tr[tr["scenario_id"] == inp.scenario_id]
    dmg = dmg[dmg["scenario_id"] == inp.scenario_id]
    sample = inp.sample
    if sample is None:
        hit = tr[(tr["rid"] == inp.rid) & (tr["event"] == "fail")]
        sample = str(hit["sample"].iloc[0]) if len(hit) else None
    if sample is None:
        return ExplainOut(run_id=inp.run_id, scenario_id=inp.scenario_id, sample=None,
                          steps=[{"rid": inp.rid, "cause": "functional", "detail": "did not fail in any kept sample"}])
    net = _network(release_id, _data(ctx))
    steps = explain(net, tr[tr["sample"] == sample], dmg[dmg["sample"] == sample], inp.rid)
    return ExplainOut(run_id=inp.run_id, scenario_id=inp.scenario_id, sample=sample,
                      steps=[s.__dict__ for s in steps])


# ------------------------------------------------------------------------------------------- knowledge
class SearchIn(BaseModel):
    query: str
    limit: int = Field(6, ge=1, le=20)


class SearchOut(BaseModel):
    hits: list[dict[str, Any]]


@op("knowledge.search", side_effect=SideEffect.READ, min_tier="public", cost_hint="ms")
def knowledge_search(inp: SearchIn, ctx: OpContext) -> SearchOut:
    """BM25 search over curated lab knowledge: dependency rules, assumptions (with grades), ontology classes,
    fragility curves. Call when the user asks how the model works or which assumption drives a result."""
    ks = _knowledge(ctx)
    if ks.entity_count() == 0:
        ks.refresh_curated()
    df = ks.search(inp.query, inp.limit)
    df["score"] = df["score"].round(4)
    return SearchOut(hits=_records(df[["kid", "kind", "label", "text", "score"]]))


class FindingIn(BaseModel):
    statement: str = Field(description="One-sentence claim; every number must come from a cited ValueRef")
    value_refs: list[ValueRef]
    run_ids: list[str]
    release_id: str


class ProposalOut(BaseModel):
    proposal_id: str
    status: str


@op("findings.propose", side_effect=SideEffect.PROPOSE, min_tier="internal", cost_hint="ms")
def propose_finding(inp: FindingIn, ctx: OpContext) -> ProposalOut:
    """Propose a finding for the lab memory. It enters the human Review queue; nothing is accepted automatically."""
    pid = _knowledge(ctx).add_proposal("finding", inp.statement[:120], inp.model_dump(mode="json"), ctx.principal,
                                       ctx.extras.get("provenance_id"))
    return ProposalOut(proposal_id=pid, status="proposed")


class ProposalsIn(BaseModel):
    status: Literal["proposed", "approved", "rejected"] | None = "proposed"


class ProposalsOut(BaseModel):
    proposals: list[dict[str, Any]]


@op("proposals.list", side_effect=SideEffect.READ, min_tier="internal", cost_hint="ms")
def proposals_list(inp: ProposalsIn, ctx: OpContext) -> ProposalsOut:
    """List proposals in the Review queue (findings, mappings, evidence, priors)."""
    df = _knowledge(ctx).list_proposals(inp.status)
    return ProposalsOut(proposals=_records(df[["proposal_id", "kind", "title", "status", "created_by", "created_at"]]))


class ViewIn(BaseModel):
    view: ViewSpec


class ViewOut(BaseModel):
    view_id: str
    valid: bool
    errors: list[str]
    preview: dict[str, Any]


@op("views.render", side_effect=SideEffect.READ, min_tier="public", cost_hint="ms")
def views_render(inp: ViewIn, ctx: OpContext) -> ViewOut:
    """Show a map and/or charts to the user. Values are never typed in: every chart/map colour references stored
    results via ValueRef (run_id + table + column + filters, or a call_id). Call whenever a visual would help;
    fix and retry if errors are returned. The UI renders only views that validate."""
    for layer in inp.view.map_layers:
        if layer.color_by is not None:
            _require_tier_ref(ctx, layer.color_by)
    for chart in inp.view.charts:
        _require_tier_ref(ctx, chart.data)
    errors, preview = validate_view(inp.view, ctx.data, _knowledge(ctx))
    vid = "view-" + stable_hash(inp.view.model_dump(mode="json"), n=12)
    if not errors:
        ctx.extras.setdefault("views", []).append(inp.view)
    return ViewOut(view_id=vid, valid=not errors, errors=errors, preview=preview)


def _require_tier_ref(ctx: OpContext, ref: ValueRef) -> None:
    needed = Tier.parse(required_tier(ref, ctx.data))
    if ctx.tier < needed:
        raise OpDenied(f"view data {ref.table or ref.call_id} requires '{needed.label}' clearance")


class DecideIn(BaseModel):
    proposal_id: str
    approve: bool
    note: str = ""


class DecideOut(BaseModel):
    proposal_id: str
    status: str


@op("proposals.decide", side_effect=SideEffect.ADMIN, min_tier="internal", cost_hint="ms")
def proposals_decide(inp: DecideIn, ctx: OpContext) -> DecideOut:
    """Approve or reject a proposal. Humans only (invariant I2)."""
    status = _knowledge(ctx).decide(inp.proposal_id, inp.approve, ctx.principal, inp.note)
    return DecideOut(proposal_id=inp.proposal_id, status=status)
