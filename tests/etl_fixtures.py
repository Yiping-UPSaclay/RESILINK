"""Offline fixtures for the real-data track: a small territory east of Marseille and a mock of every portal the
connectors talk to (Géoplateforme WFS, insee.fr, Enedis Data Fair, ODRÉ, Geofabrik), served through httpx."""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from pathlib import Path

import httpx
import pytest
import shapely
from pyproj import Transformer
from shapely.geometry import box, mapping

from resilink.domain.config import TerritoryConfig
from resilink.etl import connectors as conn_mod
from resilink.etl.contracts import load_sources
from resilink.etl.snapshot import fetch_snapshot
from tests.osm_pbf_writer import write_pbf

TERR = TerritoryConfig(id="tst", name="Test", selector={"departement": ["13"], "cog_year": 2026}, buffer_m=2000.0,
                       crs_metric="EPSG:2154", grid={"id": "insee200m", "crs": "EPSG:3035", "size_m": 200})
X0, Y0 = 890_000.0, 6_240_000.0  # Lambert-93, east of Marseille
DEPT = box(X0, Y0, X0 + 10_000, Y0 + 10_000)
DEPTS = {"13": ("Bouches-du-Rhône", "93", DEPT), "84": ("Vaucluse", "93", box(X0 + 10_000, Y0, X0 + 30_000, Y0 + 10_000)),
         "05": ("Hautes-Alpes", "93", box(X0 + 60_000, Y0, X0 + 70_000, Y0 + 10_000))}  # 05: outside the buffer
COMMUNES = {  # code: (département, population, geometry)
    "13001": ("13", 1000, box(X0, Y0, X0 + 5_000, Y0 + 10_000)),
    "13002": ("13", 2000, box(X0 + 5_000, Y0, X0 + 10_000, Y0 + 10_000)),
    "84001": ("84", 500, box(X0 + 10_000, Y0, X0 + 15_000, Y0 + 10_000)),  # meets the 2 km buffer
    "84002": ("84", 700, box(X0 + 20_000, Y0, X0 + 25_000, Y0 + 10_000)),  # 10 km away: dropped
}
XSD = """<?xml version="1.0" encoding="UTF-8"?>
<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:gml="http://www.opengis.net/gml/3.2"
  xmlns:x="urn:x" targetNamespace="urn:x"><xsd:complexType name="communeType"><xsd:complexContent>
  <xsd:extension base="gml:AbstractFeatureType"><xsd:sequence>
  <xsd:element name="code_insee" nillable="true" type="xsd:string"/>
  <xsd:element name="population" nillable="true" type="xsd:int"/>
  <xsd:element name="geometrie" nillable="true" type="gml:MultiSurfacePropertyType"/>
  </xsd:sequence></xsd:extension></xsd:complexContent></xsd:complexType>
  <xsd:element name="commune" substitutionGroup="gml:AbstractFeature" type="x:communeType"/></xsd:schema>"""
