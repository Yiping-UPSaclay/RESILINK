"""Typed configuration models (territory, experiment). Pure pydantic — loading from disk lives in resilink.io."""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TerritoryConfig(_Strict):
    id: str
    name: str
    selector: dict[str, Any] = Field(default_factory=dict)
    buffer_m: float = 30_000.0
    crs_metric: str = "EPSG:2154"
    grid: dict[str, Any] = Field(default_factory=lambda: {"id": "insee200m", "crs": "EPSG:3035", "size_m": 200})


class PriorSpec(_Strict):
    """A scalar epistemic parameter: fixed, or sampled once per outer Monte Carlo iteration."""

    prior: Literal["fixed", "lognormal", "gamma", "uniform"] = "fixed"
    value: float | None = None
    median: float | None = None
    gsd: float | None = None  # geometric standard deviation (lognormal)
    mean: float | None = None
    cv: float | None = None  # coefficient of variation (gamma)
    low: float | None = None
    high: float | None = None

    @model_validator(mode="after")
    def _check(self) -> PriorSpec:
        need = {
            "fixed": ("value",),
            "lognormal": ("median", "gsd"),
            "gamma": ("mean", "cv"),
            "uniform": ("low", "high"),
        }[self.prior]
        missing = [k for k in need if getattr(self, k) is None]
        if missing:
            raise ValueError(f"prior '{self.prior}' requires {missing}")
        return self

    def sample(self, rng: np.random.Generator) -> float:
        match self.prior:
            case "fixed":
                return float(self.value)  # type: ignore[arg-type]
            case "lognormal":
                return float(rng.lognormal(np.log(self.median), np.log(self.gsd)))  # type: ignore[arg-type]
            case "gamma":
                shape = 1.0 / (self.cv**2)  # type: ignore[operator]
                return float(rng.gamma(shape, self.mean / shape))  # type: ignore[operator]
            case "uniform":
                return float(rng.uniform(self.low, self.high))  # type: ignore[arg-type]
        raise AssertionError(self.prior)

    def central(self) -> float:
        return float({"fixed": self.value, "lognormal": self.median, "gamma": self.mean}.get(
            self.prior, ((self.low or 0.0) + (self.high or 0.0)) / 2.0))


class HazardSpec(_Strict):
    model: Literal["flood_footprints"] = "flood_footprints"
    scenarios: list[str] | None = None  # None = all scenarios of the release for this hazard


class FragilitySpec(_Strict):
    library: str = "configs/fragility/v1.yaml"


class DependencySpec(_Strict):
    model: str = "proximity_softmax@1.0.0"
    rules: str = "knowledge/dependency_rules.yaml"
    lambda_scale: PriorSpec = Field(default_factory=lambda: PriorSpec(prior="fixed", value=1.0))
    backup_scale: PriorSpec = Field(default_factory=lambda: PriorSpec(prior="fixed", value=1.0))
    enabled: bool = True  # False = no-interdependency baseline (structural sensitivity)


class CascadeSpec(_Strict):
    model: Literal["topological_temporal"] = "topological_temporal"
    horizon_h: float = 72.0
    topology_mode: Literal["meshed_optimistic"] = "meshed_optimistic"


class MonteCarloSpec(_Strict):
    outer: int = Field(10, ge=1, le=10_000)
    inner: int = Field(50, ge=1, le=100_000)
    seed: int = 20270115
    workers: int = Field(1, ge=1, le=64)

    @property
    def samples_per_scenario(self) -> int:
        return self.outer * self.inner


class OutputSpec(_Strict):
    keep_traces: int = Field(5, ge=0, le=200)
    tier: Literal["public", "internal", "restricted"] = "internal"


class ExperimentConfig(_Strict):
    experiment: str
    release: str
    hazard: HazardSpec = Field(default_factory=HazardSpec)
    fragility: FragilitySpec = Field(default_factory=FragilitySpec)
    dependencies: DependencySpec = Field(default_factory=DependencySpec)
    cascade: CascadeSpec = Field(default_factory=CascadeSpec)
    monte_carlo: MonteCarloSpec = Field(default_factory=MonteCarloSpec)
    disruption_threshold_h: float = Field(4.0, gt=0)
    metrics: list[str] = Field(
        default_factory=lambda: ["people_hours_lost", "p_disruption", "eal", "var_95", "cvar_95", "amplification_ratio"]
    )
    outputs: OutputSpec = Field(default_factory=OutputSpec)
    provenance: dict[str, str] = Field(default_factory=dict)  # authored_by / approved_by (AI artifacts, I6)
