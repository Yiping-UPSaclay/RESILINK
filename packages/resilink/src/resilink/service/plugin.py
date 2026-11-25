"""Plugin registration of the built-in service model ``connectivity``."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from resilink.plugin_api import Applicability, ModelCard, ModelPlugin, SetupContext
from resilink.service.connectivity import ConnectivityService


class ConnectivityParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topology_mode: Literal["meshed_optimistic"] = "meshed_optimistic"  # A-TOPO-01; radial modes come with OPX data


def _factory(params: ConnectivityParams, setup: SetupContext) -> ConnectivityService:
    return ConnectivityService(setup.net)


connectivity = ModelPlugin(
    card=ModelCard(
        id="connectivity", version="1.0.0", slot="service",
        summary="A node is served when it works and is connected, through working assets, to a working source of "
                "its own system (all-or-nothing service, meshed-optimistic topology). The default service model; "
                "flow models such as DC-OPF replace it for a subsystem when line data exists.",
        authors=("RESILINK core team",), licence="Apache-2.0", maturity="experimental",
        assumptions=("A-TOPO-01",), applies_to=Applicability(time_scale="both")),
    params=ConnectivityParams, factory=_factory,
)
