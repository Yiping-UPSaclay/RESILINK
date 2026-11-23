# ADR-0006 — AI-native architecture
Status: accepted (2026-11-16)

**Decision.** The lab is operated jointly by humans and agents. Planes: deterministic substrate
(only source of numbers) · capability plane (typed Operation Registry projected to CLI/UI/MCP/agents) ·
knowledge plane (curated YAML in git + DuckDB store with BM25, proposals, findings, op-call log, AI
provenance) · agent plane (roles with autonomy read/compute/propose; humans approve) · experience plane
(Copilot, generated ViewSpecs bound by ValueRef, canonical views). Invariants I1–I7 are enforced by code,
tests and import contracts. Agents default to `claude-opus-5-5` (effort per role, adaptive thinking,
server-side refusal fallback on the first-party API).
**Consequences.** New capabilities are added once (as ops); AI outputs are reviewable artifacts; the lab
degrades gracefully with AI off.
