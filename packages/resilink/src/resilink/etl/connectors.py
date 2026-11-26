"""Connectors for the portal technologies of the pilot.

Verified 2026-11-16:

* Opendatasoft Explore API v2.1 — ODRÉ (RTE, NaTran, Teréga, registre): ``/api/explore/v2.1/catalog/datasets/{id}``
  and ``.../exports/{format}`` (no record cap on exports).
* Data Fair — Enedis open data portal (migrated from Opendatasoft; the old v2.1 paths now return 410):
  ``/data-fair/api/v1/datasets/{id}`` (schema, count, licence) and ``.../lines`` (paginated, ``qs`` filter).

Verified 2026-11-26 (real-data track, D1):

* OGC WFS 2.0 — IGN Géoplateforme (``https://data.geopf.fr/wfs/ows``, GeoServer): DescribeFeatureType for the
  schema, ``RESULTTYPE=hits`` for the count, GetFeature as GeoJSON in the territory's metric CRS, paged with
  ``COUNT``/``STARTINDEX`` (server default page 5000) and a stable ``SORTBY``.
* Static files — publisher downloads such as INSEE's zip archives. insee.fr sends no Content-Length,
  Last-Modified, ETag or range support, so the schema is read from the downloaded file (``inspect``) and the
  drift gate runs on it before the snapshot is published.
"""

from __future__ import annotations

import fnmatch
import io
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import httpx

from resilink.domain.config import TerritoryConfig
from resilink.etl.contracts import FieldInfo, SourceMetadata, SourceSpec

USER_AGENT = "resilink-lab/0.1 (research; open-data client)"
GEO_TYPES = {"geo_shape", "geo_point_2d"}


def _client() -> httpx.Client:
    return httpx.Client(timeout=120, follow_redirects=True, headers={"User-Agent": USER_AGENT})


def _departements(territory: TerritoryConfig | None, spec: SourceSpec) -> list[str]:
    if territory is None:
        return []
    key = "departement_names" if spec.territory_value == "name" else "departement"
    return [str(c) for c in territory.selector.get(key, [])]


class OpendatasoftConnector:
    def describe(self, spec: SourceSpec) -> SourceMetadata:
        with _client() as c:
            r = c.get(f"{spec.base_url.rstrip('/')}/api/explore/v2.1/catalog/datasets/{spec.dataset}")
            r.raise_for_status()
            j = r.json()
        meta = j.get("metas", {}).get("default", {})
        fields = [FieldInfo(name=f["name"], type=f["type"]) for f in j.get("fields", [])]
        return SourceMetadata(
            title=meta.get("title", spec.dataset), modified=meta.get("modified"), licence=meta.get("license"),
            count=meta.get("records_count"), fields=fields,
            has_geometry=any(f.type in GEO_TYPES for f in fields), raw={"metas": meta, "fields": j.get("fields", [])},
        )

    def fetch(self, spec: SourceSpec, territory: TerritoryConfig | None, dest: Path,
              limit: int | None = None) -> tuple[str, int | None]:
        params: dict[str, Any] = {}
        deps = _departements(territory, spec)
        if deps and spec.territory_field:
            params["where"] = " OR ".join(f'{spec.territory_field}="{d}"' for d in deps)
        if limit:
            params["limit"] = limit
        url = f"{spec.base_url.rstrip('/')}/api/explore/v2.1/catalog/datasets/{spec.dataset}/exports/{spec.format}"
        with _client() as c, c.stream("GET", url, params=params) as r:
            r.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in r.iter_bytes():
                    fh.write(chunk)
            return str(r.url), None


