from __future__ import annotations

import pytest

from resilink.domain.vocab import Tier
from resilink.ops import REGISTRY, OpContext, OpDenied, SideEffect, call, load_builtin_ops
from resilink.ops.registry import is_strict_compatible
from resilink_ai.policy import PolicyDenied, load_policy

load_builtin_ops()


def test_every_op_has_when_to_call_description_and_models():
    assert len(REGISTRY.all()) >= 17
    for spec in REGISTRY.all():
        assert len(spec.summary) > 40, spec.name
        tool = spec.tool_definition()
        assert "." not in tool["name"]
        if tool.get("strict"):
            assert is_strict_compatible(tool["input_schema"])
            assert tool["input_schema"]["additionalProperties"] is False


def test_admin_ops_are_humans_only():
    agent = OpContext(principal="agent:copilot", tier=Tier.INTERNAL, autonomy=SideEffect.ADMIN)
    with pytest.raises(OpDenied):
        call("proposals.decide", {"proposal_id": "x", "approve": True}, agent)


def test_autonomy_and_tier_are_enforced():
    reader = OpContext(principal="agent:reviewer", tier=Tier.PUBLIC, autonomy=SideEffect.READ)
    with pytest.raises(OpDenied):
        call("data.aggregate", {}, reader)  # internal data, public clearance
    allowed = {s.name for s in REGISTRY.allowed(max_side_effect=SideEffect.READ, tier=Tier.PUBLIC)}
    assert "experiments.submit" not in allowed and "proposals.decide" not in allowed


def test_restricted_tier_never_routed_to_external_llm():
    pol = load_policy()
    with pytest.raises(PolicyDenied):
        pol.provider_for(Tier.RESTRICTED)
    assert pol.session_tier(Tier.RESTRICTED) == Tier.INTERNAL  # sessions are capped (I4)


def test_all_roles_default_to_opus_5_5():
    pol = load_policy()
    assert {r.model for r in pol.roles.values()} == {"claude-opus-5-5"}
    assert pol.cost_usd("claude-opus-5-5", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}) == pytest.approx(24.0)
