"""The assumption register: ids are unique, and every id cited by code, system packs or dependency rules exists."""

from __future__ import annotations

import re
from collections import Counter

from resilink.io.paths import load_yaml, repo_root

CITED = re.compile(r"\bA-[A-Z]+-\d{2}\b")


def _register() -> list[str]:
    return [a["id"] for a in load_yaml("knowledge/assumptions.yaml")["assumptions"]]


def test_assumption_ids_are_unique():
    repeated = {i: n for i, n in Counter(_register()).items() if n > 1}
    assert not repeated, f"assumption ids defined more than once: {repeated}"


def test_assumptions_cited_by_code_and_rules_are_registered():
    root = repo_root()
    files = [*root.glob("packages/*/src/**/*.py"), *root.glob("packages/*/src/**/*.yaml"),
             root / "knowledge" / "dependency_rules.yaml"]
    cited = {(m, f.relative_to(root).as_posix()) for f in files for m in CITED.findall(f.read_text(encoding="utf-8"))}
    missing = sorted((i, f) for i, f in cited if i not in set(_register()))
    assert not missing, f"cited but not in knowledge/assumptions.yaml: {missing}"
