"""Typed configuration models (territory, experiment). Pure pydantic — loading from disk lives in resilink.io."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from resilink.plugin_api.params import PriorSpec

__all__ = ["ComponentSpec", "Components", "DesignSpec", "ExperimentConfig", "OutputSpec", "PriorSpec",
           "TerritoryConfig", "upgrade_schema_1"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TerritoryConfig(_Strict):
    id: str
    name: str
    selector: dict[str, Any] = Field(default_factory=dict)
    buffer_m: float = 30_000.0
    crs_metric: str = "EPSG:2154"
    grid: dict[str, Any] = Field(default_factory=lambda: {"id": "insee200m", "crs": "EPSG:3035", "size_m": 200})


class ComponentSpec(_Strict):
    """One model for a slot: a registered model id, an optional PEP 440 version range, and its parameters."""

    model: str
    version: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


def _c(model: str, **params: Any) -> ComponentSpec:
    return ComponentSpec(model=model, params=params)


class Components(_Strict):
    hazard: ComponentSpec = Field(default_factory=lambda: _c("flood_footprints"))
    fragility: ComponentSpec = Field(default_factory=lambda: _c("lognormal_library"))
    dependency: ComponentSpec = Field(default_factory=lambda: _c("proximity_softmax"))
    service: dict[str, ComponentSpec] = Field(default_factory=lambda: {"default": _c("connectivity")})
    propagation: ComponentSpec = Field(default_factory=lambda: _c("topological_temporal"))
    restoration: ComponentSpec = Field(default_factory=lambda: _c("independent_repair"))
    consequence: list[ComponentSpec] = Field(default_factory=lambda: [_c("people_hours")])

    @model_validator(mode="after")
    def _check(self) -> Components:
        if "default" not in self.service:
            raise ValueError("components.service needs a 'default' model")
        extra = sorted(set(self.service) - {"default"})
        if extra:
            raise ValueError(f"per-subsystem service models {extra} arrive with the event kernel (ADR-0008 step "
                             "E4); use 'default' for now")
        if len(self.consequence) != 1:
            raise ValueError("exactly one consequence model is supported by the episode orchestrator 0.1")
        return self


class DesignSpec(_Strict):
    """How simulated worlds are drawn (the sampler) and how many."""

    sampler: str = "nested_mc"
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


_V1_KEYS = {"hazard", "fragility", "dependencies", "cascade", "monte_carlo", "disruption_threshold_h"}


def upgrade_schema_1(data: dict[str, Any]) -> dict[str, Any]:
    """Translate a Sprint 0 (schema 1) experiment config into schema 2, choosing the same models and values."""
    d = dict(data)
    for key in ("schema", "schema_version"):
        d.pop(key, None)
    hz = d.pop("hazard", None) or {}
    fr = d.pop("fragility", None) or {}
    dp = d.pop("dependencies", None) or {}
    cs = d.pop("cascade", None) or {}
    mc = d.pop("monte_carlo", None) or {}
    thr = d.pop("disruption_threshold_h", None)

    comps: dict[str, Any] = {
        "hazard": {"model": hz.get("model", "flood_footprints"),
                   "params": {k: v for k, v in hz.items() if k != "model" and v is not None}},
        "fragility": {"model": "lognormal_library", "params": {"library": fr["library"]} if "library" in fr else {}},
        "service": {"default": {"model": "connectivity",
                                "params": {"topology_mode": cs["topology_mode"]} if "topology_mode" in cs else {}}},
        "propagation": {"model": cs.get("model", "topological_temporal")},
        "restoration": {"model": "independent_repair"},
        "consequence": [{"model": "people_hours",
                         "params": {"disruption_threshold_h": thr} if thr is not None else {}}],
    }
    if dp.get("enabled", True) is False:
        comps["dependency"] = {"model": "no_dependencies"}
    else:
        model_id, _, version = str(dp.get("model", "proximity_softmax")).partition("@")
        comps["dependency"] = {"model": model_id, "params": {k: dp[k] for k in ("rules", "lambda_scale",
                                                                                "backup_scale") if k in dp}}
        if version:
            comps["dependency"]["version"] = f"=={version}"
    d["schema"] = 2
    d["orchestrator"] = {"model": "episode", "params": {"horizon_h": cs.get("horizon_h", 72.0)}}
    d["design"] = {"sampler": "nested_mc", **mc}
    d["components"] = comps
    return d


class ExperimentConfig(BaseModel):
    """Experiment configuration, schema 2 (ADR-0008 §4): one model per slot, chosen by id from the registry.

    Schema 1 configs (Sprint 0) are upgraded on load by ``upgrade_schema_1``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal[2] = Field(2, alias="schema")
    experiment: str
    release: str
    orchestrator: ComponentSpec = Field(default_factory=lambda: _c("episode", horizon_h=72.0))
    design: DesignSpec = Field(default_factory=DesignSpec)
    components: Components = Field(default_factory=Components)
    metrics: list[str] = Field(
        default_factory=lambda: ["people_hours_lost", "p_disruption", "eal", "var_95", "cvar_95", "amplification_ratio"]
    )
    outputs: OutputSpec = Field(default_factory=OutputSpec)
    provenance: dict[str, str] = Field(default_factory=dict)  # authored_by / approved_by (AI artifacts, I6)

    @model_validator(mode="before")
    @classmethod
    def _upgrade(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        declared = data.get("schema", data.get("schema_version"))
        if declared == 1 or (declared is None and _V1_KEYS & set(data)):
            return upgrade_schema_1(data)
        return data
