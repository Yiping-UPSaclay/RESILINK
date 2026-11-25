"""Connectivity service (built-in model ``connectivity``): within a system, a node is served when it is working and
connected, through working nodes and links, to a working source of its own system (assumption A-TOPO-01:
meshed-optimistic topology, since open data has no switch states).

Plugin module: imports only ``resilink.plugin_api`` from the core (ADR-0008, import contract).
"""

from __future__ import annotations

import numpy as np
import scipy.sparse as sp
from scipy.sparse import csgraph

from resilink.plugin_api import NetworkView


def served(net: NetworkView, node_ok: np.ndarray, edge_ok: np.ndarray) -> np.ndarray:
    """A node is served if it is ok and its component (over ok nodes and ok edges) contains an ok source of its
    system. Systems without any source are served whenever the node is ok."""
    m = edge_ok & node_ok[net.edge_src] & node_ok[net.edge_dst]
    ones = np.ones(int(m.sum()), dtype=np.int8)
    a = sp.coo_array((ones, (net.edge_src[m], net.edge_dst[m])), shape=(net.n, net.n)).tocsr()
    _, lab = csgraph.connected_components(a, directed=False)
    has = np.zeros(lab.max() + 1, dtype=bool)
    has[lab[net.is_source & node_ok]] = True
    out = has[lab] & node_ok
    no_src_layer = ~net.layer_has_source[net.layer]
    out[no_src_layer] = node_ok[no_src_layer]
    return out


class ConnectivityService:
    def __init__(self, net: NetworkView) -> None:
        self.net = net

    def served(self, node_ok: np.ndarray, edge_ok: np.ndarray) -> np.ndarray:
        return served(self.net, node_ok, edge_ok)
