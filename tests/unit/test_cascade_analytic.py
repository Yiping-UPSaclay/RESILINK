"""Hand-checkable cascades on a 5-node toy network (analytic expectations)."""

from __future__ import annotations

import numpy as np
import pytest
import scipy.sparse as sp

from resilink.cascade.topological import TopologicalTemporalCascade
from resilink.network.compiled import CompiledNetwork
from resilink.plugin_api import CAUSE_DIRECT, CAUSE_INTER, CAUSE_INTRA, DependencyRealisation, outage
from resilink.service.connectivity import ConnectivityService

# elec: S(source) - A - B ; telecom: X(source) - T ; T depends on B
RIDS = ["elec.sub_tx:t:S", "elec.sub_mv_lv:t:A", "elec.sub_mv_lv:t:B", "telecom.exchange:t:X", "telecom.mobile_site:t:T"]


def toy() -> CompiledNetwork:
    layer = np.array([0, 0, 0, 2, 2], dtype=np.int8)
    src = np.array([0, 1, 3])
    dst = np.array([1, 2, 4])
    supply = {s: sp.csr_array((1, 5)) for s in ("elec", "gas", "telecom")}
    return CompiledNetwork(
        release_id="toy-2026.01.0", rid=np.array(RIDS, dtype=object), layer=layer,
        cls=np.array(["sub_tx", "sub_mv_lv", "sub_mv_lv", "exchange", "mobile_site"], dtype=object),
        is_source=np.array([True, False, False, True, False]), xy=np.zeros((5, 2)),
        repair_median_h=np.ones(5), edge_rid=np.array(["e1", "e2", "e3"], dtype=object), edge_src=src, edge_dst=dst,
        edge_cls=np.array(["cable_mv", "cable_mv", "backhaul_logical"], dtype=object), edge_repair_median_h=np.ones(3),
        cell_id=np.array(["c0"], dtype=object), cell_pop=np.array([1.0]), cell_commune=np.array(["x"], dtype=object),
        supply=supply, layer_has_source=np.array([True, False, True, False]),
        index={r: i for i, r in enumerate(RIDS)}, edge_index={"e1": 0, "e2": 1, "e3": 2},
    )


def deps(backup: float) -> DependencyRealisation:
    return DependencyRealisation(np.array([4]), np.array([2]), np.array([backup]), np.array([0]))


def run(damage_nodes: dict[int, float], backup: float | None, horizon: float = 72.0):
    net = toy()
    rep = np.zeros(5)
    for i, t in damage_nodes.items():
        rep[i] = t
    d = deps(backup) if backup is not None else DependencyRealisation.empty()
    damage, plan = outage(rep, np.zeros(3))
    return net, TopologicalTemporalCascade(net, ConnectivityService(net)).run(d, damage, plan, horizon)


def test_no_damage_no_events():
    _, tr = run({}, backup=2.0)
    assert len(tr.t) == 0


def test_intra_and_inter_with_backup():
    net, tr = run({1: 5.0}, backup=2.0)  # A flooded, repaired at 5 h
    ev = {(net.rid[n], k): (t, c) for t, n, k, c in zip(tr.t, tr.node, tr.kind, tr.cause, strict=True)}
    assert ev[("elec.sub_mv_lv:t:A", 1)] == (0.0, CAUSE_DIRECT)
    assert ev[("elec.sub_mv_lv:t:B", 1)] == (0.0, CAUSE_INTRA)
    assert ev[("telecom.mobile_site:t:T", 1)] == (2.0, CAUSE_INTER)  # battery lasts 2 h
    assert ev[("telecom.mobile_site:t:T", -1)][0] == 5.0
    total, direct = tr.downtime(net.n)
    assert total.tolist() == pytest.approx([0, 5, 5, 0, 3])
    assert direct.tolist() == pytest.approx([0, 5, 0, 0, 0])


def test_zero_backup_fails_immediately():
    _net, tr = run({1: 5.0}, backup=0.0)
    t_fail = tr.t[(tr.node == 4) & (tr.kind == 1)]
    assert t_fail.tolist() == [0.0]


def test_backup_longer_than_outage_prevents_failure():
    _net, tr = run({1: 5.0}, backup=6.0)
    assert 4 not in tr.node.tolist()


def test_horizon_truncates_downtime():
    net, tr = run({1: 100.0}, backup=2.0, horizon=24.0)
    total, _ = tr.downtime(net.n)
    assert total[1] == pytest.approx(24.0) and total[4] == pytest.approx(22.0)
