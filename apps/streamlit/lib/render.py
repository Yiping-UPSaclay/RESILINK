"""Shared rendering helpers: ViewSpec -> pydeck + Altair; cached release/geo loaders (UI only, no analytics)."""

from __future__ import annotations

import altair as alt
import geopandas as gpd
import numpy as np
import pandas as pd
import pydeck as pdk
import streamlit as st

from resilink.experiments.runner import get_release
from resilink.views import ViewSpec, resolve

LAYER_COLORS = {"elec": [230, 159, 0], "gas": [0, 114, 178], "telecom": [204, 121, 167], "facility": [120, 120, 120]}


@st.cache_data(show_spinner=False)
def geo(release_id: str, table: str) -> gpd.GeoDataFrame:
    gdf = get_release(release_id).read(table)
    return gdf.to_crs(4326)


def _ramp(values: np.ndarray) -> list[list[int]]:
    v = np.clip(np.nan_to_num(values.astype(float)), 0, None)
    v = v / v.max() if v.max() > 0 else v
    return [[int(255 * x), int(80 * (1 - x)), int(60 + 120 * (1 - x)), 200] for x in v]


def view_state(gdf: gpd.GeoDataFrame) -> pdk.ViewState:
    minx, miny, maxx, maxy = gdf.total_bounds
    return pdk.ViewState(latitude=(miny + maxy) / 2, longitude=(minx + maxx) / 2, zoom=11.3)


def deck_layers(spec: ViewSpec) -> tuple[list[pdk.Layer], gpd.GeoDataFrame]:
    layers: list[pdk.Layer] = []
    anchor = geo(spec.release, "grid_cells")
    for i, ml in enumerate(spec.map_layers):
        values = resolve(ml.color_by) if ml.color_by is not None else None
        col = ml.color_by.column if ml.color_by is not None else None
        if ml.layer_id == "population_grid":
            g = geo(spec.release, "grid_cells")
            if values is not None:
                g = g.merge(values[["cell_id", col]], on="cell_id", how="left")
                g["color"] = _ramp(g[col].to_numpy())
            else:
                g["color"] = _ramp(g["pop"].to_numpy())
            layers.append(pdk.Layer("GeoJsonLayer", id=f"grid-{i}", data=g.__geo_interface__, pickable=True,
                                    stroked=False, filled=True, get_fill_color="properties.color", opacity=0.55))
        elif ml.layer_id == "flood_footprints":
            g = geo(spec.release, "hazard_footprints")
            if ml.scenario_id:
                g = g[g["scenario_id"] == ml.scenario_id]
            layers.append(pdk.Layer("GeoJsonLayer", id=f"flood-{i}", data=g.__geo_interface__, pickable=True,
                                    filled=True, get_fill_color=[30, 120, 220, 70], get_line_color=[30, 120, 220, 160]))
        elif ml.layer_id == "communes":
            g = geo(spec.release, "admin_areas")
            layers.append(pdk.Layer("GeoJsonLayer", id=f"communes-{i}", data=g.__geo_interface__, filled=False,
                                    get_line_color=[90, 90, 90, 160], line_width_min_pixels=1))
        elif ml.layer_id == "infrastructure_edges":
            g = geo(spec.release, "edges")
            g = g.assign(path=[list(map(list, ls.coords)) for ls in g.geometry],
                         color=[LAYER_COLORS.get(lyr, [100, 100, 100]) for lyr in g["layer"]])
            layers.append(pdk.Layer("PathLayer", id=f"edges-{i}", data=pd.DataFrame(g.drop(columns="geometry")),
                                    get_path="path", get_color="color", width_min_pixels=2, pickable=True))
        elif ml.layer_id == "infrastructure_nodes":
            g = geo(spec.release, "nodes")
            df = pd.DataFrame({"rid": g["rid"], "name": g["name"], "layer": g["layer"], "class": g["class"],
                               "lon": g.geometry.x, "lat": g.geometry.y})
            if values is not None and "rid" in values.columns:
                df = df.merge(values[["rid", col]], on="rid", how="left")
                df["color"] = _ramp(df[col].to_numpy())
            else:
                df["color"] = [[*LAYER_COLORS.get(lyr, [100, 100, 100]), 220] for lyr in df["layer"]]
            layers.append(pdk.Layer("ScatterplotLayer", id=f"nodes-{i}", data=df, get_position=["lon", "lat"],
                                    get_fill_color="color", get_radius=90, radius_min_pixels=4, pickable=True))
    return layers, anchor


def render_view(spec: ViewSpec, key: str) -> None:
    st.markdown(f"#### {spec.title}")
    if spec.narrative_md:
        st.markdown(spec.narrative_md)
    if spec.map_layers:
        layers, anchor = deck_layers(spec)
        st.pydeck_chart(pdk.Deck(layers=layers, initial_view_state=view_state(anchor), map_provider=None,
                                 tooltip={"text": "{name} {rid} {cell_id}"}), key=f"deck-{key}")
    for j, ch in enumerate(spec.charts):
        df = resolve(ch.data)
        if ch.kind == "table":
            st.dataframe(df[[ch.x, ch.y]], hide_index=True)
            continue
        mark = alt.Chart(df).mark_bar() if ch.kind == "bar" else alt.Chart(df).mark_line(point=True)
        st.altair_chart(mark.encode(x=alt.X(ch.x, sort=None), y=ch.y, tooltip=[ch.x, ch.y]).properties(title=ch.title),
                        key=f"chart-{key}-{j}")
