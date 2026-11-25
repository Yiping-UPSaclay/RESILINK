"""Nested Monte Carlo campaign.

Outer loop o (epistemic): dependency realisation + scaled priors, shared across scenarios ("same world").
Inner loop i (aleatory): damage sampling, repair times.
Seeds: ``SeedSequence(seed, spawn_key=...)`` so every sample is reproducible and independent of how the
campaign is split across worker processes (results are also aggregated in a fixed order).
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from resilink.cascade.topological import TopologicalTemporalCascade
from resilink.dependencies.model import DependencyRealisation, DependencySet
from resilink.domain.config import ExperimentConfig
from resilink.fragility.lognormal import FragilityLibrary, sample_damage
from resilink.network.compiled import CompiledNetwork
from resilink.risk.metrics import amplification_ratio, eal_from_return_periods, var_cvar


@dataclass(frozen=True)
class ScenarioInput:
    scenario_id: str
    return_period_y: float
    node_im: np.ndarray
    edge_im: np.ndarray


@dataclass(frozen=True)
class CampaignInput:
    net: CompiledNetwork
    depset: DependencySet | None
    scenarios: list[ScenarioInput]
    library: FragilityLibrary
    cfg: ExperimentConfig


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


def outer_rng(seed: int, o: int) -> np.random.Generator:
    return np.random.default_rng(np.random.SeedSequence(seed, spawn_key=(0, o)))


def inner_rng(seed: int, s: int, o: int, i: int) -> np.random.Generator:
    return np.random.default_rng(np.random.SeedSequence(seed, spawn_key=(1, s, o, i)))


def realise_dependencies(inp: CampaignInput, o: int) -> DependencyRealisation:
    if inp.depset is None or not inp.cfg.dependencies.enabled:
        return DependencyRealisation.empty()
    rng = outer_rng(inp.cfg.monte_carlo.seed, o)
    lam = inp.cfg.dependencies.lambda_scale.sample(rng)
    bsc = inp.cfg.dependencies.backup_scale.sample(rng)
    return inp.depset.realise(rng, lambda_scale=lam, backup_scale=bsc)


def _run_chunk(args: tuple[CampaignInput, int, int]) -> ChunkResult:
    inp, s, o = args
    net, cfg, scen = inp.net, inp.cfg, inp.scenarios[s]
    model = TopologicalTemporalCascade()
    deps = realise_dependencies(inp, o)
    p_node = inp.library.p_fail(net.cls, scen.node_im)
    p_edge = inp.library.p_fail(net.edge_cls, scen.edge_im)
    res = ChunkResult(s=s, o=o, node_down=np.zeros(net.n), node_downtime=np.zeros(net.n))
    for sector in net.sectors:
        res.cell_disrupt[sector] = np.zeros(len(net.cell_id))
        res.cell_outage[sector] = np.zeros(len(net.cell_id))
    thr = cfg.disruption_threshold_h
    for i in range(cfg.monte_carlo.inner):
        rng = inner_rng(cfg.monte_carlo.seed, s, o, i)
        dmg = sample_damage(rng, net, scen.node_im, scen.edge_im, inp.library, p_node, p_edge)
        trace = model.run(net, deps, dmg, cfg.cascade.horizon_h)
        total, direct = trace.downtime(net.n)
        res.node_down += trace.ever_down(net.n)
        res.node_downtime += total
        for sector in net.sectors:
            out_t = net.supply[sector] @ total
            out_d = net.supply[sector] @ direct
            res.cell_disrupt[sector] += out_t > thr
            res.cell_outage[sector] += out_t
            res.losses.append((s, o, i, sector, float(net.cell_pop @ out_t), float(net.cell_pop @ out_d)))
        if o == 0 and i < cfg.outputs.keep_traces:
            dn, de = np.flatnonzero(dmg.node_repair_t > 0), np.flatnonzero(dmg.edge_repair_t > 0)
            damage = pd.DataFrame({
                "rid": np.concatenate([net.rid[dn], net.edge_rid[de]]),
                "kind": ["node"] * len(dn) + ["edge"] * len(de),
                "repair_t_h": np.round(np.concatenate([dmg.node_repair_t[dn], dmg.edge_repair_t[de]]), 4),
                "im_value": np.concatenate([scen.node_im[dn], scen.edge_im[de]]),
            })
            res.traces.append((i, trace.to_frame(net), damage))
    return res


def run_campaign(inp: CampaignInput) -> dict[str, pd.DataFrame]:
    cfg = inp.cfg
    tasks = [(inp, s, o) for s in range(len(inp.scenarios)) for o in range(cfg.monte_carlo.outer)]
    if cfg.monte_carlo.workers > 1 and len(tasks) > 1:
        with ProcessPoolExecutor(max_workers=cfg.monte_carlo.workers) as ex:
            chunks = list(ex.map(_run_chunk, tasks, chunksize=max(1, len(tasks) // (4 * cfg.monte_carlo.workers))))
    else:
        chunks = [_run_chunk(t) for t in tasks]
    return summarise(inp, chunks)


def summarise(inp: CampaignInput, chunks: list[ChunkResult]) -> dict[str, pd.DataFrame]:
    net, cfg = inp.net, inp.cfg
    n_samples = cfg.monte_carlo.outer * cfg.monte_carlo.inner
    losses = pd.DataFrame(
        [row for c in chunks for row in c.losses],
        columns=["s", "o", "i", "sector", "loss_total", "loss_direct"],
    )
    losses["scenario_id"] = [inp.scenarios[s].scenario_id for s in losses["s"]]

    node_rows, cell_rows, trace_rows, damage_rows = [], [], [], []
    for s, scen in enumerate(inp.scenarios):
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

    metrics = compute_metrics(inp, losses)
    sample_losses = losses.drop(columns=["s"])[["scenario_id", "o", "i", "sector", "loss_total", "loss_direct"]]
    return {
        "sample_losses": sample_losses,
        "node_stats": pd.concat(node_rows, ignore_index=True),
        "cell_stats": pd.concat(cell_rows, ignore_index=True),
        "metrics": metrics,
        "traces": pd.concat(trace_rows, ignore_index=True) if trace_rows else pd.DataFrame(),
        "trace_damage": pd.concat(damage_rows, ignore_index=True) if damage_rows else pd.DataFrame(),
    }


def compute_metrics(inp: CampaignInput, losses: pd.DataFrame) -> pd.DataFrame:
    cfg = inp.cfg
    rows: list[dict] = []
    unit = "people-hours"
    allsec = losses.groupby(["s", "o", "i"], as_index=False)[["loss_total", "loss_direct"]].sum().assign(sector="all")
    frame = pd.concat([losses[["s", "o", "i", "sector", "loss_total", "loss_direct"]], allsec], ignore_index=True)
    rp = np.array([sc.return_period_y for sc in inp.scenarios])

    for sector, g in frame.groupby("sector", sort=True):
        per_outer_means = np.zeros((cfg.monte_carlo.outer, len(inp.scenarios)))
        for s, scen in enumerate(inp.scenarios):
            gs = g[g["s"] == s]
            x = gs["loss_total"].to_numpy()
            d = gs["loss_direct"].to_numpy()
            by_o = gs.groupby("o")["loss_total"].mean().reindex(range(cfg.monte_carlo.outer)).to_numpy()
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
        eal_o = np.array([eal_from_return_periods(rp, per_outer_means[o]) for o in range(cfg.monte_carlo.outer)])
        p05, p50, p95 = np.percentile(eal_o, [5, 50, 95])
        rows.append({"metric": "eal", "scenario_id": "annual", "sector": sector, "value": float(eal_o.mean()),
                     "p05": p05, "p50": p50, "p95": p95, "unit": f"{unit}/year"})
    out = pd.DataFrame(rows)
    num = ["value", "p05", "p50", "p95"]
    out[num] = out[num].astype(float).round(6)
    return out.sort_values(["metric", "scenario_id", "sector"]).reset_index(drop=True)
