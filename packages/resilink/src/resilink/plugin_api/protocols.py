"""What a model for each slot must implement (plugin API 0.1, blueprint §6.4).

Version 0.1 matches the episode orchestrator 0.1 (the Sprint 0 loop, fixed sequence). The event-driven kernel
(step E4) will move these contracts to event handlers; the plugin API is a 0.x draft until then.
"""

from __future__ import annotations

from typing import Any, Protocol

import numpy as np
import pandas as pd

from resilink.plugin_api.types import (
    CascadeTrace,
    DamageState,
    DependencyRealisation,
    RepairPlan,
    ScenarioInfo,
)


class HazardModel(Protocol):
    def scenarios(self) -> list[ScenarioInfo]: ...

    def intensity(self, scenario: ScenarioInfo) -> tuple[np.ndarray, np.ndarray]: ...  # node_im, edge_im

    def exposure_table(self) -> pd.DataFrame: ...  # asset x scenario intensities (results table "exposure")


class FragilityModel(Protocol):
    def failure_probabilities(self, node_im: np.ndarray, edge_im: np.ndarray) -> tuple[np.ndarray, np.ndarray]: ...

    def damage(self, rng: np.random.Generator, p_node: np.ndarray, p_edge: np.ndarray) -> DamageState: ...


class DependencyModel(Protocol):
    def realise(self, rng: np.random.Generator) -> DependencyRealisation: ...  # once per outer (epistemic) draw

    def table(self) -> pd.DataFrame | None: ...  # candidate dependencies (results table "dependencies")


class ServiceModel(Protocol):
    def served(self, node_ok: np.ndarray, edge_ok: np.ndarray) -> np.ndarray: ...  # within-system service


class PropagationModel(Protocol):
    def run(self, deps: DependencyRealisation, damage: DamageState, plan: RepairPlan,
            horizon_h: float) -> CascadeTrace: ...


class RestorationModel(Protocol):
    def plan(self, rng: np.random.Generator, damage: DamageState) -> RepairPlan: ...


class ConsequenceModel(Protocol):
    def evaluate(self, total_downtime_h: np.ndarray, direct_downtime_h: np.ndarray) -> dict[str, Any]: ...


class Sampler(Protocol):
    def outer_rng(self, o: int) -> np.random.Generator: ...

    def inner_rng(self, s: int, o: int, i: int) -> np.random.Generator: ...


class Orchestrator(Protocol):
    def run(self, models: Any, *, workers: int = 1, keep_traces: int = 5) -> dict[str, pd.DataFrame]: ...
