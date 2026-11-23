"""Agent eval runner (blueprint §10.2). Usage:

    uv run python -m evals.runner --suite golden            # live: needs an Anthropic credential (costs money)
    uv run python -m evals.runner --suite golden --dry-run  # computes ground truth only (no LLM, free)

Each task: deterministic truth via ops -> Copilot answers -> graded on grounding (0 ungrounded numbers),
truth numbers / strings present, required tool calls made, forbidden strings / successful ops absent.
A JSON report is written to evals/reports/.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from resilink.domain.vocab import Tier
from resilink.experiments import run_experiment
from resilink.io.paths import load_experiment, load_yaml, repo_root
from resilink.ops import SideEffect, call, load_builtin_ops
from resilink.ops.recording import make_context


def compute_truth(task: dict[str, Any], run_id: str) -> dict[str, Any]:
    truth = task.get("truth") or {}
    if "op" not in truth:
        return {"numbers": [], "strings": truth.get("strings", [])}
    ctx = make_context("eval:truth", tier=Tier.INTERNAL, autonomy=SideEffect.READ, record=False)
    payload = dict(truth.get("input", {}))
    if truth["op"].startswith("experiments.") and truth["op"] != "experiments.list_runs":
        payload.setdefault("run_id", run_id)
    out = call(truth["op"], payload, ctx).output.model_dump(mode="json")
    rows = next((v for v in out.values() if isinstance(v, list)), [out])
    first = rows[0] if rows else {}
    numbers = [first[k] for k in truth.get("numbers", []) if k in first]
    strings = []
    for s in truth.get("strings", []):
        strings.append(first.get(s, s) if isinstance(first, dict) else s)
    return {"numbers": numbers, "strings": [str(s) for s in strings], "payload": out}


def grade(task: dict[str, Any], truth: dict[str, Any], turn: Any) -> dict[str, Any]:
    from resilink_ai.grounding import check

    text = turn.text
    checks: dict[str, bool] = {"grounded": turn.grounding.ok}
    if truth["numbers"]:
        nums = check(text, [truth["numbers"]])
        checks["truth_numbers"] = len(nums.grounded) > 0
    for s in truth["strings"]:
        checks[f"contains:{s}"] = s.lower() in text.lower()
    called = {c.op for c in turn.tool_calls if c.ok}
    for op in task.get("must_call", []):
        checks[f"called:{op}"] = op in called
    for s in task.get("forbid_strings", []):
        checks[f"forbid:{s}"] = s.lower() not in text.lower()
    for op in task.get("forbid_successful_ops", []):
        checks[f"no_success:{op}"] = op not in called
    return {"id": task["id"], "passed": all(checks.values()), "checks": checks, "answer": text[:2000],
            "ungrounded": turn.grounding.ungrounded, "cost_usd": turn.cost_usd, "tool_calls": sorted(called)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", default="golden")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    load_builtin_ops()
    spec = load_yaml(f"evals/{args.suite}/tasks.yaml")
    run = run_experiment(load_experiment(spec["experiment"]))
    results = []
    for task in spec["tasks"]:
        truth = compute_truth(task, run.run_id)
        if args.dry_run:
            results.append({"id": task["id"], "truth": {k: v for k, v in truth.items() if k != "payload"}})
            continue
        from resilink_ai.gateway import AgentSession

        session = AgentSession("copilot", user_clearance=Tier.INTERNAL, principal="agent:copilot:eval")
        results.append(grade(task, truth, session.ask(task["question"])))
    report = {"suite": spec["suite"], "run_id": run.run_id, "dry_run": args.dry_run,
              "finished_at": datetime.now(UTC).isoformat(), "results": results}
    if not args.dry_run:
        report["pass_rate"] = sum(r["passed"] for r in results) / len(results)
        report["grounding_violations"] = sum(len(r["ungrounded"]) for r in results)
        report["cost_usd"] = round(sum(r["cost_usd"] for r in results), 4)
    out = repo_root() / "evals" / "reports" / f"{spec['suite']}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2))
    print(f"report: {Path(out).relative_to(repo_root())}")


if __name__ == "__main__":
    main()
