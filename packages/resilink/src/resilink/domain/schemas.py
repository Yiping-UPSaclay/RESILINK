"""pandera schemas for canonical tables (blueprint §4.4, §4.7)."""

from __future__ import annotations

import pandera.pandas as pa
from pandera.engines.geopandas_engine import Geometry

from resilink.domain.ids import RID_PATTERN
from resilink.domain.vocab import LAYERS

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
        "layer": pa.Column(str, pa.Check.isin(LAYERS)),
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
