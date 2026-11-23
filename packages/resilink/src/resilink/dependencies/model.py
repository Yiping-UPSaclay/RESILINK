"""Proximity-softmax dependency model (``proximity_softmax@1.0.0``).

Rules (``knowledge/dependency_rules.yaml``, human-approved, evidence-linked) say *which* classes depend on
*which* supporting classes and through which mechanism. This model turns rules into uncertain edges:

* ``exclusive`` groups: each dependent has exactly one supplier among its k nearest compatible candidates,
  drawn with ``p ∝ exp(-d / λ)`` (assumption A-DEP-03);
* ``independent`` rules: each candidate edge exists with probability ``p_exist`` (e.g. compressor drive type).

Backup autonomy (hours) is drawn per realised edge. Realisations are sampled in the *outer* (epistemic)
Monte Carlo loop, so the dependency structure itself is a random variable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field
from scipy.spatial import cKDTree

from resilink.domain.vocab import LAYER_CODE
from resilink.io.paths import load_yaml
from resilink.network.compiled import CompiledNetwork

MODEL_ID = "proximity_softmax"
MODEL_VERSION = "1.0.0"


class BackupSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    family: Literal["fixed", "gamma"] = "fixed"
    value: float | None = None
    mean: float | None = None
    cv: float | None = None


class DependencyRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    dependent: str  # "<layer>.<class>"
    supporter: str
    mechanism: Literal["power_supply", "fuel", "scada", "backhaul", "service"]
    dep_type: Literal["physical", "cyber", "geographic", "logical"] = "physical"
    group_mode: Literal["exclusive", "independent"] = "exclusive"
    k: int = Field(1, ge=1, le=20)
    lambda_m: float = Field(300.0, gt=0)
    max_distance_m: float = Field(5000.0, gt=0)
    p_exist: float = Field(1.0, ge=0.0, le=1.0)
    backup_h: BackupSpec = Field(default_factory=BackupSpec)
    assumption_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    status: str = "placeholder"
    rationale: str = ""


class DependencyRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    rules: list[DependencyRule]


def load_rules(path: str = "knowledge/dependency_rules.yaml") -> DependencyRules:
    return DependencyRules.model_validate(load_yaml(path))


@dataclass(frozen=True)
class DependencyRealisation:
    dep_i: np.ndarray  # dependent node index
    dep_j: np.ndarray  # supporter node index
    backup_h: np.ndarray
    rule_idx: np.ndarray

    @classmethod
    def empty(cls) -> DependencyRealisation:
        z = np.zeros(0, dtype=np.int64)
        return cls(z, z, np.zeros(0), z)

    def __len__(self) -> int:
        return len(self.dep_i)


@dataclass(frozen=True)
class DependencySet:
    """Columnar candidate edges, contiguous by group (one group per (rule, dependent))."""

    rules: DependencyRules
    dependent: np.ndarray
    supporter: np.ndarray
    distance: np.ndarray
    group: np.ndarray
    rule_idx: np.ndarray

    def __len__(self) -> int:
        return len(self.dependent)

    def _rule_arr(self, attr: str) -> np.ndarray:
        values = np.array([getattr(r, attr) for r in self.rules.rules], dtype=float)
        return values[self.rule_idx] if len(self) else np.zeros(0)

    @property
    def exclusive(self) -> np.ndarray:
        flags = np.array([r.group_mode == "exclusive" for r in self.rules.rules], dtype=bool)
        return flags[self.rule_idx] if len(self) else np.zeros(0, dtype=bool)

    def probabilities(self, lambda_scale: float = 1.0) -> np.ndarray:
        """Exclusive: softmax(-d/λ) within group. Independent: p_exist."""
        if not len(self):
            return np.zeros(0)
        lam = self._rule_arr("lambda_m") * lambda_scale
        w = np.exp(-self.distance / lam)
        starts, sizes = self._groups()
        totals = np.add.reduceat(w, starts)
        p_excl = w / np.repeat(totals, sizes)
        return np.where(self.exclusive, p_excl, self._rule_arr("p_exist"))

    def _groups(self) -> tuple[np.ndarray, np.ndarray]:
        starts = np.flatnonzero(np.r_[True, self.group[1:] != self.group[:-1]])
        sizes = np.diff(np.r_[starts, len(self.group)])
        return starts, sizes

    def realise(self, rng: np.random.Generator, lambda_scale: float = 1.0, backup_scale: float = 1.0) -> DependencyRealisation:
        if not len(self):
            return DependencyRealisation.empty()
        p = self.probabilities(lambda_scale)
        excl = self.exclusive
        starts, sizes = self._groups()
        group_of = np.repeat(np.arange(len(starts)), sizes)
        # exclusive groups: inverse-CDF categorical draw per group
        cs = np.cumsum(np.where(excl, p, 0.0))
        base = np.repeat(cs[starts] - np.where(excl, p, 0.0)[starts], sizes)
        within = cs - base
        u = np.repeat(rng.random(len(starts)), sizes)
        hit = excl & (within >= u - 1e-12)
        hit_idx = np.flatnonzero(hit)
        g_hit = group_of[hit_idx]
        first = hit_idx[np.r_[True, g_hit[1:] != g_hit[:-1]]] if len(hit_idx) else hit_idx
        chosen = np.zeros(len(self), dtype=bool)
        chosen[first] = True
        # independent candidates: Bernoulli(p_exist)
        bern = rng.random(len(self)) < p
        chosen |= (~excl) & bern
        sel = np.flatnonzero(chosen)
        backup = np.empty(len(sel))
        for n, c in enumerate(sel):
            spec = self.rules.rules[self.rule_idx[c]].backup_h
            if spec.family == "gamma" and spec.mean and spec.cv:
                shape = 1.0 / spec.cv**2
                backup[n] = rng.gamma(shape, spec.mean * backup_scale / shape)
            else:
                backup[n] = (spec.value or 0.0) * backup_scale
        return DependencyRealisation(self.dependent[sel], self.supporter[sel], backup, self.rule_idx[sel])

    def to_frame(self, net: CompiledNetwork, lambda_scale: float = 1.0) -> pd.DataFrame:
        p = self.probabilities(lambda_scale)
        rules = self.rules.rules
        return pd.DataFrame({
            "dependent_rid": net.rid[self.dependent],
            "supporter_rid": net.rid[self.supporter],
            "distance_m": np.round(self.distance, 1),
            "p": np.round(p, 4),
            "group_mode": ["exclusive" if e else "independent" for e in self.exclusive],
            "rule_id": [rules[i].id for i in self.rule_idx],
            "mechanism": [rules[i].mechanism for i in self.rule_idx],
            "dep_type": [rules[i].dep_type for i in self.rule_idx],
            "model": f"{MODEL_ID}@{MODEL_VERSION}",
        })


def _class_mask(net: CompiledNetwork, qualified: str) -> np.ndarray:
    layer, cls = qualified.split(".", 1)
    return (net.layer == LAYER_CODE[layer]) & (net.cls == cls)


def infer_proximity_softmax(net: CompiledNetwork, rules: DependencyRules) -> DependencySet:
    rows: list[tuple[int, int, float, int, int]] = []  # (dependent, supporter, distance, group, rule)
    g = 0
    for r_i, rule in enumerate(rules.rules):
        d_idx = np.flatnonzero(_class_mask(net, rule.dependent))
        s_idx = np.flatnonzero(_class_mask(net, rule.supporter))
        if len(d_idx) == 0 or len(s_idx) == 0:
            continue
        k = min(rule.k, len(s_idx))
        tree = cKDTree(net.xy[s_idx])
        dd, ii = tree.query(net.xy[d_idx], k=k, distance_upper_bound=rule.max_distance_m)
        dd = np.asarray(dd).reshape(len(d_idx), k)
        ii = np.asarray(ii).reshape(len(d_idx), k)
        for row, node in enumerate(d_idx):
            valid = np.isfinite(dd[row])
            if not valid.any():
                continue
            for dval, ival in zip(dd[row][valid], ii[row][valid], strict=True):
                rows.append((int(node), int(s_idx[ival]), float(dval), g, r_i))
                if rule.group_mode == "independent":
                    g += 1  # each independent candidate is its own group
            if rule.group_mode == "exclusive":
                g += 1
    arr = np.array(rows, dtype=float).reshape(-1, 5)
    return DependencySet(
        rules=rules,
        dependent=arr[:, 0].astype(np.int64),
        supporter=arr[:, 1].astype(np.int64),
        distance=arr[:, 2],
        group=arr[:, 3].astype(np.int64),
        rule_idx=arr[:, 4].astype(np.int64),
    )
