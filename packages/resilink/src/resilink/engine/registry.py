"""Registries of system packs, slot definitions and models, discovered through Python entry points.

ADR-0008 §1-§4, blueprint §6.13. Built-in and external plugins take the same path. A plugin that fails to load,
or fails a check, is reported as unavailable with the reason; it never stops the platform. Every registered
plugin is pinned (distribution, version, code hash) so that run manifests can record exactly which code ran.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from resilink.engine.policy import PluginPolicy, load_policy
from resilink.plugin_api import PLUGIN_API_VERSION, ModelPlugin, SlotDefinition, SystemPack, satisfies

GROUPS = {"systems": "resilink.systems", "slots": "resilink.slots", "models": "resilink.models"}
_EXPECTED: dict[str, type] = {"systems": SystemPack, "slots": SlotDefinition, "models": ModelPlugin}


@dataclass(frozen=True)
class Pin:
    """Exact identity of a plugin's code; enters run manifests and the run_id (invariant I3)."""

    group: str
    name: str
    value: str  # entry-point object reference, "module:attr"
    distribution: str | None
    distribution_version: str | None
    code_hash: str  # sha256 (16 hex) of the providing top-level package's files


@dataclass(frozen=True)
class Registered:
    obj: Any
    pin: Pin


def code_hash(top_module: str) -> str:
    """Hash of every file of a top-level package (or of a single-file module), independent of install mode."""
    try:
        spec = importlib.util.find_spec(top_module)
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return "unknown"
    h = hashlib.sha256()
    if spec.submodule_search_locations:
        for root in sorted(str(r) for r in spec.submodule_search_locations):
            base = Path(root)
            for p in sorted(base.rglob("*")):
                if p.is_file() and "__pycache__" not in p.parts and p.suffix not in (".pyc", ".pyo"):
                    h.update(p.relative_to(base).as_posix().encode())
                    h.update(b"\0")
                    h.update(p.read_bytes())
                    h.update(b"\0")
    elif spec.origin and Path(spec.origin).is_file():
        h.update(Path(spec.origin).read_bytes())
    else:
        return "unknown"
    return h.hexdigest()[:16]


def _default_entry_points(group: str) -> Iterable[Any]:
    return importlib.metadata.entry_points(group=group)  # selection by group, Python >= 3.10


