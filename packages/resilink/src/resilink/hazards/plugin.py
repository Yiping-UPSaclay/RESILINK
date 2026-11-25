"""Plugin registration of the built-in hazard model ``flood_footprints``."""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from resilink.hazards.exposure import compute_exposure, exposure_arrays
from resilink.plugin_api import STANDARD, Applicability, ModelCard, ModelPlugin, ScenarioInfo, SetupContext


class FloodFootprintsParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenarios: list[str] | None = None  # None = every flood scenario of the release


class FloodFootprints:
    """Flood scenarios from the release's hazard footprints; intensity = maximum depth at each asset."""

    def __init__(self, params: FloodFootprintsParams, setup: SetupContext) -> None:
        table = setup.release.read("hazard_scenarios").sort_values("return_period_y")
        if params.scenarios:
            table = table[table["scenario_id"].isin(params.scenarios)]
        self._exposure = compute_exposure(setup.release, table["scenario_id"].tolist())
        self._scenarios = [ScenarioInfo(r.scenario_id, float(r.return_period_y)) for r in table.itertuples()]
        self._intensity = {s.scenario_id: exposure_arrays(self._exposure, setup.net, s.scenario_id)
                           for s in self._scenarios}

    def scenarios(self) -> list[ScenarioInfo]:
        return list(self._scenarios)

    def intensity(self, scenario: ScenarioInfo) -> tuple[np.ndarray, np.ndarray]:
        return self._intensity[scenario.scenario_id]

    def exposure_table(self) -> pd.DataFrame:
        return self._exposure


flood_footprints = ModelPlugin(
    card=ModelCard(
        id="flood_footprints", version="1.0.0", slot="hazard",
        summary="Flood scenarios taken from the hazard footprints stored in the release (polygons with a maximum "
                "depth and a return period); each asset gets the deepest water it touches. The default hazard model.",
        authors=("RESILINK core team",), licence="Apache-2.0", maturity="experimental",
        applies_to=Applicability(im_types=("depth_m",), time_scale="episode"),
        writes=(STANDARD["hazard.intensity"],)),
    params=FloodFootprintsParams, factory=FloodFootprints,
)
