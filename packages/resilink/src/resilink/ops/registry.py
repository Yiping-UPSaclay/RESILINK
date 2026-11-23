"""Operation Registry core: specs, policy checks, strict JSON-Schema projection, call recording."""

from __future__ import annotations

import copy
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, get_type_hints

from pydantic import BaseModel, ValidationError

from resilink.domain.ids import tool_name
from resilink.domain.vocab import Tier
from resilink.io.hashing import canonical_json, stable_hash


class SideEffect(IntEnum):
    """Ordered autonomy ladder: A0 read < A1 compute < A2 propose < admin (humans only)."""

    READ = 0
    COMPUTE = 1
    PROPOSE = 2
    ADMIN = 3

    @property
    def label(self) -> str:
        return self.name.lower()


class OpDenied(PermissionError):
    pass


@dataclass(frozen=True)
class OpSpec:
    name: str
    summary: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    side_effect: SideEffect
    min_tier: Tier
    cost_hint: str
    fn: Callable[[Any, OpContext], BaseModel]

    @property
    def tool_name(self) -> str:
        return tool_name(self.name)

    def input_json_schema(self, strict: bool = True) -> dict[str, Any]:
        schema = self.input_model.model_json_schema()
        return strictify(schema) if strict else schema

    def output_json_schema(self) -> dict[str, Any]:
        return self.output_model.model_json_schema()

    def tool_definition(self) -> dict[str, Any]:
        """Agent tool definition. ``strict`` only when the schema has no free-form objects (strict tool use
        requires additionalProperties: false everywhere); pydantic re-validates inputs before execution anyway."""
        raw = self.input_model.model_json_schema()
        strict_ok = is_strict_compatible(raw)
        tool: dict[str, Any] = {
            "name": self.tool_name,
            "description": self.summary,
            "input_schema": strictify(raw) if strict_ok else raw,
        }
        if strict_ok:
            tool["strict"] = True
        return tool


# JSON-Schema keywords not accepted by strict tool use / structured outputs (validated server-side by pydantic).
_UNSUPPORTED = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
                "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems"}


def strictify(schema: dict[str, Any]) -> dict[str, Any]:
    """Make a pydantic schema acceptable for strict tool use: additionalProperties false on every object,
    drop unsupported numeric/string/array constraints (pydantic still enforces them when the op runs)."""
    out = copy.deepcopy(schema)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in list(node):
                if key in _UNSUPPORTED:
                    node.pop(key)
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(out)
    return out


def is_strict_compatible(schema: dict[str, Any]) -> bool:
    """False if any object is free-form (no properties, or additionalProperties other than false)."""
    ok = True

    def walk(node: Any) -> None:
        nonlocal ok
        if isinstance(node, dict):
            if node.get("type") == "object" and (
                "properties" not in node or node.get("additionalProperties", False) not in (False, None)
            ):
                ok = False
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return ok


class Registry:
    def __init__(self) -> None:
        self._ops: dict[str, OpSpec] = {}

    def register(self, spec: OpSpec) -> None:
        if spec.name in self._ops:
            raise ValueError(f"duplicate op {spec.name}")
        self._ops[spec.name] = spec

    def get(self, name: str) -> OpSpec:
        return self._ops[name]

    def by_tool_name(self, tname: str) -> OpSpec:
        for spec in self._ops.values():
            if spec.tool_name == tname:
                return spec
        raise KeyError(tname)

    def all(self) -> list[OpSpec]:
        return [self._ops[k] for k in sorted(self._ops)]

    def allowed(self, *, max_side_effect: SideEffect, tier: Tier, names: list[str] | None = None) -> list[OpSpec]:
        """Ops a principal may call (deterministic order -> cache-stable tool lists)."""
        out = [s for s in self.all() if s.side_effect <= max_side_effect and s.min_tier <= tier]
        if names is not None:
            allow = set(names)
            out = [s for s in out if s.name in allow or any(s.name.startswith(n.rstrip("*")) for n in allow if n.endswith("*"))]
        return out


REGISTRY = Registry()


def op(name: str, *, side_effect: SideEffect, min_tier: Tier | str = Tier.PUBLIC, cost_hint: str = "s",
       summary: str | None = None) -> Callable[[Callable[..., BaseModel]], Callable[..., BaseModel]]:
    def deco(fn: Callable[..., BaseModel]) -> Callable[..., BaseModel]:
        hints = get_type_hints(fn)
        params = [h for k, h in hints.items() if k != "return"]
        REGISTRY.register(OpSpec(
            name=name,
            summary=(summary or (fn.__doc__ or "").strip()),
            input_model=params[0],
            output_model=hints["return"],
            side_effect=side_effect,
            min_tier=Tier.parse(min_tier),
            cost_hint=cost_hint,
            fn=fn,
        ))
        return fn
    return deco


@dataclass
class OpContext:
    """Who is calling, with which clearance and autonomy; shared stores; quotas; call recorder."""

    principal: str = "cli"
    tier: Tier = Tier.INTERNAL
    autonomy: SideEffect = SideEffect.COMPUTE
    data: Path | None = None
    max_cascade_simulations: int = 50_000
    recorder: Callable[[str, BaseModel, BaseModel, OpContext], str] | None = None
    session_id: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OpResult:
    call_id: str
    op: str
    output: BaseModel
    elapsed_s: float

    def to_json(self) -> str:
        return canonical_json({"call_id": self.call_id, "op": self.op, "output": self.output.model_dump(mode="json")})


def authorize(spec: OpSpec, ctx: OpContext) -> None:
    if spec.side_effect == SideEffect.ADMIN and not ctx.principal.startswith("human:"):
        raise OpDenied(f"{spec.name} is an admin op: humans only (invariant I2)")
    if spec.side_effect > ctx.autonomy:
        raise OpDenied(f"{spec.name} needs autonomy '{spec.side_effect.label}', principal {ctx.principal} has '{ctx.autonomy.label}'")
    if spec.min_tier > ctx.tier:
        raise OpDenied(f"{spec.name} exposes '{spec.min_tier.label}' data, principal cleared for '{ctx.tier.label}'")


def call(name: str, payload: dict[str, Any] | BaseModel, ctx: OpContext) -> OpResult:
    """Validate -> authorize -> execute -> record. The one entry point for every projection."""
    spec = REGISTRY.get(name)
    try:
        inp = payload if isinstance(payload, spec.input_model) else spec.input_model.model_validate(payload)
    except ValidationError:
        raise
    authorize(spec, ctx)
    t0 = time.perf_counter()
    out = spec.fn(inp, ctx)
    elapsed = time.perf_counter() - t0
    if ctx.recorder is not None:
        call_id = ctx.recorder(name, inp, out, ctx)
    else:
        call_id = "call-" + stable_hash({"op": name, "in": inp.model_dump(mode="json"), "t": time.time_ns()}, n=12)
    return OpResult(call_id=call_id, op=name, output=out, elapsed_s=round(elapsed, 4))
