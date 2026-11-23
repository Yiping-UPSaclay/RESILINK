"""Canonical view: risk results of a run (metrics, exceedance curves, disruption map)."""

from __future__ import annotations

import altair as alt
import streamlit as st

from lib.render import render_view
from resilink.io.results import ResultsStore
from resilink.knowledge.store import ValueRef
from resilink.views import MapLayerSpec, ViewSpec

st.set_page_config(page_title="RESILINK · Risk", layout="wide")
st.title("Risk")
store = ResultsStore()
runs = store.list_runs()
if not runs:
    st.info("No runs yet. Run `uv run resilink demo` (or ask the Copilot / Ops console to submit an experiment).")
    st.stop()

run_id = st.selectbox("Run", [r["run_id"] for r in runs], format_func=lambda r: f"{r} · {next(x['experiment'] for x in runs if x['run_id'] == r)}")
manifest = store.manifest(run_id)
metrics = store.read(run_id, "metrics")
st.caption(f"release `{manifest['release_id']}` · {manifest['cascade_simulations']} simulations · seed entropy "
           f"{manifest['seed_entropy']} · tier `{manifest['tier']}`")

eal = metrics[(metrics.metric == "eal")].set_index("sector")
cols = st.columns(len(eal))
for c, (sector, row) in zip(cols, eal.iterrows(), strict=True):
    c.metric(f"EAL · {sector}", f"{row.value:,.0f}", help=f"people-hours/year; 5–95 %: {row.p05:,.0f} – {row.p95:,.0f}")

losses = store.read(run_id, "sample_losses")
sector = st.radio("Sector", ["all", "elec", "gas", "telecom"], horizontal=True)
lf = (losses.groupby(["scenario_id", "o", "i"], as_index=False)[["loss_total", "loss_direct"]].sum()
      if sector == "all" else losses[losses.sector == sector])
lf = lf.sort_values("loss_total", ascending=False).copy()
lf["exceedance"] = lf.groupby("scenario_id").cumcount().add(1) / lf.groupby("scenario_id")["loss_total"].transform("size")
st.altair_chart(alt.Chart(lf).mark_line().encode(
    x=alt.X("loss_total", title="people-hours lost (conditional on event)"),
    y=alt.Y("exceedance", title="P(loss > x | event)"), color="scenario_id"), key="exceed")

st.dataframe(metrics[metrics.sector == sector].drop(columns=["sector"]), hide_index=True)

scenario = st.selectbox("Scenario for map", sorted(s for s in metrics.scenario_id.unique() if s != "annual"))
map_sector = sector if sector != "all" else "elec"
render_view(ViewSpec(
    title=f"P(disruption > threshold) · {map_sector} · {scenario}", release=manifest["release_id"],
    map_layers=[
        MapLayerSpec(layer_id="population_grid", color_by=ValueRef(run_id=run_id, table="cell_stats", column="p_disruption",
                                                                    filters={"scenario_id": scenario, "sector": map_sector})),
        MapLayerSpec(layer_id="flood_footprints", scenario_id=scenario),
        MapLayerSpec(layer_id="infrastructure_edges"),
        MapLayerSpec(layer_id="infrastructure_nodes"),
    ]), key="riskmap")
