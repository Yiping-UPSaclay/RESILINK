"""Eval harness: ground truth is computable without any LLM; live evals are skipped without credentials."""

from __future__ import annotations

import os

import pytest

from resilink.io.paths import load_yaml
from resilink.ops import load_builtin_ops

load_builtin_ops()


def test_golden_truths_compute_deterministically(small_run):
    from evals.runner import compute_truth

    spec = load_yaml("evals/golden/tasks.yaml")
    for task in spec["tasks"]:
        t = compute_truth(task, small_run.run_id)
        assert isinstance(t["numbers"], list) and isinstance(t["strings"], list), task["id"]
        if task.get("truth", {}).get("numbers"):
            assert t["numbers"], task["id"]


@pytest.mark.ai
@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="live eval needs an Anthropic credential")
def test_live_golden_eval_smoke(small_run):
    from evals.runner import compute_truth, grade
    from resilink_ai.gateway import AgentSession

    task = load_yaml("evals/golden/tasks.yaml")["tasks"][0]
    truth = compute_truth(task, small_run.run_id)
    turn = AgentSession("copilot").ask(task["question"])
    assert grade(task, truth, turn)["checks"]["grounded"]