class PluginRegistry:
    def __init__(self, entry_points: Callable[[str], Iterable[Any]] | None = None,
                 policy: PluginPolicy | None = None) -> None:
        self._entry_points = entry_points or _default_entry_points
        self.policy = policy or PluginPolicy()
        self.systems: dict[str, Registered] = {}
        self.slots: dict[str, Registered] = {}
        self.models: dict[str, Registered] = {}
        self.unavailable: dict[tuple[str, str], str] = {}
        self.warnings: list[str] = []
        self._hashes: dict[str, str] = {}
        self._loaded = False

    # ---- loading -------------------------------------------------------------------------------------------
    def load(self) -> PluginRegistry:
        if self._loaded:
            return self
        for kind in ("systems", "slots", "models"):
            for ep in sorted(self._entry_points(GROUPS[kind]), key=lambda e: (e.name, e.value)):
                self._add(kind, ep)
        self._cross_check()
        self._loaded = True
        return self

    def _refuse(self, kind: str, name: str, reason: str) -> None:
        self.unavailable[(GROUPS[kind], name)] = reason

    def _add(self, kind: str, ep: Any) -> None:
        if ep.name in self.policy.disabled:
            self._refuse(kind, ep.name, "disabled in configs/plugins.yaml")
            return
        try:
            obj = ep.load()
        except Exception as exc:  # a broken plugin is reported, never fatal
            self._refuse(kind, ep.name, f"failed to load: {type(exc).__name__}: {exc}")
            return
        expected = _EXPECTED[kind]
        if not isinstance(obj, expected):
            self._refuse(kind, ep.name, f"expected a {expected.__name__}, got {type(obj).__name__}")
            return
        if obj.id != ep.name:
            self._refuse(kind, ep.name, f"entry-point name {ep.name!r} differs from the declared id {obj.id!r}")
            return
        table: dict[str, Registered] = getattr(self, kind)
        if obj.id in table:
            first = table[obj.id].pin
            self._refuse(kind, ep.name, f"duplicate id {obj.id!r}: already provided by "
                                        f"{first.distribution or first.value}")
            return
        if kind == "models":
            card = obj.card
            if not satisfies(PLUGIN_API_VERSION, card.api):
                self._refuse(kind, ep.name, f"written for plugin API {card.api}; the installed API is "
                                            f"{PLUGIN_API_VERSION}")
                return
            if not self.policy.licence_allowed(card.licence):
                self._refuse(kind, ep.name, f"licence {card.licence!r} needs review before use "
                                            "(configs/plugins.yaml)")
                return
        table[obj.id] = Registered(obj, self._pin(kind, ep))

    def _cross_check(self) -> None:
        subsystems = {s for reg in self.systems.values() for s in reg.obj.subsystems}
        for mid in list(self.models):
            card = self.models[mid].obj.card
            slot_reg = self.slots.get(card.slot)
            problem = None
            if slot_reg is None:
                problem = f"slot {card.slot!r} is not registered"
            else:
                slot: SlotDefinition = slot_reg.obj
                if not satisfies(slot.version, card.slot_version):
                    problem = (f"needs slot {card.slot!r} {card.slot_version}; the registered version is "
                               f"{slot.version}")
                elif slot.cardinality == "per_subsystem":
                    unknown = sorted(set(card.subsystems) - subsystems)
                    if unknown:
                        problem = f"declares unknown subsystems {unknown}"
                elif card.subsystems:
                    problem = f"slot {card.slot!r} is not per subsystem, but the card lists subsystems"
                if problem is None and card.execution.mode not in slot.execution:
                    problem = f"slot {card.slot!r} does not allow execution mode {card.execution.mode!r}"
            if problem:
                del self.models[mid]
                self._refuse("models", mid, problem)
        for sid, reg in self.slots.items():
            default = reg.obj.default_model
            if default and default not in self.models:
                self.warnings.append(f"slot {sid!r}: default model {default!r} is not registered")

    def _pin(self, kind: str, ep: Any) -> Pin:
        dist = getattr(ep, "dist", None)
        module = getattr(ep, "module", None) or ep.value.split(":")[0]
        top = module.split(".")[0]
        if top not in self._hashes:
            self._hashes[top] = code_hash(top)
        return Pin(group=GROUPS[kind], name=ep.name, value=ep.value,
                   distribution=getattr(dist, "name", None), distribution_version=getattr(dist, "version", None),
                   code_hash=self._hashes[top])

    # ---- lookup --------------------------------------------------------------------------------------------
    def _get(self, kind: str, ident: str) -> Registered:
        self.load()
        table: dict[str, Registered] = getattr(self, kind)
        if ident in table:
            return table[ident]
        why = self.unavailable.get((GROUPS[kind], ident))
        hint = f": {why}" if why else f"; registered: {', '.join(sorted(table)) or 'none'}"
        raise LookupError(f"no {kind[:-1]} {ident!r}{hint}")

    def system(self, ident: str) -> SystemPack:
        return self._get("systems", ident).obj

    def slot(self, ident: str) -> SlotDefinition:
        return self._get("slots", ident).obj

    def model(self, ident: str) -> ModelPlugin:
        return self._get("models", ident).obj

    def pin(self, kind: str, ident: str) -> Pin:
        return self._get(kind, ident).pin

    def describe(self) -> dict[str, Any]:
        """Machine-readable summary (CLI today; the discovery ops of step E9 later)."""
        self.load()

        def rows(kind: str, extra: Callable[[Any], dict[str, Any]]) -> list[dict[str, Any]]:
            return [{"id": i, "version": r.obj.version, **extra(r.obj), "pin": asdict(r.pin)}
                    for i, r in sorted(getattr(self, kind).items())]

        return {
            "plugin_api": PLUGIN_API_VERSION,
            "systems": rows("systems", lambda p: {"title": p.title, "subsystems": sorted(p.subsystems)}),
            "slots": rows("slots", lambda s: {"question": s.question, "cardinality": s.cardinality}),
            "models": rows("models", lambda m: {"slot": m.card.slot, "maturity": m.card.maturity,
                                                "summary": m.card.summary}),
            "unavailable": [{"group": g, "name": n, "reason": why} for (g, n), why in sorted(self.unavailable.items())],
            "warnings": list(self.warnings),
        }


@lru_cache(maxsize=1)
def get_registry() -> PluginRegistry:
    """The process-wide registry of installed plugins, loaded once with the repository's policy."""
    return PluginRegistry(policy=load_policy()).load()
