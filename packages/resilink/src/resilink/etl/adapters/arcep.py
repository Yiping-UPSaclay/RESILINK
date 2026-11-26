"""ARCEP mobile sites ("Mon Réseau Mobile") -> ``stg_telecom_mobile_sites`` (D3).

One row per operator site; a shared mast appears once per operator. The 2026 T2 file departs from its documentation
(checked 2026-11-26) and each defect is repaired here, counted in the QA figures:
* text mixes UTF-8 and cp1252 within a line: bytes that are not UTF-8 are read as cp1252;
* leading zeros are stripped from département, commune and ANFR codes: numeric codes are zero-padded (ANFR station
  ids to 10 characters, whose first three give the département, as on Corsican ids which kept theirs);
* some x/y use a decimal comma;
* some ``num_site`` values were turned into scientific notation by a spreadsheet: kept, flagged, never used as a key.
Stable id: ``telecom.mobile_site:anfr:<ANFR id>-<operator code>``, or a 1 m position fingerprint when the ANFR id is
missing. x/y are Lambert-93 (within 1 m of the file's latitude/longitude).
"""

from __future__ import annotations

import codecs
import io
import re

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from pyproj import Transformer

from resilink.domain.ids import point_fingerprint
from resilink.etl.staging import AdapterContext, Output, adapter

SRC = "arcep.sites"
TECH = ("site_2g", "site_3g", "site_4g", "site_5g")
FLAGS = ("site_zb", "site_dcc", "site_strategique", "site_capa_240mbps")
SPREADSHEET_NUMBER = re.compile(r"^\d+(?:[.,]\d+)?E[+-]?\d+$", re.IGNORECASE)


def decode_mixed(raw: bytes) -> tuple[str, int]:
    """UTF-8 text in which some byte runs are cp1252; returns the text and the number of runs repaired."""
    repaired = 0

    def fallback(exc: UnicodeDecodeError) -> tuple[str, int]:
        nonlocal repaired
        repaired += 1
        return exc.object[exc.start:exc.end].decode("cp1252", errors="replace"), exc.end

    name = "resilink_cp1252_fallback"
    codecs.register_error(name, fallback)
    return raw.decode("utf-8", errors=name).lstrip("﻿"), repaired


def pad(code: object, width: int) -> str:
    """Zero-pad a numeric code stripped of its leading zeros ('4' -> '04'); other codes ('2A') unchanged."""
    s = "" if code is None or (isinstance(code, float) and np.isnan(code)) else str(code).strip()
    return s.zfill(width) if s.isdigit() else s


def _flag(col: pd.Series) -> np.ndarray:
    return col.fillna("0").astype(str).str.strip().eq("1").to_numpy()


def _num(col: pd.Series) -> np.ndarray:
    return pd.to_numeric(col.astype(str).str.replace(",", ".", regex=False), errors="coerce").to_numpy(float)


@adapter("arcep.sites", version="1.0.0", sources=(SRC,), needs=("territory_extent",),
         produces=("stg_telecom_mobile_sites",))
