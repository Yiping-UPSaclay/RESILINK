"""Parameter building blocks for plugins: epistemic priors and file references (blueprint §6.5, §6.13)."""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, model_validator


class PriorSpec(BaseModel):
    """A scalar epistemic parameter: fixed, or sampled once per outer Monte Carlo iteration."""

    model_config = ConfigDict(extra="forbid")

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


class FileRef:
    """Marks a parameter that names a file (``Annotated[str, FileRef()]``): its content hash enters the run_id."""

    def __repr__(self) -> str:
        return "FileRef()"


def file_refs(params: BaseModel) -> dict[str, str]:
    """The file-reference parameters of a plugin's params, by name (empty values are skipped)."""
    out: dict[str, Any] = {}
    for name, field in type(params).model_fields.items():
        if any(isinstance(m, FileRef) for m in field.metadata):
            value = getattr(params, name)
            if value:
                out[name] = str(value)
    return out
