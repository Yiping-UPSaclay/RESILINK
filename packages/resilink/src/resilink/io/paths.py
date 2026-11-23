"""Repository / data root discovery and YAML config loading."""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from resilink.domain.config import ExperimentConfig, TerritoryConfig


@lru_cache(maxsize=1)
def repo_root() -> Path:
    """RESILINK_ROOT, else the nearest ancestor containing configs/ and packages/."""
    env = os.environ.get("RESILINK_ROOT")
    if env:
        return Path(env).resolve()
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents, *Path(__file__).resolve().parents):
        if (candidate / "configs").is_dir() and (candidate / "packages").is_dir():
            return candidate
    return here


def data_root() -> Path:
    env = os.environ.get("RESILINK_DATA")
    root = Path(env).resolve() if env else repo_root() / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else repo_root() / p


def load_yaml(path: str | Path) -> Any:
    with resolve(path).open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_experiment(path: str | Path) -> ExperimentConfig:
    return ExperimentConfig.model_validate(load_yaml(path))


def load_territory(path: str | Path) -> TerritoryConfig:
    return TerritoryConfig.model_validate(load_yaml(path))


def git_state() -> dict[str, Any]:
    """Code provenance for manifests: git SHA and dirty flag (best effort, never raises)."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root(), capture_output=True, text=True, timeout=10
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"], cwd=repo_root(), capture_output=True, text=True, timeout=10
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError):
        sha, dirty = "", True
    return {"git_sha": sha or "uncommitted", "dirty": dirty}
