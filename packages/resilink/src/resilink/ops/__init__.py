"""Operation Registry — the single typed contract of the lab (blueprint §1.5, decision D17).

MCP tools, CLI commands, UI forms, REST routes and agent tool definitions are generated projections of
this registry; humans and agents call exactly the same operations (invariant I7).
"""

from resilink.ops.registry import (
    REGISTRY,
    OpContext,
    OpDenied,
    OpResult,
    OpSpec,
    SideEffect,
    call,
    op,
)

__all__ = ["REGISTRY", "OpContext", "OpDenied", "OpResult", "OpSpec", "SideEffect", "call", "op"]


def load_builtin_ops() -> None:
    """Import side-effect: registers the built-in operations."""
    from resilink.ops import builtin  # noqa: F401
