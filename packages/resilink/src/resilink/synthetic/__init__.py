"""Deterministic synthetic mini-territory used by tests, evals and the demo (blueprint §14 step 5)."""

from resilink.synthetic.territory import (
    SYNTHETIC_RELEASE_ID,
    build_synthetic_tables,
    ensure_synthetic_release,
)

__all__ = ["SYNTHETIC_RELEASE_ID", "build_synthetic_tables", "ensure_synthetic_release"]
