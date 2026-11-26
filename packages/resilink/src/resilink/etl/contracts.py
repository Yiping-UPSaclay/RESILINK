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
    connector: Literal["opendatasoft", "datafair", "wfs", "http_file"]
    base_url: str
    dataset: str = Field(description="Dataset id (portals), feature type (WFS) or file path under base_url (http_file)")
    format: Literal["parquet", "geojson", "csv", "zip"] = "geojson"
    territory_field: str | None = Field(None, description="Field used to filter by département")
    territory_value: Literal["code", "name"] = Field("code", description="Filter on département code or name")
    territory_mode: Literal["field", "buffer_bbox", "none"] = Field(
        "field", description="field: filter on territory_field; buffer_bbox (WFS): features in the bounding box of the "
        "territory's départements (from territory_layer) grown by the territory buffer; none: whole file")
    territory_layer: str | None = Field(None, description="WFS feature type holding the territory's départements")
    members: list[str] = Field(default_factory=list, description="http_file archives: members to inspect and use")
    vintage: str | None = Field(None, description="Publisher edition pinned by this entry (e.g. COG 2026)")
    docs_url: str | None = Field(None, description="Publisher page documenting the dataset")
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


class Inspector(Protocol):
    """Optional connector method for sources whose schema is only known after download (static files).

    The drift gate then compares the inspected metadata before the snapshot is published."""

    def inspect(self, spec: SourceSpec, path: Path, meta: SourceMetadata) -> SourceMetadata: ...


def load_sources(path: str = "configs/sources.yaml") -> dict[str, SourceSpec]:
    raw = load_yaml(path)
    return {s["id"]: SourceSpec.model_validate(s) for s in raw["sources"]}
