"""Scripted fake client for deterministic tests of the agent loop (no network, no credentials).

A script is a list of steps; each step is a dict (``{"tools": [(tool_name, input), ...], "text": "..."}``) or
a callable ``(request_kwargs) -> dict`` that can read earlier tool results (e.g. to quote returned values).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

Step = dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]]


class _Messages:
    def __init__(self, script: list[Step]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.requests.append(kwargs)
        step = self.script.pop(0)
        if callable(step):
            step = step(kwargs)
        content = []
        if step.get("text"):
            content.append(SimpleNamespace(type="text", text=step["text"]))
        for n, (name, inp) in enumerate(step.get("tools", [])):
            content.append(SimpleNamespace(type="tool_use", id=f"toolu_{len(self.requests)}_{n}", name=name, input=inp))
        stop = step.get("stop_reason") or ("tool_use" if step.get("tools") else "end_turn")
        usage = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        return SimpleNamespace(content=content, stop_reason=stop, usage=usage, model=kwargs.get("model", "fake"))


class FakeClient:
    def __init__(self, script: list[Step]) -> None:
        self.messages = _Messages(script)
        self.beta = SimpleNamespace(messages=self.messages)


def last_tool_results(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse the JSON payloads of the tool results in the latest user message of a request."""
    msgs = request["messages"]
    for m in reversed(msgs):
        if m["role"] == "user" and isinstance(m["content"], list):
            out = []
            for block in m["content"]:
                if block.get("type") == "tool_result" and not block.get("is_error"):
                    out.append(json.loads(block["content"]))
            return out
    return []
