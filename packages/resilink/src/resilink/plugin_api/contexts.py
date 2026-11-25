"""What the engine hands to models (blueprint §6.5). Typing contracts only; the engine provides implementations."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd


class ReleaseView(Protocol):
    """Read-only access to the pinned release."""

    release_id: str

    def read(self, table: str) -> pd.DataFrame: ...


class NetworkView(Protocol):
    """Integer-indexed, geometry-free network (invariant R3). Arrays are read-only for models."""

    rid: np.ndarray
    layer: np.ndarray  # system code per node (position in layer_names)
    cls: np.ndarray
    is_source: np.ndarray
    xy: np.ndarray  # only for dependency inference and explanations, never for the cascade
    repair_median_h: np.ndarray
    edge_rid: np.ndarray
    edge_src: np.ndarray
    edge_dst: np.ndarray
    edge_cls: np.ndarray
    edge_repair_median_h: np.ndarray
    cell_id: np.ndarray
    cell_pop: np.ndarray
    supply: dict[str, Any]  # sector -> sparse (cells x nodes) supply shares
    layer_has_source: np.ndarray
    layer_names: tuple[str, ...]
    index: dict[str, int]
    edge_index: dict[str, int]

    @property
    def n(self) -> int: ...

    @property
    def e(self) -> int: ...

    @property
    def sectors(self) -> tuple[str, ...]: ...

    def layer_code(self, system: str) -> int: ...

    def full_adjacency(self) -> Any: ...


class SetupContext(Protocol):
    """Once per campaign: heavy precomputation, cached by content hash."""

    release: ReleaseView
    net: NetworkView
    systems: tuple[str, ...]  # ids of the installed system packs

    def resolve(self, ref: str) -> Path: ...  # knowledge/config files; their hashes enter the run_id

    def service_model(self, subsystem: str | None = None) -> Any: ...  # the configured service model


class SimContext(Protocol):
    """Once per simulated world (event kernel, step E4)."""

    net: NetworkView
    t: float  # hours in an episode, years in a lifecycle
    actor: str | None

    def rng(self, stream: str) -> np.random.Generator: ...

    def state(self, field: str) -> np.ndarray: ...  # declared fields only

    def schedule(self, t: float, event: str, **payload: Any) -> None: ...  # declared events only

    def service(self, subsystem: str, up_nodes: np.ndarray, up_edges: np.ndarray) -> Any: ...

    def emit(self, table: str, **columns: np.ndarray) -> None: ...  # declared tables only
