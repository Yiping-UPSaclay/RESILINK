"""Staging: adapters turn raw snapshots into canonical tables for one territory (blueprint §5.1, step "normalize").

An adapter reads the latest raw snapshot of each source it names, may read tables staged by earlier adapters, and
returns canonical tables with QA figures. Staged tables are written under ``data/staging/<territory>/`` as canonical
GeoParquet next to a JSON record of their inputs (snapshot ids, adapter version, licences, QA). The same snapshots
give byte-identical tables. A release (D6) is assembled from staged tables; releases stay the immutable unit, so
staging may be rewritten.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import geopandas as gpd
import pandas as pd
from pydantic import BaseModel, Field

from resilink.domain.config import TerritoryConfig
from resilink.domain.schemas import TABLE_SCHEMAS
from resilink.etl.contracts import RawSnapshot, SourceSpec, load_sources
from resilink.etl.snapshot import latest_snapshot
from resilink.io import geoparquet as gpq
from resilink.io.hashing import canonical_json, sha256_file
from resilink.io.paths import data_root

Tier = Literal["public", "internal", "restricted"]


class StagedTable(BaseModel):
    table: str
    territory: str
    path: str = Field(description="File name inside the territory's staging directory")
    rows: int
    sha256: str
    sensitivity: Tier
    adapter: str = Field(description="<adapter id>@<version>")
    snapshots: list[str] = Field(description="Raw snapshot ids the table was built from")
    licences: dict[str, str] = Field(default_factory=dict, description="Source id -> licence")
    qa: dict[str, Any] = Field(default_factory=dict)
    staged_at: datetime


@dataclass(frozen=True)
class Output:
    frame: gpd.GeoDataFrame | pd.DataFrame
    sensitivity: Tier
    qa: dict[str, Any] = field(default_factory=dict)


@dataclass
class AdapterContext:
    territory: TerritoryConfig
    snapshots: dict[str, RawSnapshot]
    sources: dict[str, SourceSpec]
    staging: Path

    def path(self, source_id: str) -> Path:
        return Path(self.snapshots[source_id].path)

    def read(self, table: str) -> gpd.GeoDataFrame:
        """A table staged earlier for this territory (adapters declare it in ``needs``)."""
        p = self.staging / f"{table}.parquet"
        if not p.exists():
            raise FileNotFoundError(f"staged table {table!r} missing for {self.territory.id}: stage its adapter first")
        return gpq.read_canonical(p)


@dataclass(frozen=True)
class Adapter:
    id: str
    version: str
    sources: tuple[str, ...]
    produces: tuple[str, ...]
    run: Callable[[AdapterContext], dict[str, Output]]
    needs: tuple[str, ...] = ()
    description: str = ""


ADAPTERS: dict[str, Adapter] = {}


def adapter(id: str, *, version: str, sources: tuple[str, ...], produces: tuple[str, ...],
            needs: tuple[str, ...] = ()) -> Callable[[Callable[[AdapterContext], dict[str, Output]]], Adapter]:
    def register(fn: Callable[[AdapterContext], dict[str, Output]]) -> Adapter:
        a = Adapter(id=id, version=version, sources=sources, produces=produces, run=fn, needs=needs,
                    description=(fn.__doc__ or "").strip().split("\n\n")[0])
        ADAPTERS[id] = a
        return a
    return register


def staging_dir(territory_id: str, root: Path | None = None) -> Path:
    return (root or data_root()) / "staging" / territory_id


def ordered(adapters: dict[str, Adapter] | None = None) -> list[Adapter]:
    """Adapters in dependency order (an adapter runs after the ones producing what it needs)."""
    pending = dict(adapters or ADAPTERS)
    done: set[str] = set()
    out: list[Adapter] = []
    while pending:
        ready = sorted((a for a in pending.values() if set(a.needs) <= done), key=lambda a: a.id)
        if not ready:
            raise ValueError(f"adapters with unmet needs: {sorted(pending)}")
        for a in ready:
            out.append(a)
            done |= set(a.produces)
            del pending[a.id]
    return out


def stage(adapter_id: str, territory: TerritoryConfig, *, root: Path | None = None) -> list[StagedTable]:
    """Run one adapter on the latest snapshots of its sources and write its tables to staging."""
    a = ADAPTERS[adapter_id]
    sources = load_sources()
    snaps: dict[str, RawSnapshot] = {}
    for sid in a.sources:
        snap = latest_snapshot(sid, root)
        if snap is None:
            raise FileNotFoundError(f"no raw snapshot of {sid}: run `resilink etl fetch {sid}` first")
        snaps[sid] = snap
    out_dir = staging_dir(territory.id, root)
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = a.run(AdapterContext(territory=territory, snapshots=snaps, sources=sources, staging=out_dir))
    if set(outputs) != set(a.produces):
        raise ValueError(f"adapter {a.id} returned {sorted(outputs)}, declared {sorted(a.produces)}")
    records: list[StagedTable] = []
    for table, out in sorted(outputs.items()):
        frame = out.frame
        schema = TABLE_SCHEMAS.get(table)
        if schema is not None:
            schema.validate(frame)
        path = out_dir / f"{table}.parquet"
        if isinstance(frame, gpd.GeoDataFrame):
            gpq.write_canonical(frame, path, table=table, release_id=f"staging:{territory.id}", schema_version="1.0.0")
        else:
            gpq.write_table(frame, path)
        rec = StagedTable(table=table, territory=territory.id, path=path.name, rows=len(frame),
                          sha256=sha256_file(path), sensitivity=out.sensitivity, adapter=f"{a.id}@{a.version}",
                          snapshots=[snaps[s].snapshot_id for s in a.sources],
                          licences={s: sources[s].licence for s in a.sources}, qa=out.qa,
                          staged_at=datetime.now(UTC))
        (out_dir / f"{table}.json").write_text(canonical_json(rec), encoding="utf-8")
        records.append(rec)
    return records


def list_staged(territory_id: str, root: Path | None = None) -> list[StagedTable]:
    d = staging_dir(territory_id, root)
    return [StagedTable.model_validate_json(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.json"))]
