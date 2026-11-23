# ADR-0007 — AI provider and tier routing (pending D24)
Status: proposed — needs institutional decision

**Facts (2026-11-16).** First-party Anthropic `inference_geo` accepts only "us" | "global" (no EU pin).
Vertex AI (region "eu") and Bedrock EU serve Claude but lack Batches, Files API, Managed Agents, MCP
connector and code execution.
**Decision.** Agents use only the portable feature subset (Messages, tool use, structured outputs, prompt
caching). Routing per tier in `configs/ai/policies.yaml`: public → first-party; restricted → none (never
external); internal → first-party for P0–P2 on open-data-derived data **pending D24**, switchable to
`vertex_eu` without code changes. Sessions are capped at the highest routable tier.
