"""Lognormal fragility: P(fail | IM) = Phi(ln(IM / median) / beta) (built-in model ``lognormal_library``).

Plugin module: imports only ``resilink.plugin_api`` from the core (ADR-0008, import contract).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import numpy as np
import yaml
from pydantic import BaseModel, ConfigDict, Field
from scipy.special import ndtr

from resilink.plugin_api import DamageState, NetworkView


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
    repair_sigma: float = 0.5  # read by the restoration model since E3 (parameter `sigma` of independent_repair)
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


def load_library(path: str | Path) -> FragilityLibrary:
    """Load a fragility library from an already resolved path (the engine resolves configured file references)."""
    return FragilityLibrary.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


class LognormalFragility:
    """Binary damage: an element fails when a uniform draw falls below its failure probability."""

    def __init__(self, library: FragilityLibrary, net: NetworkView) -> None:
        self.library = library
        self.net = net

    def failure_probabilities(self, node_im: np.ndarray, edge_im: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return self.library.p_fail(self.net.cls, node_im), self.library.p_fail(self.net.edge_cls, edge_im)

    def damage(self, rng: np.random.Generator, p_node: np.ndarray, p_edge: np.ndarray) -> DamageState:
        u_n = rng.random(self.net.n)
        u_e = rng.random(self.net.e)
        return DamageState.from_failed(u_n < p_node, u_e < p_edge)
