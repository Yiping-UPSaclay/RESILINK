"""RESILINK Lab Copilot (home). AI-native entry point: intent -> tools -> grounded answer + generated views.
With AI off (RESILINK_AI=off or no credentials) the same capabilities are available in the Ops console page."""

from __future__ import annotations

import json
import os

import streamlit as st

from resilink.domain.vocab import Tier
from resilink_ai.policy import ai_enabled

st.set_page_config(page_title="RESILINK Lab", page_icon=":material/hub:", layout="wide")
st.title("RESILINK — Lab Copilot")
st.caption("Digital lab for risk & resilience of interdependent infrastructures. Every number comes from the "
           "deterministic core; answers are grounding-checked and fully traced.")

with st.sidebar:
    clearance = st.selectbox("Your clearance", ["public", "internal", "restricted"], index=1)
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY")) or os.environ.get("RESILINK_AI_FORCE") == "1"
    st.markdown(f"**AI**: {'on' if ai_enabled() and has_key else 'off'}")
    if not has_key:
        st.info("No Anthropic credential found. Set ANTHROPIC_API_KEY (or use the Ops console page — "
                "the lab is fully usable without AI).")

if not (ai_enabled() and has_key):
    st.warning("AI is off. Use **Ops console** for the same operations, **Risk** for results, **Review** for proposals.")
    st.stop()

from lib.render import render_view  # noqa: E402  (import only when AI is on)
from resilink_ai.gateway import AgentSession  # noqa: E402

if "session" not in st.session_state or st.session_state.get("clearance") != clearance:
    st.session_state.session = AgentSession("copilot", user_clearance=Tier.parse(clearance), principal="agent:copilot")
    st.session_state.clearance = clearance
    st.session_state.turns = []

session: AgentSession = st.session_state.session
st.sidebar.markdown(f"Session tier: `{session.tier.label}` · provider `{session.provider.name}` · "
                    f"model `{session.role.model}` · spent ${session.spent_usd:.3f}")

for n, (question, turn) in enumerate(st.session_state.turns):
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        st.markdown(turn.text)
        g = turn.grounding
        if g.ok:
            st.success(f"Grounded: {g.checked} numbers checked against tool outputs", icon=":material/verified:")
        else:
            st.error(f"Ungrounded numbers: {', '.join(g.ungrounded)} — do not rely on them", icon=":material/warning:")
        for k, view in enumerate(turn.views):
            render_view(view, key=f"{n}-{k}")
        with st.expander(f"Trace: {len(turn.tool_calls)} tool calls · ${turn.cost_usd:.4f} · {turn.provenance_id}"):
            for c in turn.tool_calls:
                st.markdown(f"- `{c.op}` → `{c.call_id or 'error'}` {'' if c.ok else '— ' + (c.error or '')}")
                st.code(json.dumps(c.input, ensure_ascii=False)[:1500], language="json")

if prompt := st.chat_input("Ask the lab, e.g. 'Which mobile sites fail most often in the 100-year flood, and why?'"):
    with st.spinner("Working with the lab tools…"):
        turn = session.ask(prompt)
    st.session_state.turns.append((prompt, turn))
    st.rerun()
