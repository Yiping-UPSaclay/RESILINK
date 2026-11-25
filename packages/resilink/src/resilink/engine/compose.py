"""Composition (ADR-0008 §4, blueprint §6.13): resolve an experiment's components against the registry, validate
their parameters, hash the files they name, and pin the exact code of every model. All of it enters the run_id.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from resilink.domain.config import ComponentSpec, ExperimentConfig
from resilink.engine.setup import EngineSetup
from resilink.io.hashing import sha256_file
from resilink.io.paths import resolve
from resilink.io.release import Release
from resilink.network.compiled import CompiledNetwork
from resilink.plugin_api import ModelPlugin, file_refs, satisfies
from resilink.registry import Pin, PluginRegistry, get_registry

MATURITY_ORDER = ("experimental", "validated", "reference")
_MODEL_SLOTS = ("hazard", "fragility", "dependency", "propagation", "restoration")


class CompositionError(ValueError):
    """The experiment cannot be composed from the installed models (raised before any compute)."""


@dataclass(frozen=True)
class Resolved:
    key: str  # "hazard", "service.default", "consequence.0", "orchestrator", "sampler"
    slot: str
    plugin: ModelPlugin
    params: BaseModel
    pin: Pin
    files: dict[str, str]  # file-reference parameter -> sha256 of the file

    def identity(self) -> dict[str, Any]:
        """Exactly which code and inputs ran: recorded in the run manifest and hashed into the run_id."""
        return {
            "model": self.plugin.id, "version": self.plugin.version, "maturity": self.plugin.card.maturity,
            "distribution": self.pin.distribution, "distribution_version": self.pin.distribution_version,
            "code_hash": self.pin.code_hash, "params": self.params.model_dump(mode="json"), "files": dict(self.files),
        }


@dataclass(frozen=True)
class Composition:
    config: ExperimentConfig
    components: dict[str, Resolved]

    def identity(self) -> dict[str, dict[str, Any]]:
        return {key: r.identity() for key, r in sorted(self.components.items())}

    @property
    def lowest_maturity(self) -> str:
        levels = [r.plugin.card.maturity for r in self.components.values() if r.slot not in ("orchestrator", "sampler")]
        return min(levels, key=MATURITY_ORDER.index) if levels else "reference"


def _resolve(registry: PluginRegistry, slot: str, key: str, spec: ComponentSpec) -> Resolved:
    try:
        plugin = registry.model(spec.model)
    except LookupError as exc:
        raise CompositionError(f"{key}: {exc}") from None
    if plugin.card.slot != slot:
        raise CompositionError(f"{key}: model {spec.model!r} answers the {plugin.card.slot!r} slot, not {slot!r}")
    if spec.version and not satisfies(plugin.version, spec.version):
        raise CompositionError(f"{key}: {spec.model!r} {spec.version} requested; the installed version is "
                               f"{plugin.version}")
    try:
        params = plugin.params.model_validate(spec.params)
    except ValidationError as exc:
        raise CompositionError(f"{key}: invalid parameters for {spec.model!r}: {exc}") from None
    files: dict[str, str] = {}
    for name, ref in file_refs(params).items():
        path = resolve(ref)
        if not path.is_file():
            raise CompositionError(f"{key}: parameter {name!r} names {ref!r}, which does not exist")
        files[name] = sha256_file(path)
    return Resolved(key, slot, plugin, params, registry.pin("models", plugin.id), files)


def compose(cfg: ExperimentConfig | dict[str, Any], registry: PluginRegistry | None = None) -> Composition:
    """Resolve every component of an experiment; fails with a CompositionError before any compute."""
    cfg = cfg if isinstance(cfg, ExperimentConfig) else ExperimentConfig.model_validate(cfg)
    registry = registry or get_registry()
    c = cfg.components
    comps = {slot: _resolve(registry, slot, slot, getattr(c, slot)) for slot in _MODEL_SLOTS}
    for sub, spec in c.service.items():
        comps[f"service.{sub}"] = _resolve(registry, "service", f"service.{sub}", spec)
    for i, spec in enumerate(c.consequence):
        comps[f"consequence.{i}"] = _resolve(registry, "consequence", f"consequence.{i}", spec)
    comps["orchestrator"] = _resolve(registry, "orchestrator", "orchestrator", cfg.orchestrator)
    d = cfg.design
    comps["sampler"] = _resolve(registry, "sampler", "sampler",
                                ComponentSpec(model=d.sampler, params={"outer": d.outer, "inner": d.inner, "seed": d.seed}))
    return Composition(cfg, comps)


@dataclass
class LiveModels:
    """The instantiated models of one experiment, ready for the orchestrator."""

    hazard: Any
    fragility: Any
    dependency: Any
    service: Any
    propagation: Any
    restoration: Any
    consequence: Any
    sampler: Any
    orchestrator: Any


def instantiate(composition: Composition, release: Release, net: CompiledNetwork,
                systems: tuple[str, ...]) -> LiveModels:
    setup = EngineSetup(release=release, net=net, systems=systems)

    def make(key: str) -> Any:
        r = composition.components[key]
        return r.plugin.factory(r.params, setup)

    setup.services["default"] = make("service.default")  # propagation models get it through the setup context
    return LiveModels(
        hazard=make("hazard"), fragility=make("fragility"), dependency=make("dependency"),
        service=setup.services["default"], propagation=make("propagation"), restoration=make("restoration"),
        consequence=make("consequence.0"), sampler=make("sampler"), orchestrator=make("orchestrator"),
    )
