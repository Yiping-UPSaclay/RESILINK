"""Cross-infrastructure dependency models (blueprint §5, §6.3): rules + probabilistic inference + sampling."""

from resilink.dependencies.model import (
    DependencyRealisation,
    DependencyRule,
    DependencyRules,
    DependencySet,
    infer_proximity_softmax,
    load_rules,
)

__all__ = [
    "DependencyRealisation",
    "DependencyRule",
    "DependencyRules",
    "DependencySet",
    "infer_proximity_softmax",
    "load_rules",
]