class DataFairConnector:
    PAGE = 10_000

    def describe(self, spec: SourceSpec) -> SourceMetadata:
        with _client() as c:
            r = c.get(f"{spec.base_url.rstrip('/')}/data-fair/api/v1/datasets/{spec.dataset}")
            r.raise_for_status()
            j = r.json()
        fields = [FieldInfo(name=f["key"], type=f.get("type", "")) for f in j.get("schema", [])]
        geo = any(f.get("x-refersTo") == "https://purl.org/geojson/vocab#geometry" for f in j.get("schema", []))
        return SourceMetadata(
            title=j.get("title", spec.dataset), modified=j.get("dataUpdatedAt") or j.get("updatedAt"),
            licence=(j.get("license") or {}).get("title"), count=j.get("count"), fields=fields, has_geometry=geo,
            raw={k: j.get(k) for k in ("title", "updatedAt", "dataUpdatedAt", "count", "license", "bbox")},
        )

    def fetch(self, spec: SourceSpec, territory: TerritoryConfig | None, dest: Path,
              limit: int | None = None) -> tuple[str, int | None]:
        """Page through ``/lines`` as GeoJSON and write a single FeatureCollection (filtered by département)."""
        base = f"{spec.base_url.rstrip('/')}/data-fair/api/v1/datasets/{spec.dataset}/lines"
        params: dict[str, Any] = {"format": "geojson", "size": min(self.PAGE, limit or self.PAGE)}
        deps = _departements(territory, spec)
        if deps and spec.territory_field:
            params["qs"] = " OR ".join(f'{spec.territory_field}:"{d}"' for d in deps)
        features: list[dict[str, Any]] = []
        url: str | None = base
        first_url = ""
        with _client() as c:
            while url:
                r = c.get(url, params=params if url == base else None)
                r.raise_for_status()
                first_url = first_url or str(r.url)
                page = r.json()
                features.extend(page.get("features", []))
                url = page.get("next")
                if limit and len(features) >= limit:
                    features = features[:limit]
                    break
        dest.write_text(json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False), encoding="utf-8")
        return first_url, len(features)


XSD = "{http://www.w3.org/2001/XMLSchema}"


def _xsd_fields(xml: bytes) -> list[FieldInfo]:
    """Properties of a WFS DescribeFeatureType answer: the elements of each complex type's sequence."""
    root = ElementTree.fromstring(xml)
    return [FieldInfo(name=el.get("name", ""), type=el.get("type", ""))
            for ctype in root.iter(f"{XSD}complexType") for el in ctype.iter(f"{XSD}element") if el.get("name")]


def _native_key(spec: SourceSpec) -> str | None:
    """``native:<field>`` id strategies name a unique field, used as the stable WFS paging order."""
    return spec.id_strategy.split(":", 1)[1] if spec.id_strategy.startswith("native:") else None


def _in_list(field: str, values: list[str]) -> str:
    return f"{field} IN ({', '.join(repr(v) for v in values)})"


class WfsConnector:
    PAGE = 1000
    VERSION = "2.0.0"

    def _params(self, request: str, **extra: Any) -> dict[str, Any]:
        return {"SERVICE": "WFS", "VERSION": self.VERSION, "REQUEST": request, **extra}

    def describe(self, spec: SourceSpec) -> SourceMetadata:
        with _client() as c:
            r = c.get(spec.base_url, params=self._params("DescribeFeatureType", TYPENAMES=spec.dataset))
            r.raise_for_status()
            fields = _xsd_fields(r.content)
            h = c.get(spec.base_url, params=self._params("GetFeature", TYPENAMES=spec.dataset, RESULTTYPE="hits"))
            h.raise_for_status()
        m = re.search(r'numberMatched="(\d+)"', h.text)
        # WFS gives no licence or modification date per feature type: the licence is the register's, and the
        # edition is pinned by the feature type's name (vintage).
        return SourceMetadata(
            title=spec.dataset, modified=spec.vintage, licence=spec.licence, count=int(m.group(1)) if m else None,
            fields=fields, has_geometry=any(f.type.startswith("gml:") for f in fields),
            raw={"feature_type": spec.dataset, "vintage": spec.vintage},
        )

    def _get_all(self, c: httpx.Client, base_url: str, typename: str, srs: str, *, sort_by: str | None,
                 limit: int | None = None, **filters: Any) -> tuple[str, Any, list[dict[str, Any]]]:
        params = self._params("GetFeature", TYPENAMES=typename, OUTPUTFORMAT="application/json", SRSNAME=srs,
                              **filters)
        if sort_by:
            params["SORTBY"] = sort_by
        features: list[dict[str, Any]] = []
        first_url, crs = "", None
        while True:
            page = min(self.PAGE, limit - len(features)) if limit else self.PAGE
            r = c.get(base_url, params={**params, "COUNT": page, "STARTINDEX": len(features)})
            r.raise_for_status()
            j = r.json()
            first_url, crs = first_url or str(r.url), crs or j.get("crs")
            got = j.get("features", [])
            features.extend(got)
            if len(got) < page or (limit and len(features) >= limit):
                return first_url, crs, features

    def fetch(self, spec: SourceSpec, territory: TerritoryConfig | None, dest: Path,
              limit: int | None = None) -> tuple[str, int | None]:
        from shapely.geometry import shape  # only to size the territory window

        srs = territory.crs_metric if territory else "EPSG:2154"
        deps = _departements(territory, spec)
        filters: dict[str, Any] = {}
        with _client() as c:
            if spec.territory_mode == "field" and deps and spec.territory_field:
                filters["CQL_FILTER"] = _in_list(spec.territory_field, deps)
            elif spec.territory_mode == "buffer_bbox" and territory is not None:
                if not (spec.territory_layer and spec.territory_field and deps):
                    raise ValueError(f"{spec.id}: buffer_bbox needs territory_layer, territory_field and départements")
                _, _, areas = self._get_all(c, spec.base_url, spec.territory_layer, srs, sort_by=spec.territory_field,
                                            CQL_FILTER=_in_list(spec.territory_field, deps))
                if not areas:
                    raise ValueError(f"{spec.id}: no département {deps} in {spec.territory_layer}")
                b = [shape(f["geometry"]).bounds for f in areas]
                pad = territory.buffer_m
                window = (min(x[0] for x in b) - pad, min(x[1] for x in b) - pad,
                          max(x[2] for x in b) + pad, max(x[3] for x in b) + pad)
                filters["BBOX"] = ",".join(f"{v:.0f}" for v in window) + f",urn:ogc:def:crs:EPSG::{srs.split(':')[-1]}"
            url, crs, features = self._get_all(c, spec.base_url, spec.dataset, srs, sort_by=_native_key(spec),
                                               limit=limit, **filters)
        dest.write_text(json.dumps({"type": "FeatureCollection", "crs": crs, "features": features},
                                   ensure_ascii=False), encoding="utf-8")
        return url, len(features)


