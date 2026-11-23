# ADR-0002 — GeoParquet 1.1 releases + DuckDB 2.0 LTS
Status: accepted (2026-11-16)

**Decision.** Canonical releases are immutable GeoParquet **1.1** files (WKB, bbox covering, zstd,
Hilbert-sorted, EPSG:2154) with a `release.json` manifest of SHA-256 hashes; DuckDB 2.0.x (spatial, fts,
vss) is the query engine over views of those files. GeoParquet 2.0 (native Parquet GEOMETRY) is adopted
only when GeoPandas, DuckDB and the team's GDAL/QGIS all pass an interoperability test (GDAL writes 2.0
from 3.14). P0 writes one file per table; hive partitioning by layer is introduced with real volumes (P1).
**Consequences.** Serverless, reproducible, portable; PostGIS only when a §3.4 trigger fires.
