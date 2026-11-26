"""pandera schemas for canonical tables (blueprint §4.4, §4.7)."""

from __future__ import annotations

import pandera.pandas as pa
from pandera.engines.geopandas_engine import Geometry

from resilink.domain.ids import RID_PATTERN

TIERS = ("public", "internal", "restricted")
CANONICAL_CRS = "EPSG:2154"

try:  # pandera >= 0.2x exposes a dedicated geopandas schema
    from pandera.api.geopandas.container import GeoDataFrameSchema as _GeoSchema
except ImportError:  # pragma: no cover - fallback for older pandera
    _GeoSchema = pa.DataFrameSchema  # type: ignore[misc,assignment]


def _core_columns() -> dict[str, pa.Column]:
    return {
        "rid": pa.Column(str, pa.Check.str_matches(RID_PATTERN), unique=True),
        "k": pa.Column("uint32", unique=True),
        "layer": pa.Column(str, pa.Check.str_matches(r"^[a-z][a-z0-9_]*$")),  # system id; packs are checked at compile
        "class": pa.Column(str),
        "confidence": pa.Column(float, pa.Check.in_range(0.0, 1.0)),
        "sensitivity": pa.Column(str, pa.Check.isin(TIERS)),
        "primary_source": pa.Column(str),
        "geometry": pa.Column(Geometry(crs=CANONICAL_CRS)),
    }


NODE_SCHEMA = _GeoSchema(
    {**_core_columns(), "is_source": pa.Column(bool), "pos_uncertainty_m": pa.Column(float, pa.Check.ge(0))},
    strict=False,
)

EDGE_SCHEMA = _GeoSchema(
    {
        **_core_columns(),
        "src_rid": pa.Column(str, pa.Check.str_matches(RID_PATTERN)),
        "dst_rid": pa.Column(str, pa.Check.str_matches(RID_PATTERN)),
        "length_m": pa.Column(float, pa.Check.ge(0)),
    },
    strict=False,
)

GRID_SCHEMA = _GeoSchema(
    {
        "cell_id": pa.Column(str, unique=True),
        "k": pa.Column("uint32", unique=True),
        "pop": pa.Column(float, pa.Check.ge(0)),
        "admin_commune": pa.Column(str),
        "geometry": pa.Column(Geometry(crs=CANONICAL_CRS)),
    },
    strict=False,
)

ADMIN_LEVELS = ("region", "departement", "commune")

ADMIN_SCHEMA = _GeoSchema(
    {
        "admin_id": pa.Column(str, pa.Check.str_matches(RID_PATTERN), unique=True),  # admin.<level>:cog<year>:<code>
        "level": pa.Column(str, pa.Check.isin(ADMIN_LEVELS)),
        "code": pa.Column(str),
        "name": pa.Column(str),
        "cog_year": pa.Column(int),
        "geometry": pa.Column(Geometry(crs=CANONICAL_CRS)),
    },
    strict=False,
)

EXTENT_SCHEMA = _GeoSchema(  # the territory itself (core) and the core grown by the territory buffer
    {
        "extent_id": pa.Column(str, unique=True),
        "kind": pa.Column(str, pa.Check.isin(["core", "buffer"]), unique=True),
        "buffer_m": pa.Column(float, pa.Check.ge(0)),
        "geometry": pa.Column(Geometry(crs=CANONICAL_CRS)),
    },
    strict=False,
)

TABLE_SCHEMAS = {"nodes": NODE_SCHEMA, "edges": EDGE_SCHEMA, "grid_cells": GRID_SCHEMA, "admin_areas": ADMIN_SCHEMA,
                 "territory_extent": EXTENT_SCHEMA}
