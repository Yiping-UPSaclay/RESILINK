"""INSEE Filosofi 2021, 200 m cells -> ``grid_cells`` (D1).

Cell ids are INSPIRE grid codes, ``CRS3035RES200mN<northing>E<easting>``, where the coordinates are the cell's
south-west corner in EPSG:3035. This was checked on all 2,298,582 metropolitan cells on 2026-11-26: every
coordinate is a multiple of 200 and every 1 km parent id equals the floor of its child's coordinates.
The file lists inhabited cells only; values are imputed where a cell has fewer than 11 fiscal households.
Population = individuals in fiscal households (assumption A-POP-01).
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import shapely
from pyproj import Transformer

from resilink.etl.staging import AdapterContext, Output, adapter

SRC = "insee.filosofi2021_200m"
ID_PATTERN = r"^CRS(?P<epsg>\d+)RES(?P<size>\d+)mN(?P<n>\d+)E(?P<e>\d+)$"
TEXT_COLUMNS = ("idcar_200m", "idcar_1km", "idcar_nat", "lcog_geo")
NEAREST_COMMUNE_M = 500.0  # coastal cells whose centre falls in the sea take the nearest commune within this
# lcog_geo codes Paris, Lyon and Marseille by municipal arrondissement; ADMIN EXPRESS communes are whole cities
ARRONDISSEMENTS = re.compile(r"^(?:751(?:0[1-9]|1\d|20)|6938[1-9]|132(?:0[1-9]|1[0-6]))$")
ARR_CITY = {"751": "75056", "693": "69123", "132": "13055"}


def lcog_communes(lcog_geo: str) -> set[str]:
    """Communes listed in a cell's ``lcog_geo`` (comma-separated, largest share first), arrondissements mapped to
    their city."""
    codes = [c.strip() for c in lcog_geo.split(",") if c.strip()]
    return {ARR_CITY[c[:3]] if ARRONDISSEMENTS.match(c) else c for c in codes}


def departement_of(code: str) -> str:
    return code[:3] if code.startswith("97") else code[:2]


def cell_box(cell_id: str) -> tuple[int, float, float, float, float]:
    """(epsg, xmin, ymin, xmax, ymax) of an INSPIRE grid cell id (the id gives the south-west corner)."""
    m = re.match(ID_PATTERN, cell_id)
    if not m:
        raise ValueError(f"not an INSPIRE grid cell id: {cell_id!r}")
    size, n, e = int(m["size"]), int(m["n"]), int(m["e"])
    return int(m["epsg"]), float(e), float(n), float(e + size), float(n + size)


def read_cells(path: Path, epsg: int) -> tuple[str, pa.Table]:
    """The archive member whose cell ids are in ``epsg`` (metropolitan France: 3035)."""
    prefix = f"CRS{epsg}RES".encode()
    opts = pacsv.ConvertOptions(column_types={c: pa.string() for c in TEXT_COLUMNS})
    with zipfile.ZipFile(path) as z:
        for name in sorted(n for n in z.namelist() if n.lower().endswith(".csv")):
            with z.open(name) as fh:
                fh.readline()
                if not fh.readline().startswith(prefix):
                    continue
            with z.open(name) as fh:
                return name, pacsv.read_csv(fh, convert_options=opts)
    raise ValueError(f"{path}: no member with cell ids in EPSG:{epsg}")


@adapter("insee.filosofi2021", version="1.0.0", sources=(SRC,), needs=("admin_areas", "territory_extent"),
         produces=("grid_cells",))
def filosofi(ctx: AdapterContext) -> dict[str, Output]:
    """200 m population cells whose centre lies in the territory buffer, with their COG commune.

    ``pop`` is Filosofi's ``ind`` (individuals in fiscal households); the other INSEE variables keep their names.
    The commune is the one containing the cell centre (COG edition of ``admin_areas``), else the nearest within
    500 m for coastal cells; INSEE's own ``lcog_geo`` (COG 2021) is kept as ``lcog_geo_2021``.
    """
    t = ctx.territory
    grid_epsg, size = int(str(t.grid.get("crs", "")).split(":")[-1]), int(t.grid.get("size_m", 0))
    if (grid_epsg, size) != (3035, 200):
        raise ValueError(f"territory {t.id}: Filosofi 2021 cells are 200 m in EPSG:3035, not {t.grid}")
    extent = ctx.read("territory_extent").set_index("kind")
    core, grown = extent.geometry["core"], extent.geometry["buffer"]
    admin = ctx.read("admin_areas")

    member, tab = read_cells(ctx.path(SRC), grid_epsg)
    parts = pc.extract_regex(tab.column("idcar_200m"), ID_PATTERN)
    if parts.null_count:
        raise ValueError(f"{SRC}/{member}: {parts.null_count} cell ids are not INSPIRE codes")
    n = pc.cast(pc.struct_field(parts, "n"), pa.int64()).to_numpy()
    e = pc.cast(pc.struct_field(parts, "e"), pa.int64()).to_numpy()
    sizes = set(pc.unique(pc.struct_field(parts, "size")).to_pylist())
    if sizes != {str(size)}:
        raise ValueError(f"{SRC}/{member}: cell sizes {sorted(sizes)} != {size}")

    # INSEE's own total for the core, independent of geometry: cells whose largest-share commune is in a core
    # département (the staged core total should match it within boundary effects)
    deps = {str(c) for c in t.selector.get("departement", [])}
    dominant = tab.column("lcog_geo").to_pandas().fillna("").str.split(",").str[0].map(departement_of)
    insee_core = float(np.asarray(tab.column("ind").to_numpy(zero_copy_only=False))[dominant.isin(deps).to_numpy()].sum())

    xmin, ymin, xmax, ymax = gpd.GeoSeries([grown], crs=t.crs_metric).to_crs(grid_epsg).total_bounds
    window = (e + size > xmin) & (e < xmax) & (n + size > ymin) & (n < ymax)
    df = tab.filter(pa.array(window)).to_pandas()
    e, n = e[window], n[window]
    cx, cy = Transformer.from_crs(grid_epsg, t.crs_metric, always_xy=True).transform(e + size / 2, n + size / 2)
    shapely.prepare(grown)
    shapely.prepare(core)
    keep = shapely.contains_xy(grown, cx, cy)
    df, e, n, cx, cy = df[keep].reset_index(drop=True), e[keep], n[keep], cx[keep], cy[keep]

    pts = gpd.GeoDataFrame({"i": np.arange(len(df))}, geometry=gpd.points_from_xy(cx, cy), crs=t.crs_metric)
    communes = admin.loc[admin["level"] == "commune", ["code", "geometry"]]
    hit = gpd.sjoin(pts, communes, how="inner", predicate="within").sort_values(["i", "code"]).drop_duplicates("i")
    code = pd.Series("", index=pts.index, dtype=object)
    code.loc[hit["i"].to_numpy()] = hit["code"].to_numpy()
    lost = pts[code == ""]
    near = gpd.sjoin_nearest(lost, communes, how="inner", max_distance=NEAREST_COMMUNE_M).sort_values(
        ["i", "code"]).drop_duplicates("i") if len(lost) else lost.assign(code=[])
    code.loc[near["i"].to_numpy()] = near["code"].to_numpy()

    cells = gpd.GeoSeries(shapely.box(e, n, e + size, n + size), crs=f"EPSG:{grid_epsg}").to_crs(t.crs_metric)
    numeric = [c for c in df.columns if c not in TEXT_COLUMNS and c not in ("i_est_200", "i_est_1km", "ind")]
    grid = gpd.GeoDataFrame({
        "cell_id": df["idcar_200m"],
        "pop": df["ind"].astype(float),
        "admin_commune": code.astype(str).to_numpy(),
        "in_core": shapely.contains_xy(core, cx, cy),
        "imputed": df["i_est_200"].astype(bool),
        "imputed_1km": df["i_est_1km"].astype(bool),
        "lcog_geo_2021": df["lcog_geo"].fillna(""),
        **{c: df[c].astype(float) for c in numeric},
        "geometry": cells.values,
    }, geometry="geometry", crs=t.crs_metric).sort_values("cell_id").reset_index(drop=True)
    grid.insert(1, "k", np.arange(len(grid), dtype=np.uint32))

    in_core = grid["in_core"]
    census = int(admin.loc[(admin["level"] == "commune") & admin["in_core"], "population"].sum())
    pop_core = float(grid.loc[in_core, "pop"].sum())
    agrees = [c in lcog_communes(lc) for c, lc in zip(grid["admin_commune"], grid["lcog_geo_2021"], strict=True)]
    qa = {
        "source_member": member, "cells": len(grid), "cells_core": int(in_core.sum()),
        "cells_in_window": int(window.sum()),
        "pop_total": round(float(grid["pop"].sum()), 1), "pop_core": round(pop_core, 1),
        # Filosofi counts people in fiscal households (2021); the census population (ADMIN EXPRESS) also counts
        # people living in communities (care homes, student halls, barracks, prisons), so the ratio is below 1
        "insee_pop_core_by_dominant_commune": round(insee_core, 1),
        "pop_core_vs_insee": round(pop_core / insee_core, 4) if insee_core else None,
        "census_population_core": census, "pop_core_vs_census": round(pop_core / census, 4) if census else None,
        "commune_agrees_with_insee": round(float(np.mean(agrees)), 4) if agrees else None,
        "imputed_cells_share": round(float(grid["imputed"].mean()), 4) if len(grid) else None,
        "pop_share_in_imputed_cells": round(float(grid.loc[grid["imputed"], "pop"].sum() / grid["pop"].sum()), 4)
        if len(grid) and grid["pop"].sum() else None,
        "cells_commune_by_nearest": len(near), "cells_without_commune": int((grid["admin_commune"] == "").sum()),
    }
    return {"grid_cells": Output(grid, "public", qa)}
