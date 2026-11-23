# ADR-0003 — UI-agnostic core; Streamlit MVP, then FastAPI + React + MapLibre (+ deck.gl)
Status: accepted (2026-11-16)

**Decision.** The core package imports no UI/web/queue/LLM framework (import-linter contract). Streamlit
1.67 + pydeck is the research MVP (Copilot home, Risk, Review, Ops console). FastAPI (P3) and the React
agentic canvas (P4) are generated/added as projections of the same Operation Registry; Streamlit pages are
retired only after parity tests on identical run ids.
