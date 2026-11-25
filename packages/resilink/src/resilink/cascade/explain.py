"""Deterministic causal explanation of a node failure from a stored trace (feeds the Analyst agent; I1).

The chain is computed, never generated: ``asset <- cause <- ... <- hazard damage``. For intra-layer losses we
walk the shortest path towards a source in the undamaged layer graph and report the first element that was
down for a non-intra reason (direct damage, a damaged edge, or an inter-layer dependency).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.sparse import csgraph

from resilink.plugin_api import NetworkView


@dataclass(frozen=True)
class CausalStep:
    rid: str
    cause: str  # direct | intra | inter | edge_damage | functional | unknown
    t_h: float | None
    detail: str
    culprit_rid: str | None = None


def _down_interval_at(events: pd.DataFrame, rid: str, t: float) -> pd.Series | None:
    ev = events[(events["rid"] == rid) & (events["t_h"] <= t + 1e-9)].sort_values("t_h")
    if ev.empty or ev.iloc[-1]["event"] != "fail":
        return None
    return ev.iloc[-1]


def explain(
    net: NetworkView,
    events: pd.DataFrame,
    damage: pd.DataFrame,
    rid: str,
    t: float | None = None,
    max_depth: int = 8,
) -> list[CausalStep]:
    """``events``: trace frame (t_h, rid, event, cause, culprit_rid); ``damage``: (rid, kind, repair_t_h, im_value)."""
    steps: list[CausalStep] = []
    dmg = damage.set_index("rid") if len(damage) else pd.DataFrame(columns=["kind", "repair_t_h", "im_value"])
    adjacency = net.full_adjacency()
    visited: set[str] = set()
    current, t_ref = rid, t

    for _ in range(max_depth):
        if current in visited:
            steps.append(CausalStep(current, "unknown", t_ref, "cycle in dependency chain"))
            break
        visited.add(current)
        fails = events[(events["rid"] == current) & (events["event"] == "fail")].sort_values("t_h")
        if t_ref is not None:
            row = _down_interval_at(events, current, t_ref)
        else:
            row = fails.iloc[0] if len(fails) else None
        if row is None:
            steps.append(CausalStep(current, "functional", t_ref, "no failure recorded in this sample"))
            break
        t_fail = float(row["t_h"])
        cause = str(row["cause"])
        if cause == "direct":
            im = dmg.loc[current, "im_value"] if current in dmg.index else float("nan")
            rep = dmg.loc[current, "repair_t_h"] if current in dmg.index else float("nan")
            steps.append(CausalStep(current, "direct", t_fail,
                                    f"damaged by the hazard (intensity {im:g}); repair after {rep:.1f} h"))
            break
        if cause == "inter":
            sup = str(row["culprit_rid"])
            steps.append(CausalStep(current, "inter", t_fail,
                                    "supporting asset down longer than backup autonomy", culprit_rid=sup))
            current, t_ref = sup, t_fail
            continue
        # intra: walk towards the nearest source in the undamaged graph
        i = net.idx(current)
        order, pred = csgraph.breadth_first_order(adjacency, i, directed=False, return_predecessors=True)
        src = [int(o) for o in order if net.is_source[o] and net.layer[o] == net.layer[i]]
        if not src:
            steps.append(CausalStep(current, "intra", t_fail, "no source reachable in the undamaged network"))
            break
        path = [src[0]]
        while path[-1] != i:
            path.append(int(pred[path[-1]]))
        path = path[::-1]  # node -> ... -> source
        culprit = None
        for a, b in itertools.pairwise(path):
            for e in np.flatnonzero(((net.edge_src == a) & (net.edge_dst == b)) | ((net.edge_src == b) & (net.edge_dst == a))):
                er = net.edge_rid[e]
                if er in dmg.index and float(dmg.loc[er, "repair_t_h"]) > t_fail:
                    culprit = ("edge", er)
                    break
            if culprit:
                break
            nb = net.rid[b]
            r = _down_interval_at(events, nb, t_fail)
            if r is not None and r["cause"] != "intra":
                culprit = ("node", nb)
                break
        if culprit is None:
            steps.append(CausalStep(current, "intra", t_fail, "lost connectivity to its sources (meshed alternatives down)"))
            break
        kind, crid = culprit
        steps.append(CausalStep(current, "intra", t_fail, f"lost supply path; upstream {kind} {crid} down", culprit_rid=crid))
        if kind == "edge":
            im = dmg.loc[crid, "im_value"]
            steps.append(CausalStep(crid, "edge_damage", t_fail, f"line damaged by the hazard (intensity {im:g})"))
            break
        current, t_ref = crid, t_fail
    return steps
