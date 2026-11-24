"""Plugin policy from configs/plugins.yaml: licences accepted without review, and disabled plugins.

The platform licence is Apache-2.0 (decided 2026-11-23). Permissive licences are accepted by default; any other
licence is reviewed before the plugin is enabled. Changing the policy is a reviewed pull request (invariant I2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from resilink.io.paths import repo_root

DEFAULT_LICENCES = ("Apache-2.0", "MIT", "BSD-2-Clause", "BSD-3-Clause", "ISC")


@dataclass(frozen=True)
class PluginPolicy:
    licences_allowed: frozenset[str] = field(default_factory=lambda: frozenset(DEFAULT_LICENCES))
    disabled: frozenset[str] = frozenset()

    def licence_allowed(self, spdx: str) -> bool:
        """Simple SPDX expressions: 'A OR B' needs one allowed licence, 'A AND B' needs all of them."""
        expr = spdx.strip().strip("()").strip()
        if " OR " in expr:
            return any(self.licence_allowed(part) for part in expr.split(" OR "))
        if " AND " in expr:
            return all(self.licence_allowed(part) for part in expr.split(" AND "))
        return expr in self.licences_allowed


def load_policy(path: Path | None = None) -> PluginPolicy:
    path = path or repo_root() / "configs" / "plugins.yaml"
    if not path.exists():
        return PluginPolicy()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return PluginPolicy(
        licences_allowed=frozenset(data.get("licences_allowed") or DEFAULT_LICENCES),
        disabled=frozenset(data.get("disabled") or ()),
    )
