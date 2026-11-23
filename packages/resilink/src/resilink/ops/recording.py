"""Bind an OpContext to the knowledge store so every op call is logged (grounding targets + provenance)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from resilink.domain.vocab import Tier
from resilink.knowledge.store import KnowledgeStore
from resilink.ops.registry import OpContext, SideEffect


def knowledge_recorder(ks: KnowledgeStore) -> Callable[[str, BaseModel, BaseModel, OpContext], str]:
    def record(name: str, inp: BaseModel, out: BaseModel, ctx: OpContext) -> str:
        return ks.record_call(name, inp, out, ctx.principal, ctx.session_id)

    return record


def make_context(
    principal: str,
    *,
    tier: Tier | str = Tier.INTERNAL,
    autonomy: SideEffect = SideEffect.COMPUTE,
    data: Path | None = None,
    session_id: str | None = None,
    max_cascade_simulations: int = 50_000,
    record: bool = True,
) -> OpContext:
    ks = KnowledgeStore(data / "knowledge" / "knowledge.duckdb" if data else None)
    return OpContext(
        principal=principal, tier=Tier.parse(tier), autonomy=autonomy, data=data, session_id=session_id,
        max_cascade_simulations=max_cascade_simulations, recorder=knowledge_recorder(ks) if record else None,
        extras={"knowledge": ks},
    )
