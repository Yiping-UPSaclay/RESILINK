"""Connectors for the two portal technologies in the pilot (both verified 2026-11-16):

* Opendatasoft Explore API v2.1 — ODRÉ (RTE, NaTran, Teréga, registre): ``/api/explore/v2.1/catalog/datasets/{id}``
  and ``.../exports/{format}`` (no record cap on exports).
* Data Fair — Enedis open data portal (migrated from Opendatasoft; the old v2.1 paths now return 410):
  ``/data-fair/api/v1/datasets/{id}`` (schema, count, licence) and ``.../lines`` (paginated, ``qs`` filter).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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


CONNECTORS: dict[str, Any] = {"opendatasoft": OpendatasoftConnector(), "datafair": DataFairConnector()}
