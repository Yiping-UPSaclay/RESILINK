"""Immutable raw snapshots + schema-drift gate (blueprint §5.6, §5.8).

Layout: ``data/raw/<source_id>/<UTCts>-<sha12>/{data.<ext>, snapshot.json}``. A removed field, a removed
geometry or a licence change fails the fetch (DriftError) unless explicitly accepted — the RTE geometry
withdrawal of June 2026 is the reference case. Raw snapshots are then tracked with ``dvc add``.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from resilink.domain.config import TerritoryConfig
from resilink.etl.connectors import CONNECTORS
from resilink.etl.contracts import RawSnapshot, SourceMetadata, SourceSpec
from resilink.io.hashing import canonical_json, sha256_file
from resilink.io.paths import data_root


class DriftReport(BaseModel):
    source_id: str
    removed_fields: list[str] = Field(default_factory=list)
    added_fields: list[str] = Field(default_factory=list)
    type_changes: dict[str, tuple[str, str]] = Field(default_factory=dict)
    geometry_removed: bool = False
    licence_changed: tuple[str | None, str | None] | None = None
    previous_modified: str | None = None
    current_modified: str | None = None

    @property
    def breaking(self) -> bool:
        return bool(self.removed_fields or self.type_changes or self.geometry_removed or self.licence_changed)


class DriftError(RuntimeError):
    def __init__(self, report: DriftReport) -> None:
        super().__init__(f"schema drift on {report.source_id}: {report.model_dump(exclude_none=True)}")
        self.report = report


def diff(source_id: str, prev: SourceMetadata, cur: SourceMetadata) -> DriftReport:
    p = {f.name: f.type for f in prev.fields}
    c = {f.name: f.type for f in cur.fields}
    return DriftReport(
        source_id=source_id,
        removed_fields=sorted(set(p) - set(c)),
        added_fields=sorted(set(c) - set(p)),
        type_changes={k: (p[k], c[k]) for k in sorted(set(p) & set(c)) if p[k] != c[k]},
        geometry_removed=prev.has_geometry and not cur.has_geometry,
        licence_changed=(prev.licence, cur.licence) if (prev.licence or "") != (cur.licence or "") else None,
        previous_modified=prev.modified, current_modified=cur.modified,
    )


def extent_window(territory: TerritoryConfig, root: Path | None = None) -> tuple[float, float, float, float]:
    """WGS84 bounds (lon_min, lat_min, lon_max, lat_max) of the territory's staged extent (core + buffer)."""
    import geopandas as gpd

    path = (root or data_root()) / "staging" / territory.id / "territory_extent.parquet"
    if not path.exists():
        raise FileNotFoundError(f"no staged extent for {territory.id}: run `resilink etl stage ign.admin_express`")
    lon0, lat0, lon1, lat1 = gpd.read_parquet(path).to_crs("EPSG:4326").total_bounds
    return round(float(lon0), 5), round(float(lat0), 5), round(float(lon1), 5), round(float(lat1), 5)


def raw_dir(source_id: str, root: Path | None = None) -> Path:
    return (root or data_root()) / "raw" / source_id


def latest_snapshot(source_id: str, root: Path | None = None) -> RawSnapshot | None:
    snaps = sorted(raw_dir(source_id, root).glob("*/snapshot.json"))
    return RawSnapshot.model_validate_json(snaps[-1].read_text(encoding="utf-8")) if snaps else None


def fetch_snapshot(spec: SourceSpec, territory: TerritoryConfig | None = None, *, root: Path | None = None,
                   limit: int | None = None, accept_drift: bool = False) -> tuple[RawSnapshot, DriftReport | None]:
    conn = CONNECTORS[spec.connector]
    meta = conn.describe(spec)
    prev = latest_snapshot(spec.id, root)

    def gate(current: SourceMetadata) -> DriftReport | None:
        rep = diff(spec.id, prev.metadata, current) if prev else None
        if rep is not None and rep.breaking and not accept_drift:
            raise DriftError(rep)
        return rep

    inspect = getattr(conn, "inspect", None)
    report = gate(meta) if inspect is None else None  # portals publish their schema: fail before downloading
    base = raw_dir(spec.id, root)
    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".fetch-", dir=base))
    try:
        ext = "parquet" if spec.format == "parquet" and spec.connector == "opendatasoft" else (
            "geojson" if spec.format == "geojson" or spec.connector == "datafair" else spec.format)
        data_path = tmp / f"data.{ext}"
        extra = {}
        if spec.territory_mode == "extent_bbox":
            if territory is None:
                raise ValueError(f"{spec.id}: extent_bbox needs a territory")
            extra["window"] = extent_window(territory, root)
        url, rows = conn.fetch(spec, territory, data_path, limit=limit, **extra)
        if inspect is not None:  # static files: the schema is read from the download, gated before publishing
            meta = inspect(spec, data_path, meta)
            report = gate(meta)
            rows = rows if rows is not None else meta.count
        sha = sha256_file(data_path)
        now = datetime.now(UTC)
        snap_id = f"{spec.id}@{now:%Y%m%dT%H%M%SZ}#{sha[:12]}"
        final = base / f"{now:%Y%m%dT%H%M%S%fZ}-{sha[:12]}"
        n = 1
        while final.exists():  # coarse clocks (Windows ~15 ms) can repeat timestamps; snapshots stay immutable
            final = base / f"{now:%Y%m%dT%H%M%S%fZ}-{sha[:12]}-{n}"
            n += 1
        snap = RawSnapshot(snapshot_id=snap_id, source_id=spec.id, path=str(final / data_path.name), sha256=sha,
                           url=url, retrieved_at=now, rows=rows, metadata=meta)
        (tmp / "snapshot.json").write_text(canonical_json(snap), encoding="utf-8")
        tmp.rename(final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return snap, report
