"""Line networks from open-data segments (blueprint §5.5): noding, attachment to point assets, chain contraction.

Open data publishes lines as loose segments: Enedis cuts its MV lines into short pieces, OpenStreetMap splits ways
at towers and junctions, NaTran's trace is generalised to about 250 m. Nothing says which pieces meet. This module
rebuilds the network in five deterministic steps:

1. **Clip** the segments to the territory extent (optional); the cut points are flagged as *boundary* vertices.
2. **Split at T-junctions**: a segment end lying on the interior of another segment (within ``split_m``) cuts it
   there. So does a point asset (an *anchor*) lying on a segment interior with no segment end within its radius.
3. **Attach** each segment end to the nearest anchor whose radius covers it (substations, stations, terminals).
4. **Snap** the other ends: ends within ``snap_m`` of each other are one vertex (single linkage). A snapped group
   holding an attached end joins that anchor.
5. **Contract** chains: a vertex that is not an anchor or a boundary point, with exactly two segments of the same
   kind (``same`` columns), is removed and its two segments merged. Merged edges carry the summed length and the
   length-weighted mean of the ``weighted`` columns. Self-loops and closed rings without anchors are dropped.

Lines that only cross (no end on the other line) are not connected: overhead lines crossing each other or an
underground cable passing below a road do not meet. Output order depends only on the input, so the same segments
always give the same network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from scipy.sparse import coo_array
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree
from shapely.ops import substring


@dataclass(frozen=True)
class Network:
    """``vertices``: vid, x, y, anchor (rid or None), boundary, degree. ``edges``: u, v (vids), length_m, the
    ``same`` and ``weighted`` columns, n_segments, geometry (LineString in walking order from u to v)."""

    vertices: pd.DataFrame
    edges: gpd.GeoDataFrame
    stats: dict[str, Any] = field(default_factory=dict)


def _ends(geoms: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    return (shapely.get_coordinates(shapely.get_point(geoms, 0)),
            shapely.get_coordinates(shapely.get_point(geoms, -1)))


def _clip(lines: gpd.GeoDataFrame, boundary: Any) -> gpd.GeoDataFrame:
    clipped = shapely.intersection(lines.geometry.to_numpy(), boundary)
    out = lines.drop(columns="geometry").assign(geometry=clipped)
    out = gpd.GeoDataFrame(out, geometry="geometry", crs=lines.crs).explode(index_parts=False)
    keep = (out.geometry.geom_type == "LineString") & (out.geometry.length > 0)
    return out[keep.to_numpy()].reset_index(drop=True)


def _split_positions(lines: gpd.GeoDataFrame, anchors: pd.DataFrame, split_m: float) -> dict[int, list[float]]:
    """Positions (m along the line) where each segment must be cut."""
    geoms = lines.geometry.to_numpy()
    n = len(geoms)
    start, end = _ends(geoms)
    pts = np.vstack([start, end])
    owner = np.r_[np.arange(n), np.arange(n)]
    tree = shapely.STRtree(geoms)
    cut_pts: list[np.ndarray] = []
    cut_lines: list[np.ndarray] = []

    pi, li = tree.query(shapely.points(pts), predicate="dwithin", distance=split_m)
    keep = li != owner[pi]
    pi, li = pi[keep], li[keep]
    far_from_ends = (np.hypot(*(pts[pi] - start[li]).T) > split_m) & (np.hypot(*(pts[pi] - end[li]).T) > split_m)
    cut_pts.append(pts[pi[far_from_ends]])
    cut_lines.append(li[far_from_ends])

    if len(anchors) and n:  # anchors on a segment interior with no segment end within their radius
        axy = anchors[["x", "y"]].to_numpy(dtype=float)
        radius = anchors["radius_m"].to_numpy(dtype=float)
        nearest_end, _ = cKDTree(pts).query(axy)
        lonely = (nearest_end > radius) & anchors["split"].to_numpy(dtype=bool)
        if lonely.any():
            ai, nl = tree.query_nearest(shapely.points(axy[lonely]), max_distance=float(radius[lonely].max()),
                                        all_matches=False)
            d = shapely.distance(shapely.points(axy[lonely][ai]), geoms[nl])
            ok = d <= radius[lonely][ai]
            cut_pts.append(axy[lonely][ai[ok]])
            cut_lines.append(nl[ok])

    allp, alll = np.vstack(cut_pts), np.concatenate(cut_lines)
    pos = shapely.line_locate_point(geoms[alll], shapely.points(allp))
    length = shapely.length(geoms[alll])
    inner = (pos > split_m) & (pos < length - split_m)
    out: dict[int, list[float]] = {}
    for line, p in sorted(zip(alll[inner].tolist(), pos[inner].tolist(), strict=True)):
        cuts = out.setdefault(line, [])
        if not cuts or p - cuts[-1] > split_m:
            cuts.append(p)
    return out


def _split(lines: gpd.GeoDataFrame, positions: dict[int, list[float]]) -> gpd.GeoDataFrame:
    if not positions:
        return lines
    geoms = lines.geometry.to_numpy().copy()
    rows, pieces = [], []
    for i, cuts in positions.items():
        g = geoms[i]
        stops = [0.0, *cuts, g.length]
        for a, b in pairwise(stops):
            rows.append(i)
            pieces.append(substring(g, a, b))
    untouched = np.setdiff1d(np.arange(len(lines)), np.fromiter(positions, dtype=int))
    order = np.r_[untouched, np.array(rows, dtype=int)]
    out = lines.drop(columns="geometry").iloc[order].reset_index(drop=True)
    out["geometry"] = np.r_[geoms[untouched], np.array(pieces, dtype=object)]
    out = gpd.GeoDataFrame(out, geometry="geometry", crs=lines.crs)
    return out[(out.geometry.length > 0).to_numpy()].reset_index(drop=True)


def _assign_vertices(pts: np.ndarray, anchors: pd.DataFrame, snap_m: float
                     ) -> tuple[np.ndarray, list[str | None], np.ndarray]:
    """Vertex index of every segment end; per vertex: its anchor rid (or None) and its position."""
    n = len(pts)
    anchor_of = np.full(n, -1)
    if len(anchors):
        axy = anchors[["x", "y"]].to_numpy(dtype=float)
        radius = anchors["radius_m"].to_numpy(dtype=float)
        k = min(8, len(axy))
        dist, idx = cKDTree(axy).query(pts, k=k, distance_upper_bound=float(radius.max()))
        dist, idx = dist.reshape(n, k), idx.reshape(n, k)
        valid = idx < len(axy)
        covered = np.zeros_like(valid)
        covered[valid] = dist[valid] <= radius[idx[valid]]
        first = np.where(covered.any(axis=1), covered.argmax(axis=1), -1)
        hit = first >= 0
        anchor_of[hit] = idx[hit, first[hit]]
    rids = anchors["rid"].to_numpy(dtype=object) if len(anchors) else np.array([], dtype=object)
    anchor_rid = np.array([rids[a] if a >= 0 else None for a in anchor_of], dtype=object)

    pairs = cKDTree(pts).query_pairs(snap_m, output_type="ndarray")
    graph = coo_array((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n)) if len(pairs) else \
        coo_array((n, n))
    _, cluster = connected_components(graph, directed=False)
    # a snapped group holding attached ends joins its most frequent anchor (ties: smallest rid)
    ends = pd.DataFrame({"cluster": cluster, "rid": anchor_rid})
    named = ends.dropna(subset=["rid"])
    counts = named.groupby(["cluster", "rid"]).size().reset_index(name="n")
    counts = counts.sort_values(["cluster", "n", "rid"], ascending=[True, False, True]).drop_duplicates("cluster")
    group_anchor = dict(zip(counts["cluster"], counts["rid"], strict=True))
    key = np.array([a if a is not None else group_anchor.get(c, f"#{c}") for a, c in zip(anchor_rid, cluster.tolist(),
                                                                                       strict=True)], dtype=object)

    frame = pd.DataFrame({"key": key, "x": pts[:, 0], "y": pts[:, 1]})
    mean_xy = frame.groupby("key", sort=False)[["x", "y"]].mean()
    is_free = mean_xy.index.str.startswith("#")
    named_keys = sorted(mean_xy.index[~is_free])
    free = mean_xy[is_free].sort_values(["x", "y"])
    order = [*named_keys, *free.index]
    xy = mean_xy.loc[order].to_numpy(dtype=float, copy=True).reshape(-1, 2)
    if named_keys:  # anchors keep their own position
        first_row = anchors.drop_duplicates("rid").set_index("rid")
        xy[: len(named_keys)] = first_row.loc[named_keys, ["x", "y"]].to_numpy(dtype=float)
    vid_of = pd.Series(np.arange(len(order)), index=order)
    vertex_anchor = [*named_keys, *([None] * len(free))]
    return vid_of.loc[key].to_numpy(), vertex_anchor, xy


def _contract(u: np.ndarray, v: np.ndarray, seg: gpd.GeoDataFrame, keep: np.ndarray, same: tuple[str, ...],
              weighted: tuple[str, ...]) -> tuple[list[dict[str, Any]], int]:
    nv = len(keep)
    ends = np.r_[u, v]
    seg_of_end = np.r_[np.arange(len(u)), np.arange(len(u))]
    order = np.argsort(ends, kind="stable")
    starts = np.searchsorted(ends[order], np.arange(nv + 1))
    incident = [seg_of_end[order[starts[i]:starts[i + 1]]] for i in range(nv)]
    geoms = seg.geometry.to_numpy()
    lengths = seg["length_m"].to_numpy(dtype=float)
    wvals = {c: seg[c].to_numpy(dtype=float) for c in weighted}
    visited = np.zeros(len(u), dtype=bool)
    chains: list[dict[str, Any]] = []
    dropped_rings = 0

    def walk(a: int, s: int) -> dict[str, Any]:
        path, cur = [], a
        while True:
            visited[s] = True
            fwd = u[s] == cur
            path.append((s, fwd))
            cur = int(v[s] if fwd else u[s])
            if keep[cur]:
                break
            nxt = [t for t in incident[cur] if not visited[t]]
            if not nxt:  # back at the start of a ring
                break
            s = int(nxt[0])
        coords: list[np.ndarray] = []
        for s_i, fwd in path:
            c = shapely.get_coordinates(geoms[s_i])
            c = c if fwd else c[::-1]
            if coords and np.array_equal(coords[-1][-1], c[0]):
                c = c[1:]
            coords.append(c)
        idx = [s_i for s_i, _ in path]
        total = float(lengths[idx].sum())
        row: dict[str, Any] = {"u": a, "v": cur, "length_m": total, "n_segments": len(idx),
                               "geometry": shapely.LineString(np.vstack(coords))}
        for c in same:
            row[c] = seg[c].iat[idx[0]]
        for c in weighted:
            row[c] = float((wvals[c][idx] * lengths[idx]).sum() / total) if total > 0 else float(wvals[c][idx[0]])
        return row

    for a in np.flatnonzero(keep):
        for s in incident[a]:
            if not visited[s]:
                chains.append(walk(int(a), int(s)))
    for s in range(len(u)):  # rings made only of contractible vertices
        if not visited[s]:
            walk(int(u[s]), s)
            dropped_rings += 1
    return chains, dropped_rings


def build_network(lines: gpd.GeoDataFrame, anchors: pd.DataFrame | None = None, *, snap_m: float, split_m: float,
                  boundary: Any = None, boundary_tol_m: float = 1.0, same: tuple[str, ...] = (),
                  weighted: tuple[str, ...] = ()) -> Network:
    """Build a network from line segments (LineStrings in a metric CRS) and point anchors.

    ``anchors``: rid, x, y, radius_m (several rows may share a rid: one asset with several footprints) and optionally
    split (default true: the anchor may cut a segment passing within its radius when no segment end is near).
    ``same``: columns that must match for two segments to merge; ``weighted``: numeric columns averaged by length.
    """
    if snap_m < split_m:
        raise ValueError("snap_m must be at least split_m, so that split ends snap together")
    anchors = (anchors if anchors is not None else pd.DataFrame(columns=["rid", "x", "y", "radius_m"])).copy()
    if "split" not in anchors:
        anchors["split"] = True
    anchors = anchors.sort_values(["rid", "x", "y"]).reset_index(drop=True)
    cols = [*same, *(c for c in weighted if c not in same)]
    seg = gpd.GeoDataFrame(lines[[*cols, "geometry"]].copy(), geometry="geometry", crs=lines.crs).reset_index(drop=True)
    seg = seg[seg.geometry.notna() & ~seg.geometry.is_empty].explode(index_parts=False).reset_index(drop=True)
    stats: dict[str, Any] = {"segments_in": len(seg)}
    if boundary is not None:
        seg = _clip(seg, boundary)
        stats["segments_after_clip"] = len(seg)
    positions = _split_positions(seg, anchors, split_m) if len(seg) else {}
    seg = _split(seg, positions)
    stats["segments_split"] = len(positions)
    seg["length_m"] = seg.geometry.length.to_numpy()

    start, end = _ends(seg.geometry.to_numpy())
    vid, vertex_anchor, xy = _assign_vertices(np.vstack([start, end]), anchors, snap_m)
    n = len(seg)
    u, v = vid[:n], vid[n:]
    loops = u == v
    stats["self_loops_dropped"] = int(loops.sum())
    seg, u, v = seg[~loops].reset_index(drop=True), u[~loops], v[~loops]

    nv = len(vertex_anchor)
    degree = np.bincount(np.r_[u, v], minlength=nv)
    is_anchor = np.array([a is not None for a in vertex_anchor], dtype=bool)
    on_boundary = np.zeros(nv, dtype=bool)
    if boundary is not None:
        ring = shapely.boundary(boundary)
        on_boundary = (shapely.distance(shapely.points(xy), ring) <= boundary_tol_m) & ~is_anchor
    keep = is_anchor | on_boundary | (degree != 2)
    if same:  # a degree-2 vertex between segments of different kinds stays
        for col in same:
            vals = seg[col].astype(str).to_numpy()
            seen: dict[int, str] = {}
            for i, (a, b) in enumerate(zip(u, v, strict=True)):
                for w in (a, b):
                    if degree[w] == 2 and not keep[w]:
                        if w in seen and seen[w] != vals[i]:
                            keep[w] = True
                        seen.setdefault(w, vals[i])
    chains, rings = _contract(u, v, seg, keep, same, weighted)
    edges = gpd.GeoDataFrame(chains, geometry="geometry", crs=lines.crs) if chains else gpd.GeoDataFrame(
        columns=["u", "v", "length_m", "n_segments", *cols, "geometry"], geometry="geometry", crs=lines.crs)
    loops2 = edges["u"].to_numpy() == edges["v"].to_numpy()
    edges = edges[~loops2].reset_index(drop=True)
    stats["rings_dropped"] = rings + int(loops2.sum())

    used = np.unique(np.r_[edges["u"].to_numpy(dtype=int), edges["v"].to_numpy(dtype=int)])
    remap = np.full(nv, -1)
    remap[used] = np.arange(len(used))
    edges["u"] = remap[edges["u"].to_numpy(dtype=int)]
    edges["v"] = remap[edges["v"].to_numpy(dtype=int)]
    final_degree = np.bincount(np.r_[edges["u"], edges["v"]], minlength=len(used))
    vertices = pd.DataFrame({"vid": np.arange(len(used)), "x": xy[used, 0], "y": xy[used, 1],
                             "anchor": [vertex_anchor[i] for i in used], "boundary": on_boundary[used],
                             "degree": final_degree})
    stats.update({"segments": n, "vertices": len(vertices), "edges": len(edges),
                  "anchors_attached": int(vertices["anchor"].notna().sum()),
                  "boundary_vertices": int(vertices["boundary"].sum()),
                  "dead_ends": int(((final_degree == 1) & vertices["anchor"].isna().to_numpy()
                                    & ~vertices["boundary"].to_numpy()).sum())})
    return Network(vertices=vertices, edges=edges, stats=stats)
