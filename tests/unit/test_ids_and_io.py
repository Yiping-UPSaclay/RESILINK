from __future__ import annotations

import json

import geopandas as gpd
import numpy as np
import pyarrow.parquet as pq
import pytest
from shapely.geometry import Point

from resilink.domain.ids import (
    InvalidRid,
    is_rid,
    line_fingerprint,
    make_rid,
    parse_rid,
    point_fingerprint,
    tool_name,
)
from resilink.io.geoparquet import hilbert_index, read_canonical, write_canonical


def test_rid_grammar_roundtrip():
    rid = make_rid("elec", "sub_tx", "rte", "VERDO")
    assert rid == "elec.sub_tx:rte:VERDO"
    assert parse_rid(rid) == ("elec", "sub_tx", "rte", "VERDO")
    assert is_rid("elec.sub_mv_lv:fp:p895123_6245678")
    with pytest.raises(InvalidRid):
        make_rid("Elec", "sub tx", "rte", "x")


def test_fingerprints_are_stable_to_sub_metre_jitter():
    assert point_fingerprint(895123.2, 6245678.4) == point_fingerprint(895123.4, 6245678.1)
    a = line_fingerprint([(0, 0), (100, 0)], "cable_mv", 100.0)
    b = line_fingerprint([(100.2, 0.1), (0.1, -0.2)], "cable_mv", 100.3)  # reversed + jitter
    assert a == b


def test_tool_names_have_no_dots():
    assert tool_name("network.dependencies_of") == "network__dependencies_of"


def test_hilbert_index_is_a_bijection_on_small_grid():
    xs, ys = np.meshgrid(np.arange(4), np.arange(4), indexing="ij")
    d = hilbert_index(xs.ravel(), ys.ravel(), order=2)
    assert sorted(d.tolist()) == list(range(16))


def test_geoparquet_11_with_covering_bbox_and_attrs(tmp_path):
    gdf = gpd.GeoDataFrame({"rid": ["a", "b"], "v": [1.0, 2.0]},
                           geometry=[Point(890000, 6240000), Point(890100, 6240100)], crs="EPSG:2154")
    p = write_canonical(gdf, tmp_path / "t.parquet", table="t", release_id="x-2026.11.0", schema_version="1.0.0")
    geo = json.loads(pq.read_metadata(p).metadata[b"geo"])
    assert geo["version"] == "1.1.0"
    assert "covering" in geo["columns"]["geometry"]
    back = read_canonical(p)
    assert back.crs.to_epsg() == 2154
    assert back.attrs["resilink:release_id"] == "x-2026.11.0"
    assert set(back["rid"]) == {"a", "b"}
