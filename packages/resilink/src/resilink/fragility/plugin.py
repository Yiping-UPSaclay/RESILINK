"""Plugin registration of the built-in fragility model ``lognormal_library`` (entry point resilink.models)."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict

from resilink.fragility.lognormal import LognormalFragility, load_library
from resilink.plugin_api import STANDARD, Applicability, FileRef, ModelCard, ModelPlugin, SetupContext


class LognormalLibraryParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    library: Annotated[str, FileRef()] = "configs/fragility/v1.yaml"


def _factory(params: LognormalLibraryParams, setup: SetupContext) -> LognormalFragility:
    return LognormalFragility(load_library(setup.resolve(params.library)), setup.net)


lognormal_library = ModelPlugin(
    card=ModelCard(
        id="lognormal_library", version="1.0.0", slot="fragility",
        summary="Binary flood damage from a library of lognormal fragility curves per asset class "
                "(P(fail | depth) = Phi(ln(depth/median)/beta)). The default fragility model; use another "
                "library file to test alternative curves.",
        authors=("RESILINK core team",), licence="Apache-2.0", maturity="experimental",
        assumptions=("A-FRAG-01",),
        applies_to=Applicability(im_types=("depth_m",), time_scale="episode"),
        writes=(STANDARD["damage.state"],), streams=("damage",)),
    params=LognormalLibraryParams, factory=_factory,
)
