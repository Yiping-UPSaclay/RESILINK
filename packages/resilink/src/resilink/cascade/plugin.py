"""Plugin registration of the built-in propagation model ``topological_temporal``."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from resilink.cascade.topological import TopologicalTemporalCascade
from resilink.plugin_api import STANDARD, Applicability, ModelCard, ModelPlugin, SetupContext


class TopologicalTemporalParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _factory(params: TopologicalTemporalParams, setup: SetupContext) -> TopologicalTemporalCascade:
    return TopologicalTemporalCascade(setup.net, setup.service_model())


topological_temporal = ModelPlugin(
    card=ModelCard(
        id="topological_temporal", version="1.0.0", slot="propagation",
        summary="Event-driven spread of outages within and across systems: service per system from the service "
                "model, cross-system failures once a supporter has been down longer than the backup autonomy, "
                "recovery as repairs complete. The default propagation model.",
        authors=("RESILINK core team",), licence="Apache-2.0", maturity="experimental",
        assumptions=("A-TEL-01",), applies_to=Applicability(time_scale="episode"),
        writes=(STANDARD["service.served"], STANDARD["outage.hours"])),
    params=TopologicalTemporalParams, factory=_factory,
)
