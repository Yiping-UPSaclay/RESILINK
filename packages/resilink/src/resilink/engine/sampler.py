"""Nested Monte Carlo sampler (built-in engine plugin ``nested_mc``).

Outer loop o (epistemic): dependency realisation and scaled priors, shared across scenarios ("same world").
Inner loop i (aleatory): damage and repair times. Seeds use ``SeedSequence(seed, spawn_key=...)`` so every world
is reproducible and independent of how the campaign is split across worker processes (unchanged from Sprint 0;
per-slot streams arrive with the event kernel, step E4). Every world has weight 1.
"""

from __future__ import annotations

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from resilink.plugin_api import Applicability, ModelCard, ModelPlugin, SetupContext


class NestedMCParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outer: int = Field(10, ge=1)
    inner: int = Field(50, ge=1)
    seed: int = 20270115


class NestedMC:
    def __init__(self, params: NestedMCParams, setup: SetupContext | None = None) -> None:
        self.outer = params.outer
        self.inner = params.inner
        self.seed = params.seed

    def outer_rng(self, o: int) -> np.random.Generator:
        return np.random.default_rng(np.random.SeedSequence(self.seed, spawn_key=(0, o)))

    def inner_rng(self, s: int, o: int, i: int) -> np.random.Generator:
        return np.random.default_rng(np.random.SeedSequence(self.seed, spawn_key=(1, s, o, i)))


nested_mc = ModelPlugin(
    card=ModelCard(
        id="nested_mc", version="0.1.0", slot="sampler",
        summary="Nested Monte Carlo: an outer loop over uncertain model structure and parameters, an inner loop "
                "over damage and repair randomness, with seeds that do not depend on the number of workers. The "
                "default sampler.",
        authors=("RESILINK core team",), licence="Apache-2.0", maturity="experimental",
        applies_to=Applicability(time_scale="both")),
    params=NestedMCParams, factory=NestedMC,
)
