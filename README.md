# RESILINK — AI-native digital lab for interdependent infrastructure resilience

Proof-of-concept laboratory for risk and resilience analysis of interdependent French infrastructures
(electricity, gas, telecommunications), built on open data (RTE/ODRÉ, Enedis, NaTran, ARCEP, Géorisques,
INSEE, OSM, …) and designed to accept richer operator data later. Pilot territory: Bouches-du-Rhône (13).

**Status: Sprint 0 (P0 foundations).** The deterministic core, the Operation Registry, the AI layer (agent
gateway, grounding, provenance), the MCP server, the Streamlit MVP and the first real connectors run on
a synthetic territory. Real Bouches-du-Rhône releases start in P1. See
[docs/architecture/blueprint.md](docs/architecture/blueprint.md) and [docs/adr/](docs/adr/).

## Architecture in one paragraph
A deterministic Python core (GeoParquet 1.1 releases, DuckDB, multilayer network, event-driven cascade,
nested Monte Carlo risk) is the **only source of numbers**. Every capability is a typed **operation**
(`resilink.ops`) projected to the CLI, the Streamlit UI, the MCP server and AI agents. Agents (Copilot …)
can read, compute within quotas and propose; humans approve. Answers are checked by a deterministic
**grounding checker**, generated views bind values through `ValueRef`s, and every AI action is recorded
with provenance and cost. The lab works fully with AI switched off.

## Quick start (Windows / Linux)
```bash
uv sync --all-packages                 # Python 3.12 environment from uv.lock
uv run pytest -q                       # ~50 tests, ~30 s, offline
uv run resilink demo                   # synthetic flood experiment: EAL + amplification ratios
uv run resilink ops list               # the typed operations shared by humans, agents and MCP clients
uv run streamlit run apps/streamlit/Home.py
```
AI features need `ANTHROPIC_API_KEY` (see `.env.example`); without it the UI shows the Ops console.
Use the lab from Claude Code via MCP: `claude mcp add resilink -- uv run resilink-mcp`.

## Repository layout
`packages/resilink` (deterministic core) · `packages/resilink-ai` (AI gateway, grounding, roles) ·
`apps/mcp` · `apps/streamlit` · `configs/` · `knowledge/` (curated, git-approved) · `prompts/` · `evals/` ·
`tests/` · `docs/`.

## Data & licences
Open data sources keep their licences (mostly Licence Ouverte 2.0 — attribution required; OSM-derived
tables are ODbL). Asset-level vulnerability results are treated as restricted. Code licence: Apache-2.0
(see `LICENSE` and `NOTICE`).
