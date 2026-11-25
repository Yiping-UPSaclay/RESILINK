"""Controlled vocabularies shared by every RESILINK component (blueprint §4.5).

System and asset-class vocabularies are no longer constants here: they come from the installed system
packs (ADR-0008 §2; see ``resilink.systems`` and ``resilink.registry.SystemCatalog``)."""

from __future__ import annotations

from enum import IntEnum


class Tier(IntEnum):
    """Sensitivity tier (rule R5). Ordered: a principal cleared for a tier can see all lower tiers."""

    PUBLIC = 0
    INTERNAL = 1
    RESTRICTED = 2

    @classmethod
    def parse(cls, value: str | int | Tier) -> Tier:
        if isinstance(value, Tier):
            return value
        if isinstance(value, int):
            return cls(value)
        return cls[value.strip().upper()]

    @property
    def label(self) -> str:
        return self.name.lower()


# Rinaldi et al. (2001) dependency taxonomy
DEP_TYPES: tuple[str, ...] = ("physical", "cyber", "geographic", "logical")
MECHANISMS: tuple[str, ...] = ("power_supply", "fuel", "scada", "backhaul", "service")

CAUSE_DIRECT = 1  # physical damage from the hazard
CAUSE_INTRA = 2  # lost connectivity to a source within its own layer
CAUSE_INTER = 3  # a supporting asset in another layer was down longer than the backup autonomy
CAUSE_LABEL: dict[int, str] = {CAUSE_DIRECT: "direct", CAUSE_INTRA: "intra", CAUSE_INTER: "inter"}
