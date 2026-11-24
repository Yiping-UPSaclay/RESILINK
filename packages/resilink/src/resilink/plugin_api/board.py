"""The shared board: state fields, the standard field vocabulary, and declared tables and metrics.

Plugins never call each other; they read and write declared fields on a shared board (blueprint §6.7). A field
that no active model writes reads as its default, and the composition report lists it (ADR-0008 §6).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

Grain = Literal["node", "edge", "cell", "global"]
DType = Literal["bool", "int8", "int32", "float32", "float64"]
TableGrain = Literal["scenario", "outer", "inner", "t", "node", "edge", "cell", "sector"]
Reducer = Literal["mean", "bands", "var_95", "cvar_95", "mc_rel_se", "eal"]
TierName = Literal["public", "internal", "restricted"]

FIELD_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
TABLE_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
COLUMN_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StateField(_Frozen):
    """One column of the simulation state, with its grain, type, unit and default."""

    name: str  # standard ("asset.condition") or "<model_id>.<field>"
    grain: tuple[Grain, ...]
    dtype: DType
    unit: str
    default: float | int | bool | None = None  # None means NaN (float fields)
    description: str

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not FIELD_RE.match(v):
            raise ValueError(f"field name {v!r} must look like 'namespace.field' (lower case)")
        return v

    @field_validator("grain")
    @classmethod
    def _grain(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("a state field needs at least one grain")
        return v


def _std(name: str, grain: tuple[Grain, ...], dtype: DType, unit: str, default: float | int | bool | None,
         description: str) -> StateField:
    return StateField(name=name, grain=grain, dtype=dtype, unit=unit, default=default, description=description)


# Standard fields v0.1 (blueprint §6.7). Adding one is a minor plugin-API release (ADR-0008 §10).
STANDARD: dict[str, StateField] = {f.name: f for f in (
    _std("hazard.intensity", ("node", "edge"), "float32", "per hazard type", 0.0,
         "Hazard intensity measure at the asset, e.g. flood depth in metres."),
    _std("asset.age_y", ("node", "edge"), "float32", "y", None, "Asset age in years, from the release when known."),
    _std("asset.condition", ("node", "edge"), "float32", "1", 1.0, "Condition index in [0, 1]; 1 means as new."),
    _std("damage.state", ("node", "edge"), "int8", "damage-state index", 0,
         "Damage state; 0 is undamaged (binary models use 0 and 1)."),
    _std("damage.functionality", ("node", "edge"), "float32", "1", 1.0, "Residual functionality in [0, 1]."),
    _std("repair.start_h", ("node", "edge"), "float32", "h", None, "Repair start time; NaN when not damaged."),
    _std("repair.finish_h", ("node", "edge"), "float32", "h", None, "Repair completion time; NaN when not damaged."),
    _std("service.served", ("node",), "bool", "bool", True, "The node receives service."),
    _std("service.level", ("node",), "float32", "1", 1.0, "Fraction of demand served at the node."),
    _std("outage.hours", ("node",), "float32", "h", 0.0, "Accumulated outage time during the episode."),
)}


def is_standard(name: str) -> bool:
    return name in STANDARD


class ColumnDecl(_Frozen):
    name: str
    dtype: str
    unit: str
    description: str

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not COLUMN_RE.match(v):
            raise ValueError(f"column name {v!r} must be lower-case letters, digits and '_'")
        return v


class TableDecl(_Frozen):
    """A result table a model emits: results/<run_id>/ext/<model_id>/<table>.parquet (blueprint §6.12)."""

    name: str  # "<model_id>.<table>"
    grain: tuple[TableGrain, ...]
    columns: tuple[ColumnDecl, ...]
    min_tier: TierName = "internal"  # asset grain is raised to restricted on real territories; never lowered

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not TABLE_RE.match(v):
            raise ValueError(f"table name {v!r} must look like '<model_id>.<table>'")
        return v

    @property
    def column_names(self) -> set[str]:
        return {c.name for c in self.columns}


class MetricDecl(_Frozen):
    """A per-world value the core reduces identically for every model (blueprint §6.12)."""

    id: str  # "<model_id>.<metric>"
    source: str  # "<model_id>.<table>.<column>": one value per (scenario, outer, inner)
    unit: str
    better: Literal["lower", "higher"]
    reducers: tuple[Reducer, ...] = ("mean", "bands", "var_95", "cvar_95", "mc_rel_se")

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not FIELD_RE.match(v):
            raise ValueError(f"metric id {v!r} must look like '<model_id>.<metric>'")
        return v

    @field_validator("source")
    @classmethod
    def _source(cls, v: str) -> str:
        if v.count(".") != 2:
            raise ValueError(f"metric source {v!r} must look like '<model_id>.<table>.<column>'")
        return v
