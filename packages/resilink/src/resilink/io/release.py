"""Immutable, content-hashed releases (rule R1, blueprint §4.3, §4.6)."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import geopandas as gpd
import pandas as pd
from pydantic import BaseModel, Field

from resilink import __version__
from resilink.io import geoparquet as gpq
from resilink.io.hashing import canonical_json, sha256_file
from resilink.io.paths import data_root, git_state

SCHEMA_VERSION = "1.0.0"


class TableEntry(BaseModel):
    path: str
    rows: int
    sha256: str
    sensitivity: Literal["public", "internal", "restricted"]
    kind: Literal["geo", "table"]


class ReleaseManifest(BaseModel):
    release_id: str
    schema_version: str = SCHEMA_VERSION
    territory: dict[str, Any] = Field(default_factory=dict)
    code: dict[str, Any] = Field(default_factory=dict)
    snapshots: list[dict[str, Any]] = Field(default_factory=list)
    tables: dict[str, TableEntry] = Field(default_factory=dict)
    qa: dict[str, Any] = Field(default_factory=dict)
    provenance_ids: list[str] = Field(default_factory=list)
    licences: dict[str, str] = Field(default_factory=dict)

    def content_hash(self) -> str:
        """Hash of table contents only (independent of code SHA / timestamps): the reproducibility check."""
        from resilink.io.hashing import stable_hash

        return stable_hash({k: v.sha256 for k, v in sorted(self.tables.items())}, n=64)


def split_release_id(release_id: str) -> tuple[str, str]:
    territory, _, version = release_id.partition("-")
    if not territory or not version:
        raise ValueError(f"release id must look like '<territory>-<YYYY.MM.patch>': {release_id!r}")
    return territory, version


def release_dir(release_id: str, root: Path | None = None) -> Path:
    territory, version = split_release_id(release_id)
    return (root or data_root()) / "releases" / territory / version


@dataclass
class Release:
    root: Path
    manifest: ReleaseManifest
    _cache: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def release_id(self) -> str:
        return self.manifest.release_id

    def path(self, table: str) -> Path:
        return self.root / self.manifest.tables[table].path

    def has(self, table: str) -> bool:
        return table in self.manifest.tables

    def read(self, table: str) -> gpd.GeoDataFrame | pd.DataFrame:
        if table not in self._cache:
            entry = self.manifest.tables[table]
            reader = gpq.read_canonical if entry.kind == "geo" else gpq.read_table
            self._cache[table] = reader(self.root / entry.path)
        return self._cache[table].copy()


def publish_release(
    release_id: str,
    tables: dict[str, tuple[gpd.GeoDataFrame | pd.DataFrame, str]],
    *,
    territory: dict[str, Any],
    snapshots: list[dict[str, Any]] | None = None,
    qa: dict[str, Any] | None = None,
    provenance_ids: list[str] | None = None,
    licences: dict[str, str] | None = None,
    root: Path | None = None,
    overwrite: bool = False,
) -> Release:
    """Write all tables + manifest into a temp dir, then move into place atomically. Releases are immutable."""
    final = release_dir(release_id, root)
    if final.exists():
        if not overwrite:
            raise FileExistsError(f"release {release_id} already exists at {final} (releases are immutable)")
        shutil.rmtree(final)
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{final.name}-", dir=final.parent))
    entries: dict[str, TableEntry] = {}
    try:
        for name, (frame, sensitivity) in sorted(tables.items()):
            rel = f"{name}.parquet"
            if isinstance(frame, gpd.GeoDataFrame):
                gpq.write_canonical(
                    frame, tmp / rel, table=name, release_id=release_id, schema_version=SCHEMA_VERSION
                )
                kind: Literal["geo", "table"] = "geo"
            else:
                gpq.write_table(frame, tmp / rel)
                kind = "table"
            entries[name] = TableEntry(
                path=rel, rows=len(frame), sha256=sha256_file(tmp / rel), sensitivity=sensitivity, kind=kind  # type: ignore[arg-type]
            )
        manifest = ReleaseManifest(
            release_id=release_id,
            territory=territory,
            code={"resilink": __version__, **git_state()},
            snapshots=snapshots or [],
            tables=entries,
            qa=qa or {},
            provenance_ids=provenance_ids or [],
            licences=licences or {},
        )
        (tmp / "release.json").write_text(canonical_json(manifest), encoding="utf-8")
        tmp.rename(final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return Release(root=final, manifest=manifest)


def load_release(release_id: str, root: Path | None = None) -> Release:
    d = release_dir(release_id, root)
    manifest_path = d / "release.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"release {release_id} not found at {d}")
    manifest = ReleaseManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    return Release(root=d, manifest=manifest)


def list_releases(root: Path | None = None) -> list[ReleaseManifest]:
    base = (root or data_root()) / "releases"
    out: list[ReleaseManifest] = []
    for manifest_path in sorted(base.glob("*/*/release.json")):
        out.append(ReleaseManifest.model_validate_json(manifest_path.read_text(encoding="utf-8")))
    return out
