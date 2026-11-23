"""Controlled vocabularies shared by every RESILINK component (blueprint §4.5)."""

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


LAYERS: tuple[str, ...] = ("elec", "gas", "telecom", "facility")
LAYER_CODE: dict[str, int] = {name: i for i, name in enumerate(LAYERS)}

NODE_CLASSES: dict[str, tuple[str, ...]] = {
    "elec": ("sub_tx", "sub_primary", "sub_mv_lv", "generator"),
    "gas": ("junction", "compressor", "delivery_station", "lng_terminal", "storage", "commune_distribution"),
    "telecom": ("mobile_site", "exchange", "aggregation_logical"),
    "facility": ("health", "emergency"),
}

EDGE_CLASSES: dict[str, tuple[str, ...]] = {
    "elec": ("line_hv", "line_mv_aerial", "cable_mv"),
    "gas": ("pipeline_tx",),
    "telecom": ("backhaul_logical",),
}

# Rinaldi et al. (2001) dependency taxonomy
DEP_TYPES: tuple[str, ...] = ("physical", "cyber", "geographic", "logical")
MECHANISMS: tuple[str, ...] = ("power_supply", "fuel", "scada", "backhaul", "service")

# Which node class supplies each demand sector to grid cells (service links)
SECTOR_SUPPLIER_CLASS: dict[str, tuple[str, str]] = {
    "elec": ("elec", "sub_mv_lv"),
    "gas": ("gas", "delivery_station"),
    "telecom": ("telecom", "mobile_site"),
}
SECTORS: tuple[str, ...] = tuple(SECTOR_SUPPLIER_CLASS)

# Repair-time class per node class (hours, lognormal median) — placeholder priors, assumption A-REP-01
REPAIR_MEDIAN_H: dict[str, float] = {
    "sub_tx": 48.0,
    "sub_primary": 36.0,
    "sub_mv_lv": 12.0,
    "generator": 72.0,
    "junction": 24.0,
    "compressor": 48.0,
    "delivery_station": 24.0,
    "lng_terminal": 96.0,
    "storage": 96.0,
    "commune_distribution": 24.0,
    "mobile_site": 8.0,
    "exchange": 24.0,
    "aggregation_logical": 12.0,
    "line_hv": 48.0,
    "line_mv_aerial": 10.0,
    "cable_mv": 24.0,
    "pipeline_tx": 72.0,
    "backhaul_logical": 12.0,
}

CAUSE_DIRECT = 1  # physical damage from the hazard
CAUSE_INTRA = 2  # lost connectivity to a source within its own layer
CAUSE_INTER = 3  # a supporting asset in another layer was down longer than the backup autonomy
CAUSE_LABEL: dict[int, str] = {CAUSE_DIRECT: "direct", CAUSE_INTRA: "intra", CAUSE_INTER: "inter"}
