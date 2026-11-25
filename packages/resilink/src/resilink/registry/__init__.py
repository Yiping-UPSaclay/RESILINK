"""Registry of installed extensions: system packs, slot definitions and models (ADR-0008 §1, blueprint §6.13).

It sits low in the core (just above ``io``) because network compilation, the synthetic territory and ETL checks
all need the system catalog; the engine (composition, kernel) builds on it.
"""

from resilink.registry.catalog import SystemCatalog
from resilink.registry.core import (
    GROUPS,
    Pin,
    PluginRegistry,
    Registered,
    code_hash,
    get_registry,
    system_catalog,
)
from resilink.registry.policy import PluginPolicy, load_policy

__all__ = ["GROUPS", "Pin", "PluginPolicy", "PluginRegistry", "Registered", "SystemCatalog", "code_hash",
           "get_registry", "load_policy", "system_catalog"]