class HttpFileConnector:
    """A publisher's static download, stored byte for byte (its sha256 can be checked against anyone's copy)."""

    def _url(self, spec: SourceSpec) -> str:
        return f"{spec.base_url.rstrip('/')}/{spec.dataset.lstrip('/')}"

    def describe(self, spec: SourceSpec) -> SourceMetadata:
        # Nothing can be learnt before downloading (insee.fr answers HEAD without metadata): see `inspect`.
        return SourceMetadata(title=spec.dataset.rsplit("/", 1)[-1], modified=None, licence=spec.licence,
                              fields=[], has_geometry=False, raw={"url": self._url(spec), "inspected": False})

    def fetch(self, spec: SourceSpec, territory: TerritoryConfig | None, dest: Path,
              limit: int | None = None) -> tuple[str, int | None]:
        with _client() as c, c.stream("GET", self._url(spec)) as r:
            r.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
            return str(r.url), None

    def inspect(self, spec: SourceSpec, path: Path, meta: SourceMetadata) -> SourceMetadata:
        """Schema of a zip of CSV files: the header and row count of each member in ``spec.members``.

        With several members, field names are ``<member>/<column>`` so a column dropped from one file is drift."""
        if spec.format != "zip":
            raise ValueError(f"{spec.id}: inspect supports zip archives only")
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            missing = [m for m in spec.members if not any(fnmatch.fnmatch(i.filename, m) for i in infos)]
            if missing:
                raise ValueError(f"{spec.id}: archive lacks members {missing} (has {[i.filename for i in infos]})")
            chosen = [i for i in infos if not spec.members or any(fnmatch.fnmatch(i.filename, m) for m in spec.members)]
            fields: dict[str, FieldInfo] = {}
            rows: dict[str, int] = {}
            for info in chosen:
                with z.open(info) as fh:
                    text = io.TextIOWrapper(fh, encoding="utf-8", newline="")
                    header = text.readline().rstrip("\r\n")
                    rows[info.filename] = sum(1 for line in text if line.strip())
                for name in header.split(","):
                    key = f"{info.filename}/{name}" if len(chosen) > 1 else name
                    fields.setdefault(key, FieldInfo(name=key, type="text"))
            stamps = [datetime(*i.date_time).isoformat() for i in chosen]
        return meta.model_copy(update={
            "fields": list(fields.values()), "count": sum(rows.values()), "modified": max(stamps, default=None),
            "raw": {**meta.raw, "inspected": True, "members": {i.filename: i.file_size for i in infos}, "rows": rows},
        })


CONNECTORS: dict[str, Any] = {"opendatasoft": OpendatasoftConnector(), "datafair": DataFairConnector(),
                              "wfs": WfsConnector(), "http_file": HttpFileConnector()}
