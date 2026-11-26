"""Compute RESILINK progress from docs/roadmap.yaml plus measured results (feeds the progress dashboard).

Acceptance criteria are never self-reported: their status comes from the tests named in the roadmap
(tests/acceptance, marker ``acceptance``). The full mode also runs the whole test suite, ruff, the import
contracts and the synthetic demo (runtime against the ADR-0008 budget, and ``resilink reproduce``).

Usage:
    uv run python tools/progress.py            # full: acceptance + tests + lint + contracts + demo
    uv run python tools/progress.py --quick    # acceptance tests only
Writes data/progress/progress.json and data/progress/dashboard/*.json (git-ignored; the dashboard's data files)
and prints a summary.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "progress" / "progress.json"
BIN = Path(sys.executable).parent


def _run(cmd: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=False)


def _script(name: str) -> str:
    found = shutil.which(name, path=str(BIN))
    if not found:
        raise SystemExit(f"{name} not found next to {sys.executable}; run through `uv run`")
    return found


def acceptance_outcomes() -> dict[str, dict[str, str]]:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "acceptance.json"
        env = os.environ | {"RESILINK_PROGRESS_OUT": str(out)}
        _run([sys.executable, "-m", "pytest", "tests/acceptance", "-q", "-p", "no:cacheprovider"], env=env)
        return json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}


def test_suite() -> dict[str, int]:
    with tempfile.TemporaryDirectory() as tmp:
        xml = Path(tmp) / "junit.xml"
        _run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}"])
        suite = ET.parse(xml).getroot()
        suite = suite if suite.tag == "testsuite" else suite.find("testsuite")
        total, failed = int(suite.get("tests", 0)), int(suite.get("failures", 0)) + int(suite.get("errors", 0))
        skipped = int(suite.get("skipped", 0))
        return {"total": total, "passed": total - failed - skipped, "failed": failed, "skipped_or_xfail": skipped}


def import_contracts() -> dict[str, int]:
    res = _run([_script("lint-imports")])
    m = re.search(r"Contracts:\s*(\d+)\s*kept,\s*(\d+)\s*broken", res.stdout)
    return {"kept": int(m.group(1)), "broken": int(m.group(2))} if m else {"kept": 0, "broken": -1}


def ruff_clean() -> bool:
    return _run([sys.executable, "-m", "ruff", "check", "."]).returncode == 0


def demo() -> dict[str, object]:
    t0 = datetime.now(UTC)
    res = _run([_script("resilink"), "demo"])
    total_s = (datetime.now(UTC) - t0).total_seconds()
    m = re.search(r"run-[0-9a-f]{16}", res.stdout + res.stderr)
    if res.returncode != 0 or not m:
        return {"ok": False, "error": (res.stderr or res.stdout)[-400:]}
    run_id = m.group(0)
    data = Path(os.environ.get("RESILINK_DATA", ROOT / "data"))
    manifest = json.loads((data / "results" / run_id / "manifest.json").read_text(encoding="utf-8"))
    rep = _run([_script("resilink"), "reproduce", run_id])
    return {"ok": True, "run_id": run_id, "samples": manifest.get("cascade_simulations"),
            "runtime_s": manifest.get("elapsed_s"), "command_s": round(total_s, 1),
            "reproduce_identical": "IDENTICAL" in (rep.stdout + rep.stderr)}


def github_ci(repo: str) -> dict[str, object]:
    """Latest GitHub Actions run on main (public API, no credentials)."""
    import httpx

    try:
        r = httpx.get(f"https://api.github.com/repos/{repo}/actions/runs", params={"branch": "main", "per_page": 1},
                      headers={"User-Agent": "resilink-progress", "Accept": "application/vnd.github+json"},
                      timeout=15)
        r.raise_for_status()
        runs = r.json().get("workflow_runs", [])
        if not runs:
            return {"status": "none"}
        run = runs[0]
        return {"status": run["status"], "conclusion": run["conclusion"], "sha": run["head_sha"][:7],
                "url": run["html_url"], "updated_at": run["updated_at"]}
    except Exception as exc:
        return {"status": "unknown", "error": str(exc)[:200]}


def summarise(roadmap: dict, outcomes: dict, measured: dict) -> dict:
    criteria = []
    for c in roadmap.get("acceptance", []):
        status = outcomes.get(c["id"], {}).get("status", "not_run")
        criteria.append({**c, "status": status})
    phases = []
    for ph in roadmap["phases"]:
        items = ph.get("items", [])
        done = sum(1 for it in items if it["status"] == "done")
        phases.append({**ph, "done": done, "total": len(items),
                       "status": "done" if items and done == len(items)
                       else "in_progress" if done or any(it["status"] == "in_progress" for it in items)
                       else "not_started"})
    goals = [c for c in criteria if c.get("kind") == "goal"]
    guards = [c for c in criteria if c.get("kind") == "guard"]
    groups = {}
    for c in goals:
        g = groups.setdefault(c["id"].split("/")[0], {"goals_met": 0, "goals_total": 0})
        g["goals_total"] += 1
        g["goals_met"] += c["status"] in ("met", "met_remove_xfail")
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "roadmap_updated": str(roadmap.get("updated")),
        "phases": phases,
        "acceptance": criteria,
        "summary": {
            "goals_met": sum(c["status"] in ("met", "met_remove_xfail") for c in goals),
            "goals_total": len(goals),
            "guards_holding": sum(c["status"] == "met" for c in guards),
            "guards_total": len(guards),
            "open_decisions": sum(d.get("status") == "open" for d in roadmap.get("decisions", [])),
            "groups": groups,  # e.g. POC (proof of concept) and ADR-0008 (refactor)
        },
        "budgets": roadmap.get("budgets", {}),
        "decisions": roadmap.get("decisions", []),
        "measured": measured,
    }


def _check_rows(measured: dict, budgets: dict, at: str) -> list[dict]:
    rows: list[dict] = []

    def add(id_: str, label: str, value, unit: str, target: str, result: str, detail: str) -> None:
        rows.append({"id": id_, "label": label, "value": value, "unit": unit, "target": target, "result": result,
                     "detail": detail, "generated_at": at})

    if t := measured.get("tests"):
        ok = "ok" if t["failed"] == 0 else "fail"
        add("tests_passed", "Test suite", t["passed"], "tests", "no failures", ok,
            f"{t['passed']} passed, {t['failed']} failing, {t['skipped_or_xfail']} skipped or not built yet")
        add("tests_failed", "Failing tests", t["failed"], "tests", "0", ok, f"{t['failed']} failing")
    if c := measured.get("import_contracts"):
        add("import_contracts_broken", "Architecture import contracts", c["broken"], "contracts", "0 broken",
            "ok" if c["broken"] == 0 else "fail", f"{c['kept']} kept, {c['broken']} broken")
    if "ruff_clean" in measured:
        add("ruff", "Linter (ruff)", None, "", "clean", "ok" if measured["ruff_clean"] else "fail",
            "clean" if measured["ruff_clean"] else "issues found")
    if (d := measured.get("demo")) and d.get("ok"):
        # Absolute time depends on the machine's state (the same demo varies by ±10 % within minutes on a laptop),
        # so it is reported, not judged; speed regressions are checked by paired runs against main (A/B).
        add("demo_runtime_s", "Demo runtime", d["runtime_s"], "s", "A/B vs main ≤ +15 %", "info",
            f"{d['runtime_s']} s for {d['samples']:,} worlds on this machine (speed is judged by A/B runs)")
        add("demo_samples", "Demo size", d["samples"], "worlds", "", "info", f"{d['samples']:,} simulated worlds")
        add("reproduce", "Reproduction", None, "", "identical", "ok" if d["reproduce_identical"] else "fail",
            "identical" if d["reproduce_identical"] else "differs")
    elif d:
        add("demo_runtime_s", "Demo runtime", None, "s", "", "fail", "demo failed")
    if ci := measured.get("github_ci"):
        concl, status = ci.get("conclusion"), ci.get("status")
        result = "ok" if concl == "success" else "fail" if concl in ("failure", "timed_out", "cancelled") else "info"
        detail = (f"{concl or status} on commit {ci['sha']}" if ci.get("sha")
                  else "no run yet" if status == "none" else "status unavailable")
        add("ci_main", "GitHub CI (main, Linux + Windows)", None, "", "success", result, detail)
    return rows


def dashboard_files(report: dict) -> Path:
    """Write the dashboard's datasets as JSON {rows, meta}; meta carries this script so readers can check it."""
    out = OUT.parent / "dashboard"
    out.mkdir(parents=True, exist_ok=True)
    meta = [{"label": "progress.py", "value": Path(__file__).read_text(encoding="utf-8"), "type": "code",
             "lang": "python"},
            {"label": "Input", "value": "docs/roadmap.yaml, tests/acceptance, full test suite, lint-imports, ruff, "
                                        "resilink demo and reproduce"}]
    items = [{"phase": ph["id"], "phase_name": ph["name"], "phase_goal": ph.get("goal", ""),
              "planned_start": str(ph.get("planned", {}).get("start") or ""),
              "planned_end": str(ph.get("planned", {}).get("end") or ""),
              "item": it["id"], "name": it["name"], "status": it["status"], "done_on": str(it.get("done_on") or ""),
              "blocked_by": ", ".join(it.get("blocked_by", [])), "criteria": ", ".join(it.get("criteria", [])),
              "target": str(it.get("target") or "")}
             for ph in report["phases"] for it in ph.get("items", [])]
    criteria = [{**{k: c.get(k, "") for k in ("id", "kind", "text", "status", "test")},
                 "group": c["id"].split("/")[0]} for c in report["acceptance"]]
    decisions = [{k: str(d.get(k) or "") for k in ("id", "question", "needed_for", "status", "decision", "decided_on")}
                 for d in report["decisions"]]
    checks = _check_rows(report["measured"], report["budgets"], report["generated_at"])
    for name, rows in (("items", items), ("criteria", criteria), ("decisions", decisions), ("checks", checks)):
        (out / f"{name}.json").write_text(json.dumps({"rows": rows, "meta": meta}, indent=1, default=str),
                                          encoding="utf-8")
    return out


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quick", action="store_true", help="acceptance tests only")
    args = ap.parse_args()

    roadmap = yaml.safe_load((ROOT / "docs" / "roadmap.yaml").read_text(encoding="utf-8"))
    outcomes = acceptance_outcomes()
    measured: dict[str, object] = {}
    if not args.quick:
        measured = {"tests": test_suite(), "import_contracts": import_contracts(), "ruff_clean": ruff_clean(),
                    "demo": demo()}
        if repo := roadmap.get("repository"):
            measured["github_ci"] = github_ci(repo)
    report = summarise(roadmap, outcomes, measured)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    s = report["summary"]
    print(f"RESILINK progress ({report['generated_at']})")
    for ph in report["phases"]:
        print(f"  {ph['id']:<3} {ph['name']:<45} {ph['done']:>2}/{ph['total']:<2} {ph['status']}")
    goals = " · ".join(f"{g} goals met {v['goals_met']}/{v['goals_total']}" for g, v in s["groups"].items())
    print(f"  {goals} · guards holding {s['guards_holding']}/{s['guards_total']} · "
          f"open decisions {s['open_decisions']}")
    for c in report["acceptance"]:
        print(f"    {c['id']:<14} {c['status']:<17} {c['text']}")
    if measured:
        print(f"  measured: {json.dumps(measured, default=str)}")
    dash_dir = dashboard_files(report)
    print(f"written: {OUT.relative_to(ROOT)} and {dash_dir.relative_to(ROOT)}/*.json")


if __name__ == "__main__":
    main()
