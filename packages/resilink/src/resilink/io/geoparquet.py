"""Canonical GeoParquet writer/reader (blueprint §4.3, decision D1).

Conventions: GeoParquet 1.1, WKB geometry, bbox covering column, zstd, Hilbert-sorted rows, EPSG:2154,
RESILINK key/value metadata carried through ``GeoDataFrame.attrs`` (round-tripped by GeoPandas >= 1.2).
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from resilink.domain.schemas import CANONICAL_CRS

GEOPARQUET_VERSION = "1.1.0"
ROW_GROUP_SIZE = 65_536


def hilbert_index(x: np.ndarray, y: np.ndarray, order: int = 16) -> np.ndarray:
    """Vectorised Hilbert curve index of points already scaled to integers in [0, 2**order)."""
    n = 1 << order
    x = x.astype(np.int64).copy()
    y = y.astype(np.int64).copy()
    d = np.zeros_like(x)
    s = n >> 1
    while s > 0:
        rx = ((x & s) > 0).astype(np.int64)
        ry = ((y & s) > 0).astype(np.int64)
        d += s * s * ((3 * rx) ^ ry)
        # rotate quadrant
        flip = (ry == 0) & (rx == 1)
        x = np.where(flip, n - 1 - x, x)
        y = np.where(flip, n - 1 - y, y)
        swap = ry == 0
        x, y = np.where(swap, y, x), np.where(swap, x, y)
        s >>= 1
    return d


def hilbert_order(gdf: gpd.GeoDataFrame, order: int = 16) -> np.ndarray:
    if len(gdf) == 0:
        return np.arange(0)
    b = gdf.geometry.bounds
    cx = ((b["minx"] + b["maxx"]) / 2.0).to_numpy()
    cy = ((b["miny"] + b["maxy"]) / 2.0).to_numpy()
    span_x = max(cx.max() - cx.min(), 1e-9)
    span_y = max(cy.max() - cy.min(), 1e-9)
    scale = (1 << order) - 1
    ix = np.floor((cx - cx.min()) / span_x * scale)
    iy = np.floor((cy - cy.min()) / span_y * scale)
    key = hilbert_index(ix, iy, order)
    # tie-break deterministically on the original position
    return np.lexsort((np.arange(len(gdf)), key))


def write_canonical(
    gdf: gpd.GeoDataFrame,
    path: str | Path,
    *,
    table: str,
    release_id: str,
    schema_version: str,
    sort: bool = True,
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if gdf.crs is None:
        raise ValueError(f"{table}: GeoDataFrame has no CRS")
    if gdf.crs.to_string() != CANONICAL_CRS:
        gdf = gdf.to_crs(CANONICAL_CRS)
    if sort and len(gdf):
        gdf = gdf.iloc[hilbert_order(gdf)]
    gdf = gdf.reset_index(drop=True)
    gdf.attrs = {
        "resilink:schema_version": schema_version,
        "resilink:release_id": release_id,
        "resilink:table": table,
    }
    gdf.to_parquet(
        path,
        schema_version=GEOPARQUET_VERSION,
        write_covering_bbox=True,
        compression="zstd",
        index=False,
        row_group_size=ROW_GROUP_SIZE,
    )
    return path


def write_table(df: pd.DataFrame, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.reset_index(drop=True).to_parquet(path, compression="zstd", index=False)
    return path


def read_canonical(path: str | Path, columns: list[str] | None = None) -> gpd.GeoDataFrame:
    return gpd.read_parquet(path, columns=columns)


def read_table(path: str | Path, columns: list[str] | None = None) -> pd.DataFrame:
    return pd.read_parquet(path, columns=columns)
