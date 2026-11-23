"""The small fixed core of the analytics engine (ADR-0008): registries, composition, kernel, reducers.

Step E1 provides the registries; composition, the event kernel and execution modes follow in steps E4-E7.
"""

from resilink.engine.policy import PluginPolicy, load_policy
from resilink.engine.registry import GROUPS, Pin, PluginRegistry, Registered, code_hash, get_registry

__all__ = ["GROUPS", "Pin", "PluginPolicy", "PluginRegistry", "Registered", "code_hash", "get_registry",
           "load_policy"]
