"""Topological-temporal cascade (built-in model ``topological_temporal``, blueprint §6.3).

Semantics at every event time t:
1. physical state: an asset is physically ok if not damaged or already repaired;
2. within-system service: decided by the configured service model (default ``connectivity``);
3. cross-system dependency: a dependent fails once its realised supporter has been non-functional for longer
   than the sampled backup autonomy;
4. steps 2-3 are iterated to a fixed point (monotone, so it converges in <= N iterations).

Event-driven: the state is recomputed only at t=0, repair completions and backup expiries, which keeps the
per-sample cost low. Nodes that are non-functional in the undamaged baseline are excluded from accounting.
Plugin module: imports only ``resilink.plugin_api`` from the core (ADR-0008, import contract).
"""

from __future__ import annotations

import numpy as np

from resilink.plugin_api import (
    CAUSE_DIRECT,
    CAUSE_INTER,
    CAUSE_INTRA,
    CascadeTrace,
    DamageState,
    DependencyRealisation,
    NetworkView,
    RepairPlan,
    ServiceModel,
)

_EPS = 1e-9

__all__ = ["CascadeTrace", "TopologicalTemporalCascade"]


class TopologicalTemporalCascade:
    model_id = "topological_temporal"
    version = "1.0.0"

    def __init__(self, net: NetworkView, service: ServiceModel) -> None:
        self.net = net
        self.service = service

    def _fixed_point(
        self, t: float, phys: np.ndarray, eok: np.ndarray, deps: DependencyRealisation,
        func_prev: np.ndarray, down_since: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        n = self.net.n
        f = phys.copy()
        violated = np.zeros(len(deps), dtype=bool)
        while True:
            srv = self.service.served(f, eok)
            dep_bad = np.zeros(n, dtype=bool)
            if len(deps):
                sup_down = ~f[deps.dep_j]
                ds = np.where(func_prev[deps.dep_j] | np.isnan(down_since[deps.dep_j]), t, down_since[deps.dep_j])
                violated = sup_down & ((t - ds) >= deps.backup_h - _EPS)
                dep_bad[deps.dep_i[violated]] = True
            f_new = phys & srv & ~dep_bad
            if np.array_equal(f_new, f):
                return f, dep_bad, violated
            f = f_new

    def run(self, deps: DependencyRealisation, damage: DamageState, plan: RepairPlan,
            horizon_h: float) -> CascadeTrace:
        net = self.net
        n = net.n
        node_repair_t, edge_repair_t = plan.node_repair_h, plan.edge_repair_h
        all_ok = np.ones(n, dtype=bool)
        nan = np.full(n, np.nan)
        base, _, _ = self._fixed_point(0.0, all_ok, np.ones(net.e, dtype=bool), deps, all_ok, nan)

        func = base.copy()
        down_since = np.full(n, np.nan)
        ev_t: list[np.ndarray] = []
        ev_n: list[np.ndarray] = []
        ev_k: list[np.ndarray] = []
        ev_c: list[np.ndarray] = []
        ev_u: list[np.ndarray] = []

        def record(t_now: float, nodes: np.ndarray, kind: int, cause: np.ndarray, culprit: np.ndarray) -> None:
            ev_t.append(np.full(len(nodes), t_now))
            ev_n.append(nodes)
            ev_k.append(np.full(len(nodes), kind, dtype=np.int8))
            ev_c.append(cause.astype(np.int8))
            ev_u.append(culprit)

        t = 0.0
        while True:
            phys = ~(node_repair_t > t + _EPS)
            eok = ~(edge_repair_t > t + _EPS)
            f, dep_bad, violated = self._fixed_point(t, phys, eok, deps, func, down_since)
            f &= base
            down = np.flatnonzero(func & ~f)
            up = np.flatnonzero(~func & f)
            if len(down):
                cause = np.where(~phys[down], CAUSE_DIRECT, np.where(dep_bad[down], CAUSE_INTER, CAUSE_INTRA))
                culprit = np.full(len(down), -1, dtype=np.int64)
                if len(deps) and violated.any():
                    vi, vj = deps.dep_i[violated], deps.dep_j[violated]
                    first_sup = dict(zip(vi[::-1].tolist(), vj[::-1].tolist(), strict=True))  # keeps first occurrence
                    inter = cause == CAUSE_INTER
                    culprit[inter] = [first_sup.get(int(d), -1) for d in down[inter]]
                record(t, down, 1, cause, culprit)
                down_since[down] = t
            if len(up):
                record(t, up, -1, np.zeros(len(up), np.int8), np.full(len(up), -1, dtype=np.int64))
                down_since[up] = np.nan
            func = f

            cands = [node_repair_t[node_repair_t > t + _EPS], edge_repair_t[edge_repair_t > t + _EPS]]
            if len(deps):
                sup_down = ~func[deps.dep_j] & base[deps.dep_j] & func[deps.dep_i]
                expiry = down_since[deps.dep_j] + deps.backup_h
                cands.append(expiry[sup_down & (expiry > t + _EPS)])
            nxt = np.concatenate(cands)
            if nxt.size == 0:
                break
            t_next = float(nxt.min())
            if t_next > horizon_h:
                break
            t = t_next

        def cat(xs: list[np.ndarray], dtype: type) -> np.ndarray:
            return np.concatenate(xs).astype(dtype) if xs else np.zeros(0, dtype=dtype)

        return CascadeTrace(
            t=cat(ev_t, float), node=cat(ev_n, np.int64), kind=cat(ev_k, np.int8), cause=cat(ev_c, np.int8),
            culprit=cat(ev_u, np.int64), baseline_ok=base, horizon_h=horizon_h,
        )
