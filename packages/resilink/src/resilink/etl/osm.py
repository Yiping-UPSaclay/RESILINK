"""Tagged OpenStreetMap features from a PBF file, read with DuckDB spatial ``ST_ReadOSM`` (no extra dependency).

Two passes over the file: the ways matching a condition, then only the nodes they reference plus the matching
nodes. Relations matching the node condition are counted, not built (multipolygon relations are rare for the
features used here). Conditions are DuckDB SQL over the ``tags`` map, written in code (never from user input).
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import shapely

from resilink.io.duck import load_extension


def tag_in(key: str, values: tuple[str, ...]) -> str:
    """SQL condition ``tags[key] IN (...)``."""
    return f"tags['{key}'] IN (" + ", ".join("'" + v.replace("'", "''") + "'" for v in values) + ")"


def read_osm(path: Path, tags: tuple[str, ...], way_where: str, node_where: str | None = None,
             ) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    """Ways matching ``way_where`` (node coordinates as ordered ``lon``/``lat`` lists) and nodes matching
    ``node_where``; one column per tag in ``tags``."""
    con = duckdb.connect()
    load_extension(con, "spatial")
    nw = node_where or "FALSE"
    cols = ", ".join(f"tags['{t}'] AS \"{t}\"" for t in tags)
    con.execute(f"""CREATE TEMP TABLE w AS SELECT id, refs, {cols} FROM ST_ReadOSM(?)
                    WHERE kind = 'way' AND ({way_where})""", [str(path)])
    con.execute("CREATE TEMP TABLE r AS SELECT id AS way, unnest(refs) AS ref, generate_subscripts(refs, 1) AS i FROM w")
    con.execute(f"""CREATE TEMP TABLE n AS SELECT kind, id, lat, lon, ({nw}) AS hit, {cols} FROM ST_ReadOSM(?)
                    WHERE (kind = 'node' AND (id IN (SELECT ref FROM r) OR ({nw}))) OR (kind = 'relation' AND ({nw}))""",
                [str(path)])
    coords = con.execute("""SELECT r.way AS id, list(n.lon ORDER BY r.i) FILTER (WHERE n.id IS NOT NULL) AS lon,
                                   list(n.lat ORDER BY r.i) FILTER (WHERE n.id IS NOT NULL) AS lat,
                                   count(*) FILTER (WHERE n.id IS NULL) AS missing
                            FROM r LEFT JOIN (SELECT id, lon, lat FROM n WHERE kind = 'node') n ON n.id = r.ref
                            GROUP BY r.way""").df()
    ways = con.execute("SELECT * EXCLUDE (refs) FROM w").df().merge(coords, on="id", how="left")
    nodes = con.execute("SELECT * EXCLUDE (kind, hit) FROM n WHERE kind = 'node' AND hit").df()
    counts = {"ways": len(ways), "nodes": len(nodes),
              "relations_skipped": int(con.execute("SELECT count(*) FROM n WHERE kind = 'relation'").fetchone()[0]),
              "missing_node_refs": int(coords["missing"].sum()) if len(coords) else 0}
    con.close()
    return ways, nodes, counts


def read_features(path: Path, key: str, way_values: tuple[str, ...], node_values: tuple[str, ...],
                  tags: tuple[str, ...]) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    """Ways with ``key`` in ``way_values`` and nodes with ``key`` in ``node_values`` (see ``read_osm``)."""
    return read_osm(path, tags, tag_in(key, way_values), tag_in(key, node_values) if node_values else None)


def way_geometry(lon: object, lat: object, closed_polygon: bool) -> shapely.Geometry | None:
    """LineString of a way, or Polygon when asked for and the way is closed; None without two located nodes."""
    if lon is None or lat is None or len(lon) < 2:  # type: ignore[arg-type]
        return None
    xy = np.column_stack([np.asarray(lon, float), np.asarray(lat, float)])
    if closed_polygon and len(xy) >= 4 and np.array_equal(xy[0], xy[-1]):
        return shapely.Polygon(xy)
    return shapely.LineString(xy)
