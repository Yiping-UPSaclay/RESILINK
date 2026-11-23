"""Human gate (invariant I2): proposals from agents take effect only after a decision recorded here."""

from __future__ import annotations

import getpass
import json

import streamlit as st

from resilink.domain.vocab import Tier
from resilink.ops import SideEffect, call, load_builtin_ops
from resilink.ops.recording import make_context

st.set_page_config(page_title="RESILINK · Review", layout="wide")
st.title("Review queue")
load_builtin_ops()
human = f"human:{getpass.getuser()}"
ctx = make_context(human, tier=Tier.INTERNAL, autonomy=SideEffect.ADMIN)

status = st.radio("Status", ["proposed", "approved", "rejected"], horizontal=True)
props = call("proposals.list", {"status": status}, ctx).output.proposals
if not props:
    st.info("Nothing here.")
ks = ctx.extras["knowledge"]
allp = ks.list_proposals(status)
for p in props:
    with st.container(border=True):
        st.markdown(f"**{p['title']}** · `{p['kind']}` · by `{p['created_by']}` · {p['created_at']}")
        payload = allp[allp.proposal_id == p["proposal_id"]]["payload_json"].iloc[0]
        st.code(json.dumps(json.loads(payload), indent=2, ensure_ascii=False)[:3000], language="json")
        if status == "proposed":
            note = st.text_input("Note", key=f"note-{p['proposal_id']}")
            a, b = st.columns(2)
            if a.button("Approve", key=f"ok-{p['proposal_id']}", type="primary"):
                call("proposals.decide", {"proposal_id": p["proposal_id"], "approve": True, "note": note}, ctx)
                st.rerun()
            if b.button("Reject", key=f"no-{p['proposal_id']}"):
                call("proposals.decide", {"proposal_id": p["proposal_id"], "approve": False, "note": note}, ctx)
                st.rerun()
