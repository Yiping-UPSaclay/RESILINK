"""Line networks from loose segments (D6): T-junctions, crossings, snapping, anchors, boundary cuts, contraction."""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString, box

from resilink.etl.topology import build_network


def lines(*coords, **cols) -> gpd.GeoDataFrame:
    n = len(coords)
    data = {k: list(v) if isinstance(v, list | tuple) else [v] * n for k, v in cols.items()}
    return gpd.GeoDataFrame({**data, "geometry": [LineString(c) for c in coords]}, crs="EPSG:2154")


def anchors(*rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["rid", "x", "y", "radius_m"])


def edge_set(net) -> set[tuple[tuple[float, float], tuple[float, float]]]:
    xy = net.vertices.set_index("vid")[["x", "y"]].round(1)
    return {tuple(sorted((tuple(xy.loc[u]), tuple(xy.loc[v])))) for u, v in zip(net.edges["u"], net.edges["v"],
                                                                                 strict=True)}


def test_an_end_on_another_line_splits_it_but_a_crossing_does_not_connect():
    # the junction is the mean of the three snapped ends: (50, 0.13)
    net = build_network(lines([(0, 0), (100, 0)], [(50, 0.4), (50, 50)], [(20, -30), (20, 30)]), snap_m=1, split_m=1)
    assert net.stats["segments_split"] == 1
    assert edge_set(net) == {((0.0, 0.0), (50.0, 0.1)), ((50.0, 0.1), (100.0, 0.0)), ((50.0, 0.1), (50.0, 50.0)),
                             ((20.0, -30.0), (20.0, 30.0))}  # the crossing line stays apart
    assert sorted(net.vertices["degree"]) == [1, 1, 1, 1, 1, 3]


def test_ends_snap_and_plain_chains_contract_with_length_weighted_attributes():
    net = build_network(lines([(0, 0), (100, 0)], [(100.6, 0), (300, 0)], aerial=[1.0, 0.0]),
                        snap_m=1, split_m=0.5, weighted=("aerial",))
    assert len(net.edges) == 1
    e = net.edges.iloc[0]
    assert e["n_segments"] == 2
    assert e["length_m"] == pytest.approx(100 + 199.4)
    assert e["aerial"] == pytest.approx(100 / 299.4)
    assert e.geometry.coords[0] == (0, 0) and e.geometry.coords[-1] == (300, 0)


def test_segments_of_different_kinds_keep_the_vertex_between_them():
    net = build_network(lines([(0, 0), (100, 0)], [(100, 0), (200, 0)], cls=["aerial", "cable"]),
                        snap_m=1, split_m=0.5, same=("cls",))
    assert len(net.edges) == 2 and set(net.edges["cls"]) == {"aerial", "cable"}


def test_ends_near_a_substation_attach_to_it_and_tiny_loops_inside_it_are_dropped():
    segs = lines([(0, 0), (100, 0)], [(101.5, 0), (200, 0)], [(99, 1), (101, 1)])
    net = build_network(segs, anchors(("elec.sub_mv_lv:fp:p100_2", 100, 1.5, 5.0)), snap_m=1, split_m=0.5)
    assert net.stats["self_loops_dropped"] == 1
    sub = net.vertices[net.vertices["anchor"].notna()]
    assert list(sub["anchor"]) == ["elec.sub_mv_lv:fp:p100_2"] and int(sub["degree"].iat[0]) == 2
    assert (sub["x"].iat[0], sub["y"].iat[0]) == (100, 1.5)  # the anchor keeps its own position


def test_an_anchor_on_a_segment_interior_cuts_it_unless_told_not_to():
    segs = lines([(0, 0), (100, 0)])
    net = build_network(segs, anchors(("gas.delivery_station:osm:n1", 50, 3, 10.0)), snap_m=1, split_m=0.5)
    assert len(net.edges) == 2 and int(net.vertices["anchor"].notna().sum()) == 1
    no_cut = anchors(("gas.delivery_station:osm:n1", 50, 3, 10.0)).assign(split=False)
    assert len(build_network(segs, no_cut, snap_m=1, split_m=0.5).edges) == 1


def test_the_nearest_covering_anchor_wins_and_one_asset_may_have_several_footprints():
    segs = lines([(0, 0), (10, 0)], [(300, 0), (400, 0)])
    a = anchors(("elec.sub_primary:rte:P1", 120, 0, 150.0), ("elec.sub_primary:rte:P1", 250, 0, 60.0),
                ("elec.sub_mv_lv:fp:p12_0", 12, 0, 5.0))
    net = build_network(segs, a, snap_m=1, split_m=0.5)
    attached = net.vertices.dropna(subset=["anchor"]).set_index("vid")["anchor"].to_dict()
    ends = {attached.get(u) for u in net.edges["u"]} | {attached.get(v) for v in net.edges["v"]}
    assert ends == {"elec.sub_mv_lv:fp:p12_0", "elec.sub_primary:rte:P1", None}  # (10,0) -> MV/LV, (300,0) -> P1


def test_lines_are_cut_at_the_boundary_and_the_cut_points_flagged():
    net = build_network(lines([(0, 0), (200, 0)]), snap_m=1, split_m=0.5, boundary=box(-10, -10, 100, 10))
    assert len(net.edges) == 1 and net.edges["length_m"].iat[0] == pytest.approx(100)
    assert net.vertices["boundary"].tolist() == [False, True]


def test_the_same_segments_in_any_order_give_the_same_network():
    coords = [[(0, 0), (100, 0)], [(50, 0), (50, 50)], [(100, 0), (150, 10)], [(150, 10), (200, 0)],
              [(200, 0), (200, 80)], [(200, 0), (260, 0)]]
    a = anchors(("x.s:fp:a", 200, 1, 5.0), ("x.s:fp:b", 0, 1, 5.0))
    ref = build_network(lines(*coords), a, snap_m=1, split_m=0.5)
    rng = np.random.default_rng(1)
    for _ in range(3):
        order = rng.permutation(len(coords))
        net = build_network(lines(*[coords[i] for i in order]), a.iloc[::-1], snap_m=1, split_m=0.5)
        pd.testing.assert_frame_equal(net.vertices, ref.vertices)
        assert edge_set(net) == edge_set(ref)
        assert sorted(net.edges["length_m"].round(6)) == sorted(ref.edges["length_m"].round(6))


def test_snap_must_cover_split():
    with pytest.raises(ValueError, match="snap_m must be at least split_m"):
        build_network(lines([(0, 0), (1, 0)]), snap_m=0.5, split_m=1)
