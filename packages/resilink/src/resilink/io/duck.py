"""DuckDB sessions over immutable release files (views, no copies)."""

from __future__ import annotations

import duckdb

from resilink.io.release import Release

_SPATIAL_READY: set[int] = set()


def load_extension(con: duckdb.DuckDBPyConnection, name: str) -> None:
    try:
        con.execute(f"LOAD {name}")
    except duckdb.Error:
        con.execute(f"INSTALL {name}")
        con.execute(f"LOAD {name}")


def _q(path: str) -> str:
    return "'" + path.replace("\\", "/").replace("'", "''") + "'"


def connect(release: Release | None = None, *, spatial: bool = True) -> duckdb.DuckDBPyConnection:
    """In-memory catalog with one view per release table; data stays in the immutable Parquet files."""
    con = duckdb.connect()
    if spatial:
        load_extension(con, "spatial")
    if release is not None:
        for name in release.manifest.tables:
            con.execute(f'CREATE VIEW "{name}" AS SELECT * FROM read_parquet({_q(str(release.path(name)))})')
    return con
