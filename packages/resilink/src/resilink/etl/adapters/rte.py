"""RTE sites (ODRÉ, attributes only: RTE publishes no geometry since 2026-06-16) -> ``stg_elec_rte_sites`` (D2).

The national file lists substations ("Poste de transformation") and tapping points ("Point de piquage") with their
highest voltage and département name. The sites of the départements meeting the territory buffer are kept: they are
the completeness reference for high-voltage substations found in OpenStreetMap (matched in D6).
"""

from __future__ import annotations

import re
import unicodedata

import pandas as pd

from resilink.etl.staging import AdapterContext, Output, adapter

SRC = "rte.postes"


def name_key(name: str) -> str:
    """Accent-, case- and punctuation-insensitive key for French place names ("Bouches-du-Rhône" == "BOUCHES DU RHONE")."""
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def tension_kv(label: str) -> float | None:
    """'225kV' -> 225.0; '<45kV' and 'COURANT CONTINU' (HVDC) carry no single AC voltage -> None."""
    m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*kV\s*", str(label), flags=re.IGNORECASE)
    return float(m.group(1).replace(",", ".")) if m else None


@adapter("rte.sites", version="1.0.0", sources=(SRC,), needs=("admin_areas", "stg_elec_substations_osm"),
         produces=("stg_elec_rte_sites",))
def rte_sites(ctx: AdapterContext) -> dict[str, Output]:
    """RTE substations and tapping points of the départements meeting the territory buffer (no geometry), with
    the OSM substation carrying the same RTE code (tag ``ref:FR:RTE``) when there is one."""
    df = pd.read_csv(ctx.path(SRC), sep=";", dtype=str, encoding="utf-8-sig").fillna("")
    admin = ctx.read("admin_areas")
    deps = admin[admin["level"] == "departement"]
    by_key = {name_key(n): (c, bool(core)) for n, c, core in zip(deps["name"], deps["code"], deps["in_core"], strict=True)}
    keys = df["departement"].map(name_key)
    keep = keys.isin(by_key)
    out = df[keep].copy()
    out["departement_code"] = keys[keep].map(lambda k: by_key[k][0])
    out["in_core"] = keys[keep].map(lambda k: by_key[k][1]).astype(bool)
    out["tension_kv"] = out["tension"].map(tension_kv).astype(float)
    out = out[["code_poste", "nom_poste", "fonction", "etat", "tension", "tension_kv", "departement",
               "departement_code", "in_core"]].sort_values("code_poste").reset_index(drop=True)
    if out["code_poste"].duplicated().any():
        raise ValueError(f"{SRC}: duplicate code_poste {out.loc[out['code_poste'].duplicated(), 'code_poste'].tolist()}")
    osm = ctx.read("stg_elec_substations_osm")
    by_ref: dict[str, list[str]] = {}
    for osm_id, refs in zip(osm["osm_id"], osm["ref_fr_rte"], strict=True):
        for ref in (r.strip() for r in str(refs).split(";")):
            if ref:
                by_ref.setdefault(ref, []).append(osm_id)
    out["osm_ids_by_ref"] = out["code_poste"].map(lambda c: ";".join(sorted(by_ref.get(c, []))))
    core = out[out["in_core"]]
    in_service = core[(core["fonction"] == "Poste de transformation") & (core["etat"] == "EN EXPLOITATION")]
    qa = {
        "sites_national": len(df), "sites": len(out), "sites_core": len(core),
        "departements_found": sorted(out["departement_code"].unique()),
        "departements_missing": sorted(c for c, _ in by_key.values() if c not in set(out["departement_code"])),
        "core_by_function": core["fonction"].value_counts().sort_index().to_dict(),
        "core_substations_by_tension": core.loc[core["fonction"] == "Poste de transformation", "tension"]
        .value_counts().sort_index().to_dict(),
        # blueprint §5.6 gate (>= 90 % once D6 adds name and position matching); here by RTE code only
        "core_substations_in_service": len(in_service),
        "core_substations_matched_by_osm_ref": int((in_service["osm_ids_by_ref"] != "").sum()),
        "core_substations_unmatched": in_service.loc[in_service["osm_ids_by_ref"] == "", "code_poste"].tolist(),
    }
    return {"stg_elec_rte_sites": Output(out, "public", qa)}
