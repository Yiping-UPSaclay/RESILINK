"""System catalog: the registered system packs as one lookup table for the core (ADR-0008 §2, blueprint §6.3).

It replaces the system constants that Sprint 0 hard-coded in ``domain/vocab.py``: which systems exist, their
node and link classes, repair times, and which class supplies the population grid.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

import pandas as pd

from resilink.plugin_api import SystemPack


@dataclass(frozen=True)
class SystemCatalog:
    packs: tuple[SystemPack, ...]
    _by_id: dict[str, SystemPack] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        packs = tuple(sorted(self.packs, key=lambda p: p.id))
        ids = [p.id for p in packs]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate system packs in catalog: {ids}")
        object.__setattr__(self, "packs", packs)
        object.__setattr__(self, "_by_id", {p.id: p for p in packs})

    @property
    def systems(self) -> tuple[str, ...]:
        return tuple(self._by_id)

    def pack(self, system: str) -> SystemPack:
        try:
            return self._by_id[system]
        except KeyError:
            raise KeyError(f"no system pack {system!r} is installed (installed: {', '.join(self.systems) or 'none'})"
                           ) from None

    def population_suppliers(self, systems: Iterable[str] | None = None) -> tuple[tuple[str, str], ...]:
        """(system, supplier class) for systems that serve the population grid, in system-id order."""
        wanted = None if systems is None else set(systems)
        return tuple((p.id, p.served.population_supplier) for p in self.packs
                     if p.served.population_supplier and (wanted is None or p.id in wanted))

    def repair_median_h(self, system: str, cls: str) -> float:
        try:
            return float(self.pack(system).repair_median_h[cls])
        except KeyError:
            raise KeyError(f"system pack {system!r} declares no repair time for class {cls!r}") from None

    def problems(self, nodes: pd.DataFrame, edges: pd.DataFrame | None = None) -> list[str]:
        """Every (system, class) in the tables must be declared by an installed pack, with a repair time."""
        out: list[str] = []
        for kind, df, attr in (("node", nodes, "node_classes"), ("edge", edges, "link_classes")):
            if df is None or df.empty:
                continue
            for layer, cls in sorted(df[["layer", "class"]].drop_duplicates().itertuples(index=False, name=None)):
                pack = self._by_id.get(layer)
                if pack is None:
                    out.append(f"{kind} class {cls!r} belongs to system {layer!r}, which has no installed system pack")
                elif cls not in getattr(pack, attr):
                    out.append(f"{kind} class {cls!r} is not declared by system pack {layer!r}")
                elif cls not in pack.repair_median_h:
                    out.append(f"system pack {layer!r} declares no repair time for {kind} class {cls!r}")
        return out
