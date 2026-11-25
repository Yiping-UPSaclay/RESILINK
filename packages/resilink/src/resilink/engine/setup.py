"""The setup context the engine hands to model factories (implements ``plugin_api.SetupContext``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from resilink.io.paths import resolve
from resilink.io.release import Release
from resilink.network.compiled import CompiledNetwork


@dataclass
class EngineSetup:
    release: Release
    net: CompiledNetwork
    systems: tuple[str, ...]
    services: dict[str, Any] = field(default_factory=dict)

    def resolve(self, ref: str) -> Path:
        return resolve(ref)

    def service_model(self, subsystem: str | None = None) -> Any:
        if subsystem and subsystem in self.services:
            return self.services[subsystem]
        return self.services["default"]
