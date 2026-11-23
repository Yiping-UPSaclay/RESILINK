"""Source register models and connector contract."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from resilink.domain.config import TerritoryConfig
from resilink.io.paths import load_yaml


class SourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    provider: str
    connector: Literal["opendatasoft", "datafair"]
    base_url: str
    dataset: str
    format: Literal["parquet", "geojson", "csv"] = "geojson"
    territory_field: str | None = Field(None, description="Field used to filter by département")
    territory_value: Literal["code", "name"] = Field("code", description="Filter on département code or name")
    role: str = ""
    licence: str = "Licence Ouverte 2.0"
    sensitivity: Literal["public", "internal", "restricted"] = "internal"
    refresh: str = "manual"
    id_strategy: str = "fingerprint"
    priority: Literal["P0", "P1", "P2"] = "P0"
    notes: str = ""


class FieldInfo(BaseModel):
    name: str
    type: str


class SourceMetadata(BaseModel):
    title: str
    modified: str | None = None
    licence: str | None = None
    count: int | None = None
    fields: list[FieldInfo]
    has_geometry: bool
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)


class RawSnapshot(BaseModel):
    snapshot_id: str  # <source>@<UTC ts>#<sha256[:12]>
    source_id: str
    path: str
    sha256: str
    url: str
    retrieved_at: datetime
    rows: int | None = None
    metadata: SourceMetadata


class Connector(Protocol):
    def describe(self, spec: SourceSpec) -> SourceMetadata: ...

    def fetch(self, spec: SourceSpec, territory: TerritoryConfig | None, dest: Path, limit: int | None = None) -> tuple[str, int | None]:
        """Stream the (optionally territory-filtered) export to ``dest``; return (url, rows if known)."""
        ...


def load_sources(path: str = "configs/sources.yaml") -> dict[str, SourceSpec]:
    raw = load_yaml(path)
    return {s["id"]: SourceSpec.model_validate(s) for s in raw["sources"]}
