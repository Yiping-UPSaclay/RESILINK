"""RESILINK plugin API: the only part of the core that plugins import (ADR-0008, blueprint §6.5).

Draft (0.x) until two independent theses have used it; after 1.0, minor releases only add and a major release
coexists with the previous one for at least two platform releases or six months (ADR-0008 §10).
"""

from resilink.plugin_api.board import (
    STANDARD,
    ColumnDecl,
    MetricDecl,
    StateField,
    TableDecl,
    is_standard,
)
from resilink.plugin_api.cards import (
    Applicability,
    Citation,
    DataNeed,
    Execution,
    ModelCard,
    ModelPlugin,
)
from resilink.plugin_api.contexts import NetworkView, ReleaseView, SetupContext, SimContext
from resilink.plugin_api.slots import (
    CORE_EVENTS,
    DAMAGE_CHANGED,
    DECISION_EPOCH,
    ELEMENT_CHANGED,
    EPISODE_END,
    EPISODE_START,
    HAZARD_CHANGED,
    OUTER_START,
    SERVICE_CHANGED,
    On,
    SlotDefinition,
)
from resilink.plugin_api.systems import AttributeDecl, Served, Subsystem, SystemPack
from resilink.plugin_api.versioning import PLUGIN_API_VERSION, satisfies

__all__ = [
    "CORE_EVENTS", "DAMAGE_CHANGED", "DECISION_EPOCH", "ELEMENT_CHANGED", "EPISODE_END", "EPISODE_START",
    "HAZARD_CHANGED", "OUTER_START", "PLUGIN_API_VERSION", "SERVICE_CHANGED", "STANDARD",
    "Applicability", "AttributeDecl", "Citation", "ColumnDecl", "DataNeed", "Execution", "MetricDecl",
    "ModelCard", "ModelPlugin", "NetworkView", "On", "ReleaseView", "Served", "SetupContext", "SimContext",
    "SlotDefinition", "StateField", "Subsystem", "SystemPack", "TableDecl", "is_standard", "satisfies",
]
