"""Lognormal fragility: P(fail | IM) = Phi(ln(IM / median) / beta). Repair times ~ lognormal(median_h, sigma)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field
from scipy.special import ndtr

from resilink.io.paths import load_yaml
from resilink.network.compiled import CompiledNetwork


class FragilityCurve(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_class: str
    hazard: str = "flood"
    im_type: str = "depth_m"
    median: float = Field(gt=0)
    beta: float = Field(gt=0)
    assumption_id: str
    grade: Literal["A", "B", "C"] = "C"
    evidence_ids: list[str] = Field(default_factory=list)


class FragilityLibrary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    repair_sigma: float = 0.5
    curves: list[FragilityCurve]

    def arrays(self, classes: np.ndarray, hazard: str = "flood") -> tuple[np.ndarray, np.ndarray]:
        lookup = {c.asset_class: (c.median, c.beta) for c in self.curves if c.hazard == hazard}
        med = np.array([lookup.get(c, (np.inf, 1.0))[0] for c in classes], dtype=float)
        beta = np.array([lookup.get(c, (np.inf, 1.0))[1] for c in classes], dtype=float)
        return med, beta

    def p_fail(self, classes: np.ndarray, im: np.ndarray, hazard: str = "flood") -> np.ndarray:
        med, beta = self.arrays(classes, hazard)
        with np.errstate(divide="ignore", invalid="ignore"):
            z = np.log(np.where(im > 0, im, 1.0) / med) / beta
        p = np.where((im > 0) & np.isfinite(med), ndtr(z), 0.0)
        return p


def load_library(path: str = "configs/fragility/v1.yaml") -> FragilityLibrary:
    return FragilityLibrary.model_validate(load_yaml(path))


@dataclass(frozen=True)
class Damage:
    node_repair_t: np.ndarray  # hours until repaired (0 = undamaged)
    edge_repair_t: np.ndarray

    @property
    def node_failed(self) -> np.ndarray:
        return self.node_repair_t > 0

    @property
    def edge_failed(self) -> np.ndarray:
        return self.edge_repair_t > 0

    @classmethod
    def none(cls, net: CompiledNetwork) -> Damage:
        return cls(np.zeros(net.n), np.zeros(net.e))


def sample_damage(
    rng: np.random.Generator,
    net: CompiledNetwork,
    node_im: np.ndarray,
    edge_im: np.ndarray,
    library: FragilityLibrary,
    p_node: np.ndarray | None = None,
    p_edge: np.ndarray | None = None,
) -> Damage:
    if p_node is None:
        p_node = library.p_fail(net.cls, node_im)
    if p_edge is None:
        p_edge = library.p_fail(net.edge_cls, edge_im)
    u_n = rng.random(net.n)
    u_e = rng.random(net.e)
    rep_n = rng.lognormal(np.log(net.repair_median_h), library.repair_sigma)
    rep_e = rng.lognormal(np.log(net.edge_repair_median_h), library.repair_sigma)
    return Damage(np.where(u_n < p_node, rep_n, 0.0), np.where(u_e < p_edge, rep_e, 0.0))
