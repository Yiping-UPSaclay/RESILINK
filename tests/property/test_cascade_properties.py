"""Property-based checks of cascade semantics on the synthetic territory (hypothesis)."""

from __future__ import annotations

import numpy as np
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from resilink.cascade.topological import TopologicalTemporalCascade
from resilink.dependencies.model import DependencyRealisation, infer_proximity_softmax, load_rules
from resilink.domain.vocab import CAUSE_INTER
from resilink.fragility.lognormal import Damage

SETTINGS = settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])


def _damage(net, nodes: set[int], edges: set[int], repair: float = 10.0) -> Damage:
    n = np.zeros(net.n)
    e = np.zeros(net.e)
    n[list(nodes)] = repair
    e[list(edges)] = repair
    return Damage(n, e)


@SETTINGS
@given(data=st.data())
def test_more_damage_never_fewer_failures(net, data):
    deps = infer_proximity_softmax(net, load_rules()).realise(np.random.default_rng(0))
    a = set(data.draw(st.lists(st.integers(0, net.n - 1), max_size=6)))
    extra = set(data.draw(st.lists(st.integers(0, net.n - 1), max_size=6)))
    model = TopologicalTemporalCascade()
    small = model.run(net, deps, _damage(net, a, set()), 72.0).ever_down(net.n)
    large = model.run(net, deps, _damage(net, a | extra, set()), 72.0).ever_down(net.n)
    assert np.all(large >= small)


@SETTINGS
@given(nodes=st.sets(st.integers(0, 58), max_size=8))
def test_without_dependencies_no_cross_layer_failures(net, nodes):
    nodes = {n for n in nodes if n < net.n}
    tr = TopologicalTemporalCascade().run(net, DependencyRealisation.empty(), _damage(net, nodes, set()), 72.0)
    assert not np.any(tr.cause == CAUSE_INTER)
    damaged_layers = {int(net.layer[n]) for n in nodes}
    failed_layers = {int(net.layer[n]) for n in tr.node[tr.kind == 1]}
    assert failed_layers <= damaged_layers  # layers are independent without dependencies


@SETTINGS
@given(nodes=st.sets(st.integers(0, 58), min_size=1, max_size=5))
def test_downtime_bounded_by_horizon_and_restores_after_repair(net, nodes):
    nodes = {n for n in nodes if n < net.n}
    deps = infer_proximity_softmax(net, load_rules()).realise(np.random.default_rng(1))
    tr = TopologicalTemporalCascade().run(net, deps, _damage(net, nodes, set(), repair=10.0), 72.0)
    total, direct = tr.downtime(net.n)
    assert np.all(total <= 72.0 + 1e-9) and np.all(direct <= total + 1e-9)
    # with every repair at 10 h and backups <= horizon, nothing stays down beyond the repairs + longest backup chain
    assert np.all(total <= 72.0)


def test_layer_codes_follow_the_systems_present_in_the_release(net, release):
    """Codes are internal to a compiled network: the release's systems, sorted (installing packs never shifts them)."""
    assert net.layer_names == tuple(sorted(set(release.read("nodes")["layer"])))
    assert all(net.layer_code(name) == i for i, name in enumerate(net.layer_names))
