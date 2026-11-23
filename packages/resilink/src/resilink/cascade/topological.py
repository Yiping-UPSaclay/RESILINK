"""Topological-temporal cascade (blueprint §6.3).

Semantics at every event time t:
1. physical state: an asset is physically ok if not damaged or already repaired;
2. intra-layer service: an ok node is served if connected (through ok nodes and ok edges) to an ok source;
3. inter-layer dependency: a dependent fails once its realised supporter has been non-functional for longer
   than the sampled backup autonomy;
4. steps 2-3 are iterated to a fixed point (monotone, so it converges in <= N iterations).

Event-driven: the state is recomputed only at t=0, repair completions and backup expiries, which keeps the
per-sample cost low. Nodes that are non-functional in the undamaged baseline are excluded from accounting.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from resilink.dependencies.model import DependencyRealisation
from resilink.domain.vocab import CAUSE_DIRECT, CAUSE_INTER, CAUSE_INTRA, CAUSE_LABEL
from resilink.fragility.lognormal import Damage
from resilink.network.compiled import CompiledNetwork, served

_EPS = 1e-9


@dataclass(frozen=True)
class CascadeTrace:
    t: np.ndarray  # event times (h)
    node: np.ndarray  # node index
    kind: np.ndarray  # +1 fail, -1 restore
    cause: np.ndarray  # cause code for fail events (0 for restore)
    culprit: np.ndarray  # supporter index for inter-layer failures, else -1
    baseline_ok: np.ndarray
    horizon_h: float

    def downtime(self, n: int) -> tuple[np.ndarray, np.ndarray]:
        """Total and direct-cause down hours per node within [0, horizon]."""
        total = np.zeros(n)
        direct = np.zeros(n)
        if len(self.t) == 0:
            return total, direct
        order = np.lexsort((self.t, self.node))
        t, node, kind, cause = self.t[order], self.node[order], self.kind[order], self.cause[order]
        fails = np.flatnonzero(kind == 1)
        nxt = fails + 1
        has_next = (nxt < len(t))
        end = np.full(len(fails), self.horizon_h)
        valid_next = has_next.copy()
        valid_next[has_next] = (node[nxt[has_next]] == node[fails[has_next]]) & (kind[nxt[has_next]] == -1)
        end[valid_next] = t[nxt[valid_next]]
        dur = np.clip(end - t[fails], 0.0, None)
        np.add.at(total, node[fails], dur)
        np.add.at(direct, node[fails], np.where(cause[fails] == CAUSE_DIRECT, dur, 0.0))
        return total, direct

    def ever_down(self, n: int) -> np.ndarray:
        out = np.zeros(n, dtype=bool)
        out[self.node[self.kind == 1]] = True
        return out

    def to_frame(self, net: CompiledNetwork) -> pd.DataFrame:
        return pd.DataFrame({
            "t_h": np.round(self.t, 4),
            "rid": net.rid[self.node],
            "event": np.where(self.kind == 1, "fail", "restore"),
            "cause": [CAUSE_LABEL.get(int(c), "") for c in self.cause],
            "culprit_rid": [net.rid[c] if c >= 0 else "" for c in self.culprit],
        })


class TopologicalTemporalCascade:
    model_id = "topological_temporal"
    version = "1.0.0"

    def _fixed_point(
        self, net: CompiledNetwork, t: float, phys: np.ndarray, eok: np.ndarray, deps: DependencyRealisation,
        func_prev: np.ndarray, down_since: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        f = phys.copy()
        violated = np.zeros(len(deps), dtype=bool)
        while True:
            srv = served(net, f, eok)
            dep_bad = np.zeros(net.n, dtype=bool)
            if len(deps):
                sup_down = ~f[deps.dep_j]
                ds = np.where(func_prev[deps.dep_j] | np.isnan(down_since[deps.dep_j]), t, down_since[deps.dep_j])
                violated = sup_down & ((t - ds) >= deps.backup_h - _EPS)
                dep_bad[deps.dep_i[violated]] = True
            f_new = phys & srv & ~dep_bad
            if np.array_equal(f_new, f):
                return f, dep_bad, violated
            f = f_new

    def run(self, net: CompiledNetwork, deps: DependencyRealisation, damage: Damage, horizon_h: float) -> CascadeTrace:
        n = net.n
        all_ok = np.ones(n, dtype=bool)
        nan = np.full(n, np.nan)
        base, _, _ = self._fixed_point(net, 0.0, all_ok, np.ones(net.e, dtype=bool), deps, all_ok, nan)

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
            phys = ~(damage.node_repair_t > t + _EPS)
            eok = ~(damage.edge_repair_t > t + _EPS)
            f, dep_bad, violated = self._fixed_point(net, t, phys, eok, deps, func, down_since)
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

            cands = [damage.node_repair_t[damage.node_repair_t > t + _EPS],
                     damage.edge_repair_t[damage.edge_repair_t > t + _EPS]]
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
