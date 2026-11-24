"""System packs: one infrastructure system as data (blueprint §6.3, ADR-0008 §2).

A pack declares subsystems, asset classes, what "served" means, optional system tables, repair assumptions,
dependency rules, operators (actors) and a synthetic fixture. New systems (water, roads, heat, hydrogen) are
added as packs, after a one-page extension proposal (blueprint §6.14).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from resilink.plugin_api.versioning import check_id, check_semver

CLASS_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Subsystem(_Frozen):
    nodes: tuple[str, ...]
    links: tuple[str, ...] = ()
    default_service: str = "connectivity"  # model id used for the service slot unless the experiment overrides it


class Served(_Frozen):
    measure: str  # e.g. "load_served_fraction", "pressure_available", "site_on_air"
    population_supplier: str | None = None  # node class that supplies grid cells, if any


class AttributeDecl(_Frozen):
    unit: str
    required: bool = False
    description: str = ""


class SystemPack(_Frozen):
    id: str
    version: str
    title: str
    subsystems: dict[str, Subsystem]
    served: Served
    attributes: dict[str, dict[str, AttributeDecl]] = Field(default_factory=dict)  # class -> column -> decl
    tables: dict[str, dict[str, str]] = Field(default_factory=dict)  # optional system tables: name -> column -> unit
    repair_median_h: dict[str, float] = Field(default_factory=dict)  # class -> median repair time
    repair_assumption: str | None = None  # assumption ID behind repair_median_h
    dependency_rules: str | None = None  # path of the rules file (relative to the repository root)
    actors: dict[str, tuple[str, ...]] = Field(default_factory=dict)  # operator -> subsystems it runs
    synthetic: str | None = None  # "module:attr" of the fixture used by the conformance kit

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return check_id(v, "system id")

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        return check_semver(v, "system pack version")

    @model_validator(mode="after")
    def _consistency(self) -> SystemPack:
        if not self.subsystems:
            raise ValueError(f"system {self.id!r} needs at least one subsystem")
        seen: dict[str, str] = {}
        for sub_name, sub in self.subsystems.items():
            if not (sub_name == self.id or sub_name.startswith(f"{self.id}_")):
                raise ValueError(f"subsystem {sub_name!r} must be named '{self.id}' or '{self.id}_<name>'")
            for c in (*sub.nodes, *sub.links):
                if not CLASS_RE.match(c):
                    raise ValueError(f"class name {c!r} must be lower-case letters, digits and '_'")
                if c in seen:
                    raise ValueError(f"class {c!r} appears in both {seen[c]!r} and {sub_name!r}")
                seen[c] = sub_name
        if self.served.population_supplier and self.served.population_supplier not in self.node_classes:
            raise ValueError(f"population supplier {self.served.population_supplier!r} is not a node class "
                             f"of {self.id!r}")
        unknown = [c for c in (*self.attributes, *self.repair_median_h) if c not in seen]
        if unknown:
            raise ValueError(f"attributes or repair times declared for unknown classes {sorted(set(unknown))}")
        if any(v <= 0 for v in self.repair_median_h.values()):
            raise ValueError("repair median times must be positive")
        for t in self.tables:
            if not t.startswith(f"{self.id}_"):
                raise ValueError(f"system table {t!r} must be named '{self.id}_<name>'")
        for actor, subs in self.actors.items():
            missing = [s for s in subs if s not in self.subsystems]
            if missing:
                raise ValueError(f"actor {actor!r} runs unknown subsystems {missing}")
        return self

    @property
    def node_classes(self) -> tuple[str, ...]:
        return tuple(c for s in self.subsystems.values() for c in s.nodes)

    @property
    def link_classes(self) -> tuple[str, ...]:
        return tuple(c for s in self.subsystems.values() for c in s.links)

    def subsystem_of(self, cls: str) -> str | None:
        for name, sub in self.subsystems.items():
            if cls in sub.nodes or cls in sub.links:
                return name
        return None

    @classmethod
    def from_yaml(cls, path: str | Path) -> SystemPack:
        return cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
