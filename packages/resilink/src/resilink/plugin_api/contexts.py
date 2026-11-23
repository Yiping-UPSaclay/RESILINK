"""What the engine hands to models (blueprint §6.5). Typing contracts only; the engine provides implementations."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd


class ReleaseView(Protocol):
    """Read-only access to the pinned release."""

    release_id: str

    def read(self, table: str, columns: Sequence[str] | None = None) -> pd.DataFrame: ...


class NetworkView(Protocol):
    """Integer-indexed, geometry-free network (invariant R3). Arrays are read-only for models."""

    rid: np.ndarray
    layer: np.ndarray
    cls: np.ndarray
    is_source: np.ndarray
    edge_rid: np.ndarray
    edge_src: np.ndarray
    edge_dst: np.ndarray
    edge_cls: np.ndarray

    @property
    def n(self) -> int: ...

    @property
    def e(self) -> int: ...


class SetupContext(Protocol):
    """Once per campaign: heavy precomputation, cached by content hash."""

    release: ReleaseView
    net: NetworkView

    def resolve(self, ref: str) -> Path: ...  # knowledge/config files; their hashes enter the run_id


class SimContext(Protocol):
    """Once per simulated world."""

    net: NetworkView
    t: float  # hours in an episode, years in a lifecycle
    actor: str | None

    def rng(self, stream: str) -> np.random.Generator: ...

    def state(self, field: str) -> np.ndarray: ...  # declared fields only

    def schedule(self, t: float, event: str, **payload: Any) -> None: ...  # declared events only

    def service(self, subsystem: str, up_nodes: np.ndarray, up_edges: np.ndarray) -> Any: ...

    def emit(self, table: str, **columns: np.ndarray) -> None: ...  # declared tables only
