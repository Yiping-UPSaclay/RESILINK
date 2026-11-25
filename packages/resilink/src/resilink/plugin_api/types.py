"""Values that models exchange (ADR-0008 §6). Plugins never import each other; they share these types instead."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from resilink.plugin_api.contexts import NetworkView

CAUSE_DIRECT = 1  # physical damage from the hazard
CAUSE_INTRA = 2  # lost connectivity to a source within its own system
CAUSE_INTER = 3  # a supporting asset in another system was down longer than the backup autonomy
CAUSE_LABEL: dict[int, str] = {CAUSE_DIRECT: "direct", CAUSE_INTRA: "intra", CAUSE_INTER: "inter"}


@dataclass(frozen=True)
class ScenarioInfo:
    scenario_id: str
    return_period_y: float


@dataclass(frozen=True)
class DependencyRealisation:
    """One drawn dependency structure: dependent node i relies on supporter j, with a backup autonomy (h)."""

    dep_i: np.ndarray
    dep_j: np.ndarray
    backup_h: np.ndarray
    rule_idx: np.ndarray

    @classmethod
    def empty(cls) -> DependencyRealisation:
        z = np.zeros(0, dtype=np.int64)
        return cls(z, z, np.zeros(0), z)

    def __len__(self) -> int:
        return len(self.dep_i)


@dataclass(frozen=True)
class DamageState:
    """Damage state per element; 0 = undamaged (binary models use 0 and 1)."""

    node_ds: np.ndarray
    edge_ds: np.ndarray

    @property
    def node_failed(self) -> np.ndarray:
        return self.node_ds > 0

    @property
    def edge_failed(self) -> np.ndarray:
        return self.edge_ds > 0

    @classmethod
    def none(cls, n: int, e: int) -> DamageState:
        return cls(np.zeros(n, dtype=np.int8), np.zeros(e, dtype=np.int8))

    @classmethod
    def from_failed(cls, node_failed: np.ndarray, edge_failed: np.ndarray) -> DamageState:
        return cls(np.asarray(node_failed).astype(np.int8), np.asarray(edge_failed).astype(np.int8))


@dataclass(frozen=True)
class RepairPlan:
    """Hours from the event until each element is repaired; 0 = not damaged."""

    node_repair_h: np.ndarray
    edge_repair_h: np.ndarray

    @classmethod
    def none(cls, n: int, e: int) -> RepairPlan:
        return cls(np.zeros(n), np.zeros(e))


def outage(node_repair_h: np.ndarray, edge_repair_h: np.ndarray) -> tuple[DamageState, RepairPlan]:
    """Damage and repair plan from 'down until' times (0 = undamaged); handy for tests and simple models."""
    node_t = np.asarray(node_repair_h, dtype=float)
    edge_t = np.asarray(edge_repair_h, dtype=float)
    return DamageState.from_failed(node_t > 0, edge_t > 0), RepairPlan(node_t, edge_t)


@dataclass(frozen=True)
class CascadeTrace:
    """Fail (+1) and restore (-1) events per node, with cause codes, over one episode."""

    t: np.ndarray  # event times (h)
    node: np.ndarray  # node index
    kind: np.ndarray  # +1 fail, -1 restore
    cause: np.ndarray  # cause code for fail events (0 for restore)
    culprit: np.ndarray  # supporter index for inter-system failures, else -1
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

    def to_frame(self, net: NetworkView) -> pd.DataFrame:
        return pd.DataFrame({
            "t_h": np.round(self.t, 4),
            "rid": net.rid[self.node],
            "event": np.where(self.kind == 1, "fail", "restore"),
            "cause": [CAUSE_LABEL.get(int(c), "") for c in self.cause],
            "culprit_rid": [net.rid[c] if c >= 0 else "" for c in self.culprit],
        })
