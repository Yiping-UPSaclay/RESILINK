"""Plugin registration of the built-in restoration model ``independent_repair``."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from resilink.plugin_api import STANDARD, Applicability, ModelCard, ModelPlugin, SetupContext
from resilink.restoration.independent import IndependentRepair


class IndependentRepairParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sigma: float = Field(0.5, gt=0, description="Lognormal spread of repair times (A-REP-01)")


def _factory(params: IndependentRepairParams, setup: SetupContext) -> IndependentRepair:
    return IndependentRepair(setup.net, params.sigma)


independent_repair = ModelPlugin(
    card=ModelCard(
        id="independent_repair", version="1.0.0", slot="restoration",
        summary="Every damaged element is repaired after a lognormal time around its class median from the system "
                "packs, independently of the others (no shared crews or spares). The default restoration model.",
        authors=("RESILINK core team",), licence="Apache-2.0", maturity="experimental",
        assumptions=("A-REP-01",), applies_to=Applicability(time_scale="episode"),
        writes=(STANDARD["repair.finish_h"],), streams=("repair",)),
    params=IndependentRepairParams, factory=_factory,
)
