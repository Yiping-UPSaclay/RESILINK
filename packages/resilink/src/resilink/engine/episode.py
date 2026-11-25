"""Episode orchestrator 0.1 (built-in engine plugin ``episode``): Sprint 0's Monte Carlo loop, now fed by plugins.

For each scenario s and outer draw o: realise dependencies once; then for each inner draw i: damage
(fragility) -> repair plan (restoration) -> outage spread (propagation, using the service model) -> losses
(consequence). The sequence is fixed; the event-driven kernel (ADR-0008 step E4) generalises it as ``episode`` 0.2.
Results are aggregated in a fixed order, so they do not depend on the number of worker processes.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from resilink.plugin_api import Applicability, ModelCard, ModelPlugin, ScenarioInfo, SetupContext
from resilink.risk.metrics import amplification_ratio, eal_from_return_periods, var_cvar


@dataclass(frozen=True)
class Campaign:
    """Everything a worker process needs (picklable): the network, the live models and the design."""

    net: Any
    scenarios: list[ScenarioInfo]
    intensities: list[tuple[np.ndarray, np.ndarray]]
    fragility: Any
    dependency: Any
    restoration: Any
    propagation: Any
    consequence: Any
    sampler: Any
    outer: int
    inner: int
    keep_traces: int
    horizon_h: float


@dataclass
class ChunkResult:
    s: int
    o: int
    losses: list[tuple[int, int, int, str, float, float]] = field(default_factory=list)  # s,o,i,sector,total,direct
    node_down: np.ndarray | None = None
    node_downtime: np.ndarray | None = None
    cell_disrupt: dict[str, np.ndarray] = field(default_factory=dict)
    cell_outage: dict[str, np.ndarray] = field(default_factory=dict)
    traces: list[tuple[int, pd.DataFrame, pd.DataFrame]] = field(default_factory=list)  # (i, events, damage)


def _run_chunk(args: tuple[Campaign, int, int]) -> ChunkResult:
    camp, s, o = args
    net = camp.net
    node_im, edge_im = camp.intensities[s]
    deps = camp.dependency.realise(camp.sampler.outer_rng(o))
    p_node, p_edge = camp.fragility.failure_probabilities(node_im, edge_im)
    res = ChunkResult(s=s, o=o, node_down=np.zeros(net.n), node_downtime=np.zeros(net.n))
    for sector in net.sectors:
        res.cell_disrupt[sector] = np.zeros(len(net.cell_id))
        res.cell_outage[sector] = np.zeros(len(net.cell_id))
    for i in range(camp.inner):
        rng = camp.sampler.inner_rng(s, o, i)
        damage = camp.fragility.damage(rng, p_node, p_edge)
        plan = camp.restoration.plan(rng, damage)
        trace = camp.propagation.run(deps, damage, plan, camp.horizon_h)
        total, direct = trace.downtime(net.n)
        res.node_down += trace.ever_down(net.n)
        res.node_downtime += total
        for sector, oc in camp.consequence.evaluate(total, direct).items():
            res.cell_disrupt[sector] += oc.cell_disrupted
            res.cell_outage[sector] += oc.cell_outage_h
            res.losses.append((s, o, i, sector, oc.people_hours, oc.people_hours_direct))
        if o == 0 and i < camp.keep_traces:
            dn, de = np.flatnonzero(plan.node_repair_h > 0), np.flatnonzero(plan.edge_repair_h > 0)
            damage_rows = pd.DataFrame({
                "rid": np.concatenate([net.rid[dn], net.edge_rid[de]]),
                "kind": ["node"] * len(dn) + ["edge"] * len(de),
                "repair_t_h": np.round(np.concatenate([plan.node_repair_h[dn], plan.edge_repair_h[de]]), 4),
                "im_value": np.concatenate([node_im[dn], edge_im[de]]),
            })
            res.traces.append((i, trace.to_frame(net), damage_rows))
    return res


def summarise(camp: Campaign, chunks: list[ChunkResult]) -> dict[str, pd.DataFrame]:
    net = camp.net
    n_samples = camp.outer * camp.inner
    losses = pd.DataFrame(
        [row for c in chunks for row in c.losses],
        columns=["s", "o", "i", "sector", "loss_total", "loss_direct"],
    )
    losses["scenario_id"] = [camp.scenarios[s].scenario_id for s in losses["s"]]

    node_rows, cell_rows, trace_rows, damage_rows = [], [], [], []
    for s, scen in enumerate(camp.scenarios):
        sc = [c for c in chunks if c.s == s]
        down = np.sum([c.node_down for c in sc], axis=0)
        dt = np.sum([c.node_downtime for c in sc], axis=0)
        node_rows.append(pd.DataFrame({
            "scenario_id": scen.scenario_id, "rid": net.rid,
            "p_down": down / n_samples, "mean_downtime_h": dt / n_samples,
        }))
        for sector in net.sectors:
            dis = np.sum([c.cell_disrupt[sector] for c in sc], axis=0)
            out = np.sum([c.cell_outage[sector] for c in sc], axis=0)
            served_mask = np.asarray(net.supply[sector].sum(axis=1)).ravel() > 0
            cell_rows.append(pd.DataFrame({
                "scenario_id": scen.scenario_id, "sector": sector, "cell_id": net.cell_id[served_mask],
                "k": np.flatnonzero(served_mask).astype(np.uint32),
                "p_disruption": dis[served_mask] / n_samples, "mean_outage_h": out[served_mask] / n_samples,
            }))
        for c in sc:
            for i, ev, dmg in c.traces:
                trace_rows.append(ev.assign(scenario_id=scen.scenario_id, sample=f"o{c.o}-i{i}"))
                damage_rows.append(dmg.assign(scenario_id=scen.scenario_id, sample=f"o{c.o}-i{i}"))

    metrics = compute_metrics(camp, losses)
    sample_losses = losses.drop(columns=["s"])[["scenario_id", "o", "i", "sector", "loss_total", "loss_direct"]]
    return {
        "sample_losses": sample_losses,
        "node_stats": pd.concat(node_rows, ignore_index=True),
        "cell_stats": pd.concat(cell_rows, ignore_index=True),
        "metrics": metrics,
        "traces": pd.concat(trace_rows, ignore_index=True) if trace_rows else pd.DataFrame(),
        "trace_damage": pd.concat(damage_rows, ignore_index=True) if damage_rows else pd.DataFrame(),
    }


def compute_metrics(camp: Campaign, losses: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict] = []
    unit = "people-hours"
    allsec = losses.groupby(["s", "o", "i"], as_index=False)[["loss_total", "loss_direct"]].sum().assign(sector="all")
    frame = pd.concat([losses[["s", "o", "i", "sector", "loss_total", "loss_direct"]], allsec], ignore_index=True)
    rp = np.array([sc.return_period_y for sc in camp.scenarios])

    for sector, g in frame.groupby("sector", sort=True):
        per_outer_means = np.zeros((camp.outer, len(camp.scenarios)))
        for s, scen in enumerate(camp.scenarios):
            gs = g[g["s"] == s]
            x = gs["loss_total"].to_numpy()
            d = gs["loss_direct"].to_numpy()
            by_o = gs.groupby("o")["loss_total"].mean().reindex(range(camp.outer)).to_numpy()
            per_outer_means[:, s] = by_o
            p05, p50, p95 = np.percentile(by_o, [5, 50, 95])
            var95, cvar95 = var_cvar(x, 0.95)
            rows += [
                {"metric": "mean_loss", "scenario_id": scen.scenario_id, "sector": sector, "value": float(x.mean()),
                 "p05": p05, "p50": p50, "p95": p95, "unit": unit},
                {"metric": "var_95", "scenario_id": scen.scenario_id, "sector": sector, "value": var95,
                 "p05": np.nan, "p50": np.nan, "p95": np.nan, "unit": unit},
                {"metric": "cvar_95", "scenario_id": scen.scenario_id, "sector": sector, "value": cvar95,
                 "p05": np.nan, "p50": np.nan, "p95": np.nan, "unit": unit},
                {"metric": "mean_direct_loss", "scenario_id": scen.scenario_id, "sector": sector,
                 "value": float(d.mean()), "p05": np.nan, "p50": np.nan, "p95": np.nan, "unit": unit},
                {"metric": "amplification_ratio", "scenario_id": scen.scenario_id, "sector": sector,
                 "value": amplification_ratio(float(x.mean()), float(d.mean())),
                 "p05": np.nan, "p50": np.nan, "p95": np.nan, "unit": "ratio"},
                {"metric": "mc_rel_se", "scenario_id": scen.scenario_id, "sector": sector,
                 "value": float(x.std(ddof=1) / np.sqrt(len(x)) / x.mean()) if len(x) > 1 and x.mean() > 0 else np.nan,
                 "p05": np.nan, "p50": np.nan, "p95": np.nan, "unit": "ratio"},
            ]
        eal_o = np.array([eal_from_return_periods(rp, per_outer_means[o]) for o in range(camp.outer)])
        p05, p50, p95 = np.percentile(eal_o, [5, 50, 95])
        rows.append({"metric": "eal", "scenario_id": "annual", "sector": sector, "value": float(eal_o.mean()),
                     "p05": p05, "p50": p50, "p95": p95, "unit": f"{unit}/year"})
    out = pd.DataFrame(rows)
    num = ["value", "p05", "p50", "p95"]
    out[num] = out[num].astype(float).round(6)
    return out.sort_values(["metric", "scenario_id", "sector"]).reset_index(drop=True)


class EpisodeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    horizon_h: float = Field(72.0, gt=0, description="Length of the simulated episode after the hazard (hours)")


class Episode:
    def __init__(self, params: EpisodeParams, setup: SetupContext) -> None:
        self.horizon_h = params.horizon_h
        self.net = setup.net

    def run(self, models: Any, *, workers: int = 1, keep_traces: int = 5) -> dict[str, pd.DataFrame]:
        scenarios = models.hazard.scenarios()
        camp = Campaign(
            net=self.net, scenarios=scenarios, intensities=[models.hazard.intensity(s) for s in scenarios],
            fragility=models.fragility, dependency=models.dependency, restoration=models.restoration,
            propagation=models.propagation, consequence=models.consequence, sampler=models.sampler,
            outer=models.sampler.outer, inner=models.sampler.inner, keep_traces=keep_traces,
            horizon_h=self.horizon_h,
        )
        tasks = [(camp, s, o) for s in range(len(scenarios)) for o in range(camp.outer)]
        if workers > 1 and len(tasks) > 1:
            with ProcessPoolExecutor(max_workers=workers) as ex:
                chunks = list(ex.map(_run_chunk, tasks, chunksize=max(1, len(tasks) // (4 * workers))))
        else:
            chunks = [_run_chunk(t) for t in tasks]
        return summarise(camp, chunks)


episode = ModelPlugin(
    card=ModelCard(
        id="episode", version="0.1.0", slot="orchestrator",
        summary="Runs one hazard episode per simulated world in a fixed sequence (damage, repair plan, outage "
                "spread, losses) over a horizon of hours, inside the nested Monte Carlo design. The default "
                "orchestrator; version 0.2 (event-driven kernel) will let decisions react during the episode.",
        authors=("RESILINK core team",), licence="Apache-2.0", maturity="experimental",
        applies_to=Applicability(time_scale="episode")),
    params=EpisodeParams, factory=Episode,
)
