---
name: add-op
description: Add a new typed operation to the RESILINK Operation Registry so humans (CLI / Ops console), agents and MCP clients all get the capability. Use when asked to expose a new lab capability, query or action.
---

# Add an operation (AI-native capability workflow)

1. Decide the side-effect class: `READ` (no state change), `COMPUTE` (deterministic, quota-bounded runs),
   `PROPOSE` (creates a proposal for human review), `ADMIN` (humans only — never for agents).
2. Decide the minimum tier of the data it can expose (`public` / `internal` / `restricted`). Asset-level
   vulnerability on real territories is `restricted`; use `_require_tier` for data-dependent checks.
3. In `packages/resilink/src/resilink/ops/builtin.py` (or a new module imported by `load_builtin_ops`):
   - define pydantic input/output models (prefer typed fields over `dict[str, Any]` so the tool can be strict);
   - decorate the function with `@op("area.verb_noun", side_effect=..., min_tier=..., cost_hint=...)`;
   - write the docstring as the agent-facing description: what it returns AND **when to call it**.
4. Keep the op a thin facade over the deterministic core (no business logic in the op itself, no LLM calls).
5. Add a test in `tests/integration/test_ops_agents_mcp.py`; run `uv run pytest -q`, `uv run lint-imports`,
   `uv run ruff check .`. Check `uv run resilink ops schema <name>` shows a sensible strict schema.
6. If agents should use it, add the op name/pattern to the role's `ops` list in `configs/ai/models.yaml`.
