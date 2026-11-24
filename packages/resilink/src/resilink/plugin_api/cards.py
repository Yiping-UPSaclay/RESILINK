"""Model cards and the plugin object every model ships (blueprint §6.5, ADR-0008 §4)."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from resilink.plugin_api.board import FIELD_RE, STANDARD, MetricDecl, StateField, TableDecl
from resilink.plugin_api.versioning import check_id, check_semver, check_spec

STREAM_RE = re.compile(r"^[a-z][a-z0-9_]*$")
Maturity = Literal["experimental", "validated", "reference"]
ExecMode = Literal["in_process", "worker", "batch"]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Citation(_Frozen):
    kind: Literal["thesis", "article", "report", "dataset", "software"]
    title: str
    authors: tuple[str, ...]
    year: int
    doi: str | None = None
    url: str | None = None


class Applicability(_Frozen):
    im_types: tuple[str, ...] = ()  # hazard intensity types, e.g. "flood_depth_m"
    systems: tuple[str, ...] = ()  # system pack ids
    classes: tuple[str, ...] = ()  # asset classes
    time_scale: Literal["episode", "lifecycle", "both"] = "episode"


class DataNeed(_Frozen):
    """A release table (and columns, with units) a model needs; checked before any compute (blueprint §6.9)."""

    table: str
    columns: dict[str, str] = Field(default_factory=dict)  # column -> unit


class Execution(_Frozen):
    """How a model runs (blueprint §6.10). Worker and batch modes need a pinned environment."""

    mode: ExecMode = "in_process"
    environment: str | None = None  # uv project dir, container image@digest, or Julia project dir
    entry: str | None = None  # command or module run inside that environment

    @model_validator(mode="after")
    def _env(self) -> Execution:
        if self.mode != "in_process" and not (self.environment and self.entry):
            raise ValueError(f"execution mode {self.mode!r} needs both 'environment' and 'entry'")
        return self


class ModelCard(_Frozen):
    id: str
    version: str  # SemVer of the model, independent of the package version
    slot: str
    slot_version: str = ">=0.1,<0.2"  # supported range of the slot definition
    subsystems: tuple[str, ...] = ()  # for per_subsystem slots: where the model may be used
    summary: str  # agent- and human-facing: what it does AND when to use it
    authors: tuple[str, ...]
    affiliation: str | None = None
    citation: Citation | None = None
    licence: str  # SPDX identifier
    maturity: Maturity
    assumptions: tuple[str, ...] = ()  # A-... IDs
    applies_to: Applicability = Applicability()
    reads: tuple[str, ...] = ()
    writes: tuple[StateField, ...] = ()
    tables: tuple[TableDecl, ...] = ()
    metrics: tuple[MetricDecl, ...] = ()
    streams: tuple[str, ...] = ()
    requires: tuple[DataNeed, ...] = ()
    optional: tuple[DataNeed, ...] = ()
    execution: Execution = Execution()
    runtime_hint_ms: float | None = None
    api: str = ">=0.1,<0.2"  # supported PLUGIN_API_VERSION range

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return check_id(v, "model id")

    @field_validator("slot")
    @classmethod
    def _slot(cls, v: str) -> str:
        return check_id(v, "slot id")

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        return check_semver(v, "model version")

    @field_validator("slot_version", "api")
    @classmethod
    def _ranges(cls, v: str) -> str:
        return check_spec(v)

    @field_validator("summary")
    @classmethod
    def _summary(cls, v: str) -> str:
        if len(v.strip()) < 30:
            raise ValueError("summary must say what the model does and when to use it (at least 30 characters)")
        return v

    @field_validator("authors")
    @classmethod
    def _authors(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v or not all(a.strip() for a in v):
            raise ValueError("at least one author is required")
        return v

    @field_validator("reads")
    @classmethod
    def _reads(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        bad = [f for f in v if not FIELD_RE.match(f)]
        if bad:
            raise ValueError(f"read fields must look like 'namespace.field': {bad}")
        return v

    @field_validator("streams")
    @classmethod
    def _streams(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        bad = [s for s in v if not STREAM_RE.match(s)]
        if bad:
            raise ValueError(f"stream names must be lower-case identifiers: {bad}")
        return v

    @model_validator(mode="after")
    def _ownership(self) -> ModelCard:
        own = f"{self.id}."
        for f in self.writes:
            if f.name in STANDARD:
                std = STANDARD[f.name]
                if (f.dtype, f.unit) != (std.dtype, std.unit):
                    raise ValueError(f"standard field {f.name!r} is {std.dtype} in {std.unit!r}; "
                                     f"the card declares {f.dtype} in {f.unit!r}")
            elif not f.name.startswith(own):
                raise ValueError(f"model {self.id!r} may write only standard fields or its own namespace "
                                 f"'{own}*', not {f.name!r}")
        tables = {t.name: t for t in self.tables}
        for t in self.tables:
            if not t.name.startswith(own):
                raise ValueError(f"table {t.name!r} must be in the model's namespace '{own}*'")
        for m in self.metrics:
            if not m.id.startswith(own):
                raise ValueError(f"metric {m.id!r} must be in the model's namespace '{own}*'")
            table, column = m.source.rsplit(".", 1)
            if table not in tables or column not in tables[table].column_names:
                raise ValueError(f"metric {m.id!r} reads {m.source!r}, which is not a declared table column")
        if self.maturity in ("validated", "reference") and self.citation is None:
            raise ValueError(f"a {self.maturity!r} model needs a citation")
        return self

    @property
    def owned_fields(self) -> set[str]:
        return {f.name for f in self.writes}


@dataclass(frozen=True)
class ModelPlugin:
    """What a ``resilink.models`` entry point points to: a card, a parameter model and a factory."""

    card: ModelCard
    params: type[BaseModel]
    factory: Callable[[Any, Any], object]  # factory(params, setup_context) -> object implementing the slot protocol

    def __post_init__(self) -> None:
        if not (isinstance(self.params, type) and issubclass(self.params, BaseModel)):
            raise TypeError(f"{self.card.id}: params must be a pydantic model class")
        if not callable(self.factory):
            raise TypeError(f"{self.card.id}: factory must be callable")

    @property
    def id(self) -> str:
        return self.card.id

    @property
    def version(self) -> str:
        return self.card.version

    def params_schema(self) -> dict[str, Any]:
        return self.params.model_json_schema()
