"""AI policy: roles, tier -> provider routing (I4), autonomy, quotas, budgets, prices."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any

from resilink.domain.vocab import Tier
from resilink.io.paths import load_yaml, resolve
from resilink.ops.registry import SideEffect


class PolicyDenied(PermissionError):
    pass


@dataclass(frozen=True)
class Role:
    name: str
    model: str
    effort: str
    system_prompt: str
    prompt_sha: str
    autonomy: SideEffect
    ops: tuple[str, ...]
    max_iterations: int
    max_tokens: int


@dataclass(frozen=True)
class Provider:
    name: str
    kind: str  # anthropic | vertex | none
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AIPolicy:
    roles: dict[str, Role]
    providers: dict[str, Provider]
    routing: dict[Tier, str]
    quotas: dict[str, Any]
    budgets: dict[str, float]
    prices: dict[str, dict[str, float]]
    server_side_fallbacks: bool

    def provider_for(self, tier: Tier) -> Provider:
        name = self.routing.get(tier, "none")
        if name == "none":
            raise PolicyDenied(f"no external LLM may process '{tier.label}' data (invariant I4)")
        return self.providers[name]

    def session_tier(self, user_clearance: Tier) -> Tier:
        """Cap the agent session at the highest tier <= user clearance that has an allowed provider.
        Agents therefore never receive data their provider is not allowed to see."""
        for tier in sorted(Tier, reverse=True):
            if tier <= user_clearance and self.routing.get(tier, "none") != "none":
                return tier
        raise PolicyDenied("no tier is routable to an LLM provider")

    def cost_usd(self, model: str, usage: dict[str, int]) -> float:
        p = self.prices.get(model)
        if not p:
            return 0.0
        return (
            usage.get("input_tokens", 0) * p["input"]
            + usage.get("output_tokens", 0) * p["output"]
            + usage.get("cache_read_input_tokens", 0) * p["cache_read"]
            + usage.get("cache_creation_input_tokens", 0) * p["cache_write_5m"]
        ) / 1e6


def load_policy() -> AIPolicy:
    models = load_yaml("configs/ai/models.yaml")
    pol = load_yaml("configs/ai/policies.yaml")
    roles = {}
    for name, r in models["roles"].items():
        text = resolve(r["prompt"]).read_text(encoding="utf-8")
        roles[name] = Role(
            name=name, model=r["model"], effort=r["effort"], system_prompt=text,
            prompt_sha=hashlib.sha256(text.encode()).hexdigest()[:16],
            autonomy=SideEffect[r["autonomy"].upper()], ops=tuple(r["ops"]),
            max_iterations=int(r.get("max_iterations", 12)), max_tokens=int(r.get("max_tokens", 16000)),
        )
    providers = {n: Provider(n, p["kind"], {k: v for k, v in p.items() if k != "kind"}) for n, p in pol["providers"].items()}
    routing = {Tier.parse(k): v for k, v in pol["routing"].items()}
    return AIPolicy(roles=roles, providers=providers, routing=routing, quotas=pol.get("quotas", {}),
                    budgets=pol.get("budgets", {}), prices=models.get("prices_usd_per_mtok", {}),
                    server_side_fallbacks=bool(models.get("server_side_fallbacks", True)))


def ai_enabled() -> bool:
    """AI can be switched off globally (invariant I7: the lab works without AI)."""
    return os.environ.get("RESILINK_AI", "on").lower() not in ("off", "0", "false")
