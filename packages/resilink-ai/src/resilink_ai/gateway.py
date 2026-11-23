"""AI Gateway + agent session (blueprint §1.6).

Responsibilities: tier-aware provider selection (I4), role model/effort, cache-stable tool projection from the
Operation Registry, refusal / max_tokens handling, budgets, op execution through the same policy-checked
``resilink.ops.call`` used by every other projection, grounding check of the final answer (I1) and AI
provenance (I6).

The loop is written out explicitly (rather than the SDK tool runner) because tools are generated dynamically
from the registry, every call must go through policy + provenance, and the same code must run unchanged on
the first-party API and EU-resident providers (portable feature subset only).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from resilink.domain.vocab import Tier
from resilink.ops import REGISTRY, OpDenied, call, load_builtin_ops
from resilink.ops.recording import make_context
from resilink.views import ViewSpec
from resilink_ai.grounding import GroundingReport, check
from resilink_ai.policy import AIPolicy, PolicyDenied, Provider, load_policy

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def make_client(provider: Provider) -> Any:
    import anthropic

    if provider.kind == "vertex":
        import os

        project = os.environ.get(provider.params.get("project_id_env", "GCP_PROJECT"), "")
        return anthropic.AnthropicVertex(project_id=project, region=provider.params.get("region", "eu"))
    if provider.kind == "anthropic":
        return anthropic.Anthropic()  # credentials: ANTHROPIC_API_KEY or an `ant auth login` profile
    raise PolicyDenied(f"provider kind {provider.kind!r} cannot be used")


@dataclass
class ToolCallRecord:
    op: str
    call_id: str | None
    input: dict[str, Any]
    ok: bool
    error: str | None = None
    output_json: str | None = None


@dataclass
class TurnResult:
    text: str
    tool_calls: list[ToolCallRecord]
    grounding: GroundingReport
    usage: dict[str, int]
    cost_usd: float
    stop_reason: str
    model: str
    provenance_id: str
    views: list[ViewSpec] = field(default_factory=list)


def _usage_dict(usage: Any) -> dict[str, int]:
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return {k: int(getattr(usage, k, 0) or 0) for k in keys}


class AgentSession:
    def __init__(
        self,
        role: str = "copilot",
        *,
        user_clearance: Tier = Tier.INTERNAL,
        client: Any = None,
        data: Path | None = None,
        policy: AIPolicy | None = None,
        principal: str | None = None,
    ) -> None:
        load_builtin_ops()
        self.policy = policy or load_policy()
        self.role = self.policy.roles[role]
        self.tier = self.policy.session_tier(user_clearance)  # cap at what the provider may see (I4)
        self.provider = self.policy.provider_for(self.tier)
        self._client = client
        self.session_id = "sess-" + uuid.uuid4().hex[:12]
        self.ctx = make_context(
            principal or f"agent:{role}", tier=self.tier, autonomy=self.role.autonomy, data=data,
            session_id=self.session_id,
            max_cascade_simulations=int(self.policy.quotas.get("agent_max_cascade_simulations", 20_000)),
        )
        self.knowledge = self.ctx.extras["knowledge"]
        specs = REGISTRY.allowed(max_side_effect=self.role.autonomy, tier=self.tier, names=list(self.role.ops))
        self.tools = [s.tool_definition() for s in specs]  # sorted -> deterministic, cache-stable prefix
        self.messages: list[dict[str, Any]] = []
        self.reference_outputs: list[str] = []
        self.spent_usd = 0.0

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = make_client(self.provider)
        return self._client

    # ----------------------------------------------------------------------------------------------
    def _request(self) -> Any:
        kwargs: dict[str, Any] = dict(
            model=self.role.model,
            max_tokens=self.role.max_tokens,
            system=[{"type": "text", "text": self.role.system_prompt, "cache_control": {"type": "ephemeral"}}],
            tools=self.tools,
            messages=self.messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.role.effort},
        )
        if self.provider.kind == "anthropic" and self.policy.server_side_fallbacks:
            return self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        return self.client.messages.create(**kwargs)

    def _run_tool(self, block: Any, records: list[ToolCallRecord]) -> dict[str, Any]:
        name = getattr(block, "name", "")
        raw_input = getattr(block, "input", {}) or {}
        offered = {t["name"] for t in self.tools}
        if name not in offered:  # only tools offered to this role can run (defence in depth on top of policy)
            records.append(ToolCallRecord(name, None, raw_input, False, "tool not available to this role"))
            return {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                    "content": f"tool {name} is not available to role {self.role.name}"}
        spec = REGISTRY.by_tool_name(name)
        try:
            res = call(spec.name, raw_input, self.ctx)  # validate -> authorize -> execute -> record
            out = res.to_json()
            self.reference_outputs.append(out)
            records.append(ToolCallRecord(spec.name, res.call_id, raw_input, True, None, out))
            return {"type": "tool_result", "tool_use_id": block.id, "content": out}
        except (ValidationError, OpDenied, PolicyDenied, ValueError, KeyError, FileNotFoundError) as exc:
            msg = f"{type(exc).__name__}: {exc}"
            records.append(ToolCallRecord(spec.name, None, raw_input, False, msg))
            return {"type": "tool_result", "tool_use_id": block.id, "is_error": True, "content": msg[:4000]}

    def ask(self, text: str) -> TurnResult:
        started = datetime.now(UTC)
        self.messages.append({"role": "user", "content": text})
        self.reference_outputs.append(json.dumps({"user": text}))
        records: list[ToolCallRecord] = []
        usage_total = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        final_text, stop_reason, model = "", "", self.role.model
        views_before = len(self.ctx.extras.get("views", []))
        budget = float(self.policy.budgets.get("session_usd", 5.0))

        for _ in range(self.role.max_iterations):
            if self.spent_usd >= budget:
                final_text, stop_reason = f"Session budget of ${budget:.2f} reached; stopping.", "budget"
                break
            resp = self._request()
            model = getattr(resp, "model", model) or model
            u = _usage_dict(getattr(resp, "usage", None))
            for k in usage_total:
                usage_total[k] += u[k]
            self.spent_usd += self.policy.cost_usd(self.role.model, u)
            stop_reason = getattr(resp, "stop_reason", "") or ""
            content = list(getattr(resp, "content", []) or [])
            if stop_reason == "refusal":
                final_text = "The model declined this request."
                break
            self.messages.append({"role": "assistant", "content": content})
            tool_uses = [b for b in content if getattr(b, "type", "") == "tool_use"]
            texts = [getattr(b, "text", "") for b in content if getattr(b, "type", "") == "text"]
            if stop_reason == "max_tokens" and tool_uses:
                self.messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                     "content": "Tool input was truncated (max_tokens); retry with a smaller request."} for b in tool_uses]})
                continue
            if not tool_uses:
                final_text = "\n".join(t for t in texts if t).strip()
                break
            results = [self._run_tool(b, records) for b in tool_uses]
            self.messages.append({"role": "user", "content": results})  # all results in ONE user message
        else:
            final_text = final_text or "Stopped: maximum number of tool iterations reached."
            stop_reason = stop_reason or "max_iterations"

        grounding = check(final_text, self.reference_outputs)
        cost = self.policy.cost_usd(self.role.model, usage_total)
        prov_id = self.knowledge.record_provenance({
            "session_id": self.session_id, "role": self.role.name, "provider": self.provider.name, "model": model,
            "effort": self.role.effort, "prompt_sha": self.role.prompt_sha,
            "tool_calls": [{"op": r.op, "call_id": r.call_id, "ok": r.ok, "error": r.error} for r in records],
            "usage": {**usage_total, "grounding": grounding.as_dict()}, "cost_usd": cost, "stop_reason": stop_reason,
            "started_at": started.replace(tzinfo=None), "ended_at": datetime.now(UTC).replace(tzinfo=None),
        })
        views = list(self.ctx.extras.get("views", []))[views_before:]
        return TurnResult(final_text, records, grounding, usage_total, round(cost, 6), stop_reason, model, prov_id, views)