def arcep_sites(ctx: AdapterContext) -> dict[str, Output]:
    """Operator mobile sites inside the territory buffer, with ANFR station ids, technologies and shared masts."""
    t = ctx.territory
    text, repaired = decode_mixed(ctx.path(SRC).read_bytes())
    df = pd.read_csv(io.StringIO(text), sep=ctx.sources[SRC].delimiter, dtype=str)
    df.columns = [c.strip().lower() for c in df.columns]
    n_national = len(df)
    x, y = _num(df["x"]), _num(df["y"])
    lon, lat = _num(df["longitude"]), _num(df["latitude"])
    tx, ty = Transformer.from_crs("EPSG:4326", t.crs_metric, always_xy=True).transform(lon, lat)
    gap = np.hypot(tx - x, ty - y)

    extent = ctx.read("territory_extent").set_index("kind")
    core, grown = extent.geometry["core"], extent.geometry["buffer"]
    shapely.prepare(core)
    shapely.prepare(grown)
    ok = np.isfinite(x) & np.isfinite(y)
    keep = ok & shapely.contains_xy(grown, np.where(ok, x, 0.0), np.where(ok, y, 0.0))
    df, x, y = df[keep].reset_index(drop=True), x[keep], y[keep]

    anfr = df["id_station_anfr"].map(lambda v: pad(v, 10))
    op = df["code_op"].map(lambda v: pad(v, 5))
    support = [point_fingerprint(a, b) for a, b in zip(x, y, strict=True)]
    rid = [f"telecom.mobile_site:anfr:{a}-{o}" if a else f"telecom.mobile_site:fp:{s}-{o}"
           for a, o, s in zip(anfr, op, support, strict=True)]
    dep = df["insee_dep"].map(lambda v: pad(v, 2))
    mangled = df["num_site"].fillna("").str.strip().str.match(SPREADSHEET_NUMBER).to_numpy()
    sites = gpd.GeoDataFrame({
        "rid": rid, "code_op": op.to_numpy(), "operator": df["nom_op"].fillna("").to_numpy(),
        "id_station_anfr": anfr.to_numpy(), "num_site": df["num_site"].fillna("").to_numpy(),
        "num_site_spreadsheet_damaged": mangled, "id_site_partage": df["id_site_partage"].fillna("").to_numpy(),
        "insee_dep": dep.to_numpy(), "insee_com": df["insee_com"].map(lambda v: pad(v, 5)).to_numpy(),
        **{c: _flag(df[c]) for c in TECH + FLAGS if c in df.columns},
        "date_5g": df.get("date_ouverturecommerciale_5g", pd.Series("", index=df.index)).fillna("").str[:10].to_numpy(),
        "support_id": [f"telecom.support:fp:{s}" for s in support],
        "in_core": shapely.contains_xy(core, x, y),
        "geometry": shapely.points(x, y),
    }, geometry="geometry", crs=t.crs_metric)
    dups = int(sites["rid"].duplicated().sum())
    if dups:
        raise ValueError(f"{SRC}: {dups} duplicate site ids, e.g. {sites.loc[sites['rid'].duplicated(), 'rid'].head(3).tolist()}")
    sites = sites.sort_values("rid").reset_index(drop=True)

    core_sites = sites[sites["in_core"]]
    per_support = core_sites.groupby("support_id")["code_op"].nunique()
    with_anfr = sites["id_station_anfr"] != ""
    qa = {
        "rows_national": n_national, "sites": len(sites), "sites_core": len(core_sites),
        "core_by_operator": core_sites["operator"].value_counts().sort_index().to_dict(),
        "core_with_technology": {c[5:]: int(core_sites[c].sum()) for c in TECH if c in core_sites},
        "sites_with_core_departement_code": int(sites["insee_dep"].isin(
            [str(c) for c in t.selector.get("departement", [])]).sum()),  # ARCEP's own attribute, cross-check
        # same 1 m position for several operators; operators often survey a shared mast a few metres apart, so
        # ARCEP's shared-site id (id_site_partage) is the better measure of sharing
        "core_supports": len(per_support), "core_supports_shared_same_position": int((per_support > 1).sum()),
        "core_sites_with_shared_site_id": int((core_sites["id_site_partage"] != "").sum()),
        "missing_anfr_id": int((~with_anfr).sum()),
        "anfr_departement_differs": int((with_anfr & (sites["id_station_anfr"].str[1:3] != sites["insee_dep"].str[:2])
                                         & ~sites["insee_dep"].str.startswith("97")).sum()),
        "codes_zero_padded": int((df["insee_dep"].fillna("").str.len() == 1).sum()),
        "num_site_spreadsheet_damaged": int(mangled.sum()),
        "text_runs_repaired_from_cp1252_national": repaired,
        "xy_vs_latlon_max_m": round(float(np.nanmax(gap)), 2) if len(gap) else None,
    }
    return {"stg_telecom_mobile_sites": Output(sites, "internal", qa)}
