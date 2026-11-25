"""Plugin registration of the built-in consequence model ``people_hours``."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from resilink.consequence.people_hours import PeopleHours
from resilink.plugin_api import Applicability, ModelCard, ModelPlugin, SetupContext


class PeopleHoursParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    disruption_threshold_h: float = Field(4.0, gt=0, description="Outage length that counts as a disruption")


def _factory(params: PeopleHoursParams, setup: SetupContext) -> PeopleHours:
    return PeopleHours(setup.net, params.disruption_threshold_h)


people_hours = ModelPlugin(
    card=ModelCard(
        id="people_hours", version="1.0.0", slot="consequence",
        summary="Loss = population x hours without service, per system serving the population grid, with cells "
                "flagged as disrupted beyond a threshold. The default consequence model; economic or critical-user "
                "models can be added alongside it.",
        authors=("RESILINK core team",), licence="Apache-2.0", maturity="experimental",
        assumptions=("A-LOSS-01",), applies_to=Applicability(time_scale="both")),
    params=PeopleHoursParams, factory=_factory,
)
