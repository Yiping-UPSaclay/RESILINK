"""Generative-UI contract (blueprint §1.9): ViewSpec + deterministic validation and value resolution.

Agents describe *what* to show; values are always resolved from stored results or recorded op outputs via
``ValueRef`` (invariant I1). P0 charts use a small typed grammar (bar / line / table); the constrained
Vega-Lite subset is planned for P2.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field

from resilink.io.paths import load_yaml
from resilink.io.results import ResultsStore
from resilink.knowledge.store import KnowledgeStore, ValueRef

MAP_LAYERS = ("infrastructure_nodes", "infrastructure_edges", "population_grid", "flood_footprints", "communes")
MAX_ROWS = 500


class MapLayerSpec(BaseModel):
    layer_id: Literal["infrastructure_nodes", "infrastructure_edges", "population_grid", "flood_footprints", "communes"]
    color_by: ValueRef | None = Field(None, description="Values joined to features by rid (nodes) or cell_id (grid)")
    scenario_id: str | None = Field(None, description="Filter for flood_footprints")


class ChartSpec(BaseModel):
    kind: Literal["bar", "line", "table"]
    title: str
    data: ValueRef
    x: str
    y: str


class ViewSpec(BaseModel):
    title: str
    release: str = "synthetic-2026.11.0"
    narrative_md: str = ""
    map_layers: list[MapLayerSpec] = Field(default_factory=list)
    charts: list[ChartSpec] = Field(default_factory=list)


def resolve(ref: ValueRef, data: Path | None = None, knowledge: KnowledgeStore | None = None) -> pd.DataFrame:
    """ValueRef -> DataFrame. Raises ValueError when the reference cannot be resolved."""
    if ref.call_id:
        ks = knowledge or KnowledgeStore(data / "knowledge" / "knowledge.duckdb" if data else None)
        rec = ks.get_call(ref.call_id)
        if rec is None:
            raise ValueError(f"unknown call_id {ref.call_id}")
        payload = rec["output"]
        rows = next((v for v in payload.values() if isinstance(v, list)), [payload])
        df = pd.DataFrame(rows)
    elif ref.run_id and ref.table:
        store = ResultsStore(data)
        if not store.has_table(ref.run_id, ref.table):
            raise ValueError(f"run {ref.run_id} has no table {ref.table}")
        df = store.read(ref.run_id, ref.table)
    else:
        raise ValueError("ValueRef needs call_id, or run_id + table")
    for col, val in ref.filters.items():
        if col not in df.columns:
            raise ValueError(f"filter column {col!r} not in {list(df.columns)}")
        df = df[df[col].astype(str) == str(val)]
    if ref.column:
        if ref.column not in df.columns:
            raise ValueError(f"column {ref.column!r} not in {list(df.columns)}")
        if ref.agg != "none":
            v = getattr(df[ref.column], ref.agg)() if ref.agg != "count" else len(df)
            return pd.DataFrame({ref.column: [v]})
    return df


def required_tier(ref: ValueRef, data: Path | None = None) -> str:
    if ref.table:
        tier = load_yaml("configs/layers.yaml")["tables"].get(ref.table, {}).get("tier", "internal")
        if ref.run_id:
            release = ResultsStore(data).manifest(ref.run_id)["release_id"]
            if release.startswith("synthetic-"):
                return "public"
        return tier
    return "internal"


def validate_view(spec: ViewSpec, data: Path | None = None, knowledge: KnowledgeStore | None = None) -> tuple[list[str], dict[str, Any]]:
    """Return (errors, resolved preview). Checks layer ids, ValueRef resolvability, columns, size caps."""
    errors: list[str] = []
    preview: dict[str, Any] = {}
    for i, layer in enumerate(spec.map_layers):
        if layer.color_by is not None:
            try:
                df = resolve(layer.color_by, data, knowledge)
                preview[f"map_layer_{i}"] = {"rows": len(df), "column": layer.color_by.column}
            except ValueError as exc:
                errors.append(f"map_layers[{i}]: {exc}")
    for i, chart in enumerate(spec.charts):
        try:
            df = resolve(chart.data, data, knowledge)
            missing = [c for c in (chart.x, chart.y) if c not in df.columns]
            if missing:
                errors.append(f"charts[{i}]: columns {missing} not in data {list(df.columns)}")
            elif len(df) > MAX_ROWS:
                errors.append(f"charts[{i}]: {len(df)} rows exceed cap {MAX_ROWS}; add filters")
            else:
                preview[f"chart_{i}"] = json.loads(df[[chart.x, chart.y]].to_json(orient="records"))
        except ValueError as exc:
            errors.append(f"charts[{i}]: {exc}")
    return errors, preview
