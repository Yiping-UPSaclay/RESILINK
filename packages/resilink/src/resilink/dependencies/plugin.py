"""Plugin registration of the built-in dependency models ``proximity_softmax`` and ``no_dependencies``."""

from __future__ import annotations

from typing import Annotated

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from resilink.dependencies.model import MODEL_VERSION, infer_proximity_softmax, load_rules
from resilink.plugin_api import (
    Applicability,
    DependencyRealisation,
    FileRef,
    ModelCard,
    ModelPlugin,
    PriorSpec,
    SetupContext,
)


class ProximitySoftmaxParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: Annotated[str, FileRef()] = "knowledge/dependency_rules.yaml"
    lambda_scale: PriorSpec = Field(default_factory=lambda: PriorSpec(prior="fixed", value=1.0))  # A-DEP-03
    backup_scale: PriorSpec = Field(default_factory=lambda: PriorSpec(prior="fixed", value=1.0))


class ProximitySoftmax:
    def __init__(self, params: ProximitySoftmaxParams, setup: SetupContext) -> None:
        self.params = params
        self.net = setup.net
        rules = load_rules(setup.resolve(params.rules))
        self.depset = infer_proximity_softmax(setup.net, rules, known_systems=setup.systems)

    def realise(self, rng: np.random.Generator) -> DependencyRealisation:
        lam = self.params.lambda_scale.sample(rng)
        bsc = self.params.backup_scale.sample(rng)
        return self.depset.realise(rng, lambda_scale=lam, backup_scale=bsc)

    def table(self) -> pd.DataFrame:
        return self.depset.to_frame(self.net, self.params.lambda_scale.central())


class NoDependencies:
    """The no-interdependency baseline: every system runs on its own (structural sensitivity)."""

    def realise(self, rng: np.random.Generator) -> DependencyRealisation:
        return DependencyRealisation.empty()

    def table(self) -> None:
        return None


class _NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _no_dependencies(params: _NoParams, setup: SetupContext) -> NoDependencies:
    return NoDependencies()


proximity_softmax = ModelPlugin(
    card=ModelCard(
        id="proximity_softmax", version=MODEL_VERSION, slot="dependency",
        summary="Infers which asset of another system each asset relies on, from approved dependency rules and "
                "distance: exclusive suppliers drawn with p ~ exp(-d/lambda), optional links with p_exist, and a "
                "backup autonomy per link. The default dependency model for open data, where links are not published.",
        authors=("RESILINK core team",), licence="Apache-2.0", maturity="experimental",
        assumptions=("A-DEP-03", "A-TEL-01", "A-GAS-01"), applies_to=Applicability(time_scale="episode"),
        streams=("dependencies",)),
    params=ProximitySoftmaxParams, factory=ProximitySoftmax,
)

no_dependencies = ModelPlugin(
    card=ModelCard(
        id="no_dependencies", version="1.0.0", slot="dependency",
        summary="No dependencies between systems. Use it as the baseline that shows how much interdependency "
                "amplifies losses.",
        authors=("RESILINK core team",), licence="Apache-2.0", maturity="experimental",
        applies_to=Applicability(time_scale="both")),
    params=_NoParams, factory=_no_dependencies,
)
