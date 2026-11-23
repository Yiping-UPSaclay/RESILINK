"""Slot definitions: registered, versioned kinds of modelling question (blueprint §6.4, ADR-0008 §3).

A slot is data plus a ``Protocol``, not code inside the engine. Slots subscribe to kernel events (blueprint §6.6)
and say which standard board fields their models own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, get_args

from resilink.plugin_api.board import STANDARD
from resilink.plugin_api.versioning import check_id, check_semver

EVENT_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")

# Kernel events v0.1. Slots may add namespaced events "<slot_id>.<name>".
OUTER_START = "outer.start"
EPISODE_START = "episode.start"
HAZARD_CHANGED = "hazard.changed"
DAMAGE_CHANGED = "damage.changed"
ELEMENT_CHANGED = "element.changed"
SERVICE_CHANGED = "service.changed"
DECISION_EPOCH = "decision.epoch"
EPISODE_END = "episode.end"
CORE_EVENTS = frozenset({OUTER_START, EPISODE_START, HAZARD_CHANGED, DAMAGE_CHANGED, ELEMENT_CHANGED,
                         SERVICE_CHANGED, DECISION_EPOCH, EPISODE_END})

Cardinality = Literal["single", "per_subsystem", "per_actor", "many"]
ExecMode = Literal["in_process", "worker", "batch"]


@dataclass(frozen=True)
class On:
    """A trigger: run the slot's handler when ``event`` fires; ``phase`` orders handlers at the same instant."""

    event: str
    phase: int = 50

    def __post_init__(self) -> None:
        if not EVENT_RE.match(self.event):
            raise ValueError(f"event {self.event!r} must look like 'namespace.name'")
        if not 0 <= self.phase <= 100:
            raise ValueError(f"phase {self.phase} must be between 0 and 100")


@dataclass(frozen=True)
class SlotDefinition:
    id: str
    version: str
    question: str
    protocol: type  # the methods a model for this slot must implement
    cardinality: Cardinality = "single"
    triggers: tuple[On, ...] = ()
    owns: tuple[str, ...] = ()  # standard board fields its models may write
    emits: tuple[str, ...] = ()  # events its models may schedule
    checks: tuple[str, ...] = ()  # property checks the conformance kit runs for this slot
    default_model: str | None = None  # the "do nothing special" model
    ensembles: bool = True
    execution: tuple[ExecMode, ...] = ("in_process",)
    engine_level: bool = False  # orchestrator / sampler slots used by the engine itself

    def __post_init__(self) -> None:
        for name in ("triggers", "owns", "emits", "checks", "execution"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        check_id(self.id, "slot id")
        check_semver(self.version, "slot version")
        if len(self.question.strip()) < 15:
            raise ValueError(f"slot {self.id!r}: state the question it answers")
        if not isinstance(self.protocol, type):
            raise TypeError(f"slot {self.id!r}: protocol must be a class (typing.Protocol)")
        if self.cardinality not in get_args(Cardinality):
            raise ValueError(f"slot {self.id!r}: cardinality must be one of {get_args(Cardinality)}")
        if not all(isinstance(t, On) for t in self.triggers):
            raise TypeError(f"slot {self.id!r}: triggers must be On(...) instances")
        unknown = [f for f in self.owns if f not in STANDARD]
        if unknown:
            raise ValueError(f"slot {self.id!r} owns unknown standard fields {unknown}; "
                             "adding a standard field is a plugin-API change")
        for ev in self.emits:
            if not EVENT_RE.match(ev):
                raise ValueError(f"slot {self.id!r}: event {ev!r} must look like 'namespace.name'")
            if ev not in CORE_EVENTS and not ev.startswith(f"{self.id}."):
                raise ValueError(f"slot {self.id!r} may define new events only as '{self.id}.<name>', not {ev!r}")
        bad_modes = [m for m in self.execution if m not in get_args(ExecMode)]
        if bad_modes:
            raise ValueError(f"slot {self.id!r}: unknown execution modes {bad_modes}")
        if self.default_model is not None:
            check_id(self.default_model, "default model id")

    @property
    def events(self) -> set[str]:
        return {t.event for t in self.triggers}
