"""Ops console: forms generated from the Operation Registry (capability parity with agents, invariant I7)."""

from __future__ import annotations

import getpass
import json

import streamlit as st

from resilink.domain.vocab import Tier
from resilink.ops import REGISTRY, SideEffect, call, load_builtin_ops
from resilink.ops.recording import make_context

st.set_page_config(page_title="RESILINK · Ops console", layout="wide")
st.title("Ops console")
st.caption("The same typed operations the agents and MCP clients use — no AI required.")
load_builtin_ops()
clearance = st.sidebar.selectbox("Clearance", ["public", "internal", "restricted"], index=1)
ctx = make_context(f"human:{getpass.getuser()}", tier=Tier.parse(clearance), autonomy=SideEffect.PROPOSE)

specs = REGISTRY.allowed(max_side_effect=SideEffect.PROPOSE, tier=Tier.parse(clearance))
name = st.selectbox("Operation", [s.name for s in specs])
spec = REGISTRY.get(name)
st.markdown(f"*{spec.side_effect.label} · min tier {spec.min_tier.label} · cost {spec.cost_hint}* — {spec.summary}")
with st.expander("Input schema"):
    st.json(spec.input_json_schema(strict=False))
example = {k: f.default for k, f in spec.input_model.model_fields.items()
           if not f.is_required() and isinstance(f.default, str | int | float | bool)}
example |= {k: "" for k, f in spec.input_model.model_fields.items() if f.is_required()}
payload = st.text_area("Input (JSON)", json.dumps(example, indent=2), height=180)
if st.button("Call", type="primary"):
    try:
        res = call(name, json.loads(payload), ctx)
        st.success(f"call_id `{res.call_id}` · {res.elapsed_s}s")
        st.json(res.output.model_dump(mode="json"))
    except Exception as exc:
        st.error(f"{type(exc).__name__}: {exc}")