CRS_2154 = {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::2154"}}
HEADER = "idcar_200m,idcar_1km,idcar_nat,i_est_200,i_est_1km,lcog_geo,ind,men,ind_80p"
TO_WGS84 = Transformer.from_crs(2154, 4326, always_xy=True)


def lonlat(x: float, y: float) -> tuple[float, float]:
    lon, lat = TO_WGS84.transform(x, y)
    return round(lon, 7), round(lat, 7)


def wfs_features() -> tuple[list[dict], list[dict]]:
    deps = [{"type": "Feature", "geometry": mapping(g), "properties": {
        "code_insee": code, "nom_officiel": name, "code_insee_de_la_region": reg}}
        for code, (name, reg, g) in DEPTS.items()]
    communes = [{"type": "Feature", "geometry": mapping(geom), "properties": {
        "code_insee": code, "nom_officiel": f"Commune {code}", "statut": "Commune simple", "population": pop,
        "date_du_recensement": "2023-01-01Z", "code_insee_du_departement": dep}}
        for code, (dep, pop, geom) in COMMUNES.items()]
    return deps, communes


def cells_csv(drop_column: str | None = None) -> str:
    """Inhabited 200 m cells (EPSG:3035) over the fixture area; lcog_geo = commune holding the centre."""
    to3035 = Transformer.from_crs(2154, 3035, always_xy=True)
    to2154 = Transformer.from_crs(3035, 2154, always_xy=True)
    xs, ys = to3035.transform([X0 - 3_000, X0 + 16_000], [Y0 - 3_000, Y0 + 13_000])
    rows = []
    for e in range(int(min(xs)) // 200 * 200, int(max(xs)), 200):
        for n in range(int(min(ys)) // 200 * 200, int(max(ys)), 200):
            cx, cy = to2154.transform(e + 100, n + 100)
            code = next((c for c, (_, _, g) in COMMUNES.items() if g.contains(shapely.Point(cx, cy))), "84002")
            rows.append([f"CRS3035RES200mN{n}E{e}", f"CRS3035RES1000mN{n // 1000 * 1000}E{e // 1000 * 1000}",
                         f"CRS3035RES2000mN{n // 2000 * 2000}E{e // 2000 * 2000}", str((e // 200) % 2), "0", code,
                         "1", "0.4", "0.1"])
    cols = HEADER.split(",")
    keep = [i for i, c in enumerate(cols) if c != drop_column]
    return "\n".join([",".join(cols[i] for i in keep)] + [",".join(r[i] for i in keep) for r in rows]) + "\n"


def cells_zip(drop_column: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("carreaux_200m_met.csv", cells_csv(drop_column))
        z.writestr("carreaux_200m_mart.csv", HEADER + "\nCRS5490RES200mN1592600E728800,x,x,1,1,97226,1,0.6,0\n")
        z.writestr("carreaux_200m_reun.csv", HEADER + "\nCRS2975RES200mN7634200E359400,x,x,1,1,97412,19,6.9,0\n")
    return buf.getvalue()


# ------------------------------------------------------------------------------------------------ electricity
def _point(fid: str, x: float, y: float, commune: str) -> dict:
    return {"type": "Feature", "id": fid, "geometry": {"type": "Point", "coordinates": list(lonlat(x, y))},
            "properties": {"_id": fid, "code_commune": commune, "code_departement": commune[:2]}}


def _line(fid: str, xy: list[tuple[float, float]], commune: str) -> dict:
    return {"type": "Feature", "id": fid, "geometry": {"type": "LineString", "coordinates": [list(lonlat(*p)) for p in xy]},
            "properties": {"_id": fid, "code_commune": commune, "code_departement": commune[:2]}}


ENEDIS = {
    "poste-electrique": [
        _point("s-core", X0 + 2_000, Y0 + 2_000, "13001"),
        _point("s-core-dup", X0 + 2_000.2, Y0 + 2_000.3, "13001"),  # same position at 1 m: one substation
        _point("s-buffer", X0 + 11_000, Y0 + 5_000, "84001"),
        _point("s-out", X0 + 22_000, Y0 + 5_000, "84002"),  # outside the buffer
    ],
    "reseau-hta": [
        _line("a-core", [(X0 + 1_000, Y0 + 1_000), (X0 + 1_500, Y0 + 1_000)], "13001"),
        _line("a-out", [(X0 + 22_000, Y0 + 1_000), (X0 + 23_000, Y0 + 1_000)], "84002"),
    ],
    "reseau-souterrain-hta": [
        _line("u-core", [(X0 + 3_000, Y0 + 3_000), (X0 + 3_000, Y0 + 3_400)], "13001"),
    ],
}
ENEDIS_SCHEMA = [{"key": k, "type": "string"} for k in ("_id", "code_commune", "code_departement")] + [
    {"key": "geometry", "type": "string", "x-refersTo": "https://purl.org/geojson/vocab#geometry"}]
RTE_CSV = ("﻿code_poste;nom_poste;fonction;etat;tension;departement\n"
           "AAAAA;ALPHA;Poste de transformation;EN EXPLOITATION;225kV;Bouches-du-Rhône\n"
           "BBBBB;BRAVO;Poste de transformation;EN EXPLOITATION;63kV;Vaucluse\n"
           "CCCCC;CHARLIE;Poste de transformation;EN EXPLOITATION;400kV;Savoie\n"
           "DDDDD;DELTA;Point de piquage;EN EXPLOITATION;<45kV;BOUCHES-DU-RHONE\n"
           "EEEEE;ECHO;Poste de transformation;EN EXPLOITATION;63kV;Bouches-du-Rhône\n")
RTE_FIELDS = [{"name": n, "type": "text"} for n in ("code_poste", "nom_poste", "fonction", "etat", "tension", "departement")]


def osm_pbfs(tmp: Path) -> dict[str, bytes]:
    """Two overlapping extracts: the 225 kV line (way 10) is in both and must be kept once."""
    def node(i: int, x: float, y: float, tags: dict | None = None) -> tuple[int, float, float, dict]:
        lon, lat = lonlat(x, y)
        return (i, lat, lon, tags or {})

    shared = [node(1, X0 + 1_000, Y0 + 5_000), node(2, X0 + 9_000, Y0 + 5_000)]
    paca_nodes = [
        *shared,
        node(3, X0 + 2_000, Y0 + 8_000), node(4, X0 + 4_000, Y0 + 8_000),  # 20 kV minor line: dropped
        node(5, X0 + 5_000, Y0 + 1_000), node(6, X0 + 5_000, Y0 + 2_000),  # power=line, no voltage: kept
        node(7, X0 + 6_000, Y0 + 6_000, {"power": "substation", "voltage": "225000;63000", "ref:FR:RTE": "AAAAA",
                                         "name": "Alpha", "substation": "transmission"}),
        node(8, X0 + 7_000, Y0 + 9_000, {"power": "substation", "voltage": "20000;400",
                                         "substation": "minor_distribution"}),  # dropped
        node(20, X0 + 11_000, Y0 + 2_000), node(21, X0 + 11_100, Y0 + 2_000), node(22, X0 + 11_100, Y0 + 2_100),
        node(23, X0 + 11_000, Y0 + 2_100),  # 63 kV substation polygon in the buffer
    ]
    paca_ways = [(10, [1, 2], {"power": "line", "voltage": "225000", "circuits": "2"}),
                 (11, [3, 4], {"power": "minor_line", "voltage": "20000"}),
                 (12, [5, 6], {"power": "line"}),
                 (13, [20, 21, 22, 23, 20], {"power": "substation", "voltage": "63000", "ref:FR:RTE": "BBBBB"})]
    lr_nodes = [*shared, node(30, X0 + 40000, Y0 + 5000), node(31, X0 + 45000, Y0 + 5000)]
    lr_ways = [(10, [1, 2], {"power": "line", "voltage": "225000", "circuits": "2"}),
               (14, [30, 31], {"power": "line", "voltage": "400000"})]  # outside the buffer
    return {"paca": write_pbf(tmp / "paca.pbf", paca_nodes, paca_ways).read_bytes(),
            "languedoc-roussillon": write_pbf(tmp / "lr.pbf", lr_nodes, lr_ways).read_bytes()}


class Portal:
    """Answers the connectors' requests; ``seen`` records query parameters, ``files`` the static downloads."""

    def __init__(self, tmp: Path) -> None:
        self.seen: list[dict[str, str]] = []
        self.zip = cells_zip()
        self.pbf = osm_pbfs(tmp)
        self.md5_override: str | None = None
        self.deps, self.communes = wfs_features()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, q = request.url.path, dict(request.url.params)
        self.seen.append({"path": path, **q})
        if path.endswith(".zip"):
            return httpx.Response(200, content=self.zip)
        if ".osm.pbf" in path:
            region = "paca" if "provence" in path else "languedoc-roussillon"
            data = self.pbf[region]
            if path.endswith(".md5"):
                md5 = self.md5_override or hashlib.md5(data, usedforsecurity=False).hexdigest()
                return httpx.Response(200, text=f"{md5}  {path.rsplit('/', 1)[-1][:-4]}\n")
            return httpx.Response(200, content=data)
        if "/data-fair/api/v1/datasets/" in path:
            return self._datafair(request, path, q)
        if "/api/explore/v2.1/catalog/datasets/postes-electriques-rte" in path:
            if path.endswith("/exports/csv"):
                return httpx.Response(200, content=RTE_CSV.encode("utf-8"))
            return httpx.Response(200, json={"metas": {"default": {"title": "Sites RTE", "license": "Licence Ouverte v2.0",
                                                                    "records_count": 5}}, "fields": RTE_FIELDS})
        return self._wfs(q)

    def _datafair(self, request: httpx.Request, path: str, q: dict[str, str]) -> httpx.Response:
        slug = path.split("/datasets/")[1].split("/")[0]
        feats = ENEDIS[slug]
        if not path.endswith("/lines"):
            return httpx.Response(200, json={"title": slug, "count": len(feats), "schema": ENEDIS_SCHEMA,
                                             "license": {"title": "Licence Ouverte / Open Licence version 2.0"}})
        size, after = int(q["size"]), int(q.get("after", 0))
        page = feats[after:after + size]
        headers = {}
        if after + size < len(feats):  # Data Fair puts the GeoJSON next link in the Link header
            nxt = request.url.copy_merge_params({"after": str(after + size)})
            headers["Link"] = f'<{nxt}>; rel="next"'
        return httpx.Response(200, headers=headers, json={"type": "FeatureCollection", "total": len(feats),
                                                          "features": page})

    def _wfs(self, q: dict[str, str]) -> httpx.Response:
        if q["REQUEST"] == "DescribeFeatureType":
            return httpx.Response(200, content=XSD.encode())
        feats = self.deps if q["TYPENAMES"].endswith(":departement") else self.communes
        if q.get("RESULTTYPE") == "hits":
            return httpx.Response(200, text=f'<wfs:FeatureCollection numberMatched="{len(feats)}"/>')
        if "CQL_FILTER" in q:
            wanted = set(re.findall(r"'(\w+)'", q["CQL_FILTER"]))
            feats = [f for f in feats if f["properties"]["code_insee"] in wanted]
        elif "BBOX" in q:
            x0, y0, x1, y1 = (float(v) for v in q["BBOX"].split(",")[:4])
            window = box(x0, y0, x1, y1)
            feats = [f for f in feats if shapely.geometry.shape(f["geometry"]).intersects(window)]
        start, count = int(q["STARTINDEX"]), int(q["COUNT"])
        return httpx.Response(200, json={"type": "FeatureCollection", "crs": CRS_2154,
                                         "features": feats[start:start + count]})


def make_portal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Portal:
    """Route the connectors' HTTP client to a fresh mock portal (used by the ``portal`` fixture in conftest)."""
    p = Portal(tmp_path)
    monkeypatch.setattr(conn_mod, "_client", lambda: httpx.Client(transport=httpx.MockTransport(p.handler)))
    monkeypatch.setattr(conn_mod.WfsConnector, "PAGE", 2)  # exercise paging
    monkeypatch.setattr(conn_mod.DataFairConnector, "PAGE", 1)
    return p


def fetch_all(root: Path, source_ids: tuple[str, ...]) -> None:
    specs = load_sources()
    for sid in source_ids:
        fetch_snapshot(specs[sid], TERR, root=root)


TERRITORY_SOURCES = ("ign.admin_express.departement", "ign.admin_express.commune", "insee.filosofi2021_200m")
ELECTRICITY_SOURCES = ("rte.postes", "enedis.poste_electrique", "enedis.reseau_hta", "enedis.reseau_souterrain_hta",
                       "osm.geofabrik.paca", "osm.geofabrik.languedoc_roussillon")
