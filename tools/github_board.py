"""Sync the GitHub board from docs/roadmap.yaml (roadmap item C3): one milestone per phase, one issue per item.

The roadmap stays the single source of truth: issues are regenerated from it, never edited by hand. Each issue
carries a hidden marker (``<!-- roadmap:<item id> -->``) so a rerun updates it instead of creating a duplicate.
Done items are closed as completed; a phase's milestone closes when all its items are done. Items' ``refs``
(pull requests ``#n`` or commit SHAs) become links. It calls the GitHub CLI, so it acts with the user's own login.

    uv run python tools/github_board.py            # dry run: list the changes
    uv run python tools/github_board.py --apply    # make them

GitHub lists a new issue a few seconds after creating it: leave a minute between two --apply runs, or a rerun may
not see the newest issue and create it twice.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
MARK = "<!-- roadmap:{} -->"
LABELS = {
    "roadmap": ("0e8a16", "Item of docs/roadmap.yaml (synced by tools/github_board.py)"),
    "status: in progress": ("fbca04", "Roadmap item in progress"),
    "status: blocked": ("b60205", "Roadmap item waiting for a decision or a tool"),
    "status: deferred": ("c5def5", "Roadmap item built when a thesis needs it"),
}
STATUS_LABEL = {"in_progress": "status: in progress", "blocked": "status: blocked", "deferred": "status: deferred"}


def gh_exe() -> str:
    found = shutil.which("gh")
    if found:
        return found
    default = Path("C:/Program Files/GitHub CLI/gh.exe")  # Git Bash may not have it on PATH on Windows
    if default.exists():
        return str(default)
    sys.exit("GitHub CLI (gh) not found: install it and run `gh auth login`")


def api(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    args = [gh_exe(), "api", "-X", method, path, "-H", "Accept: application/vnd.github+json"]
    if payload is not None:
        args += ["--input", "-"]
    r = subprocess.run(args, input=json.dumps(payload) if payload is not None else None, capture_output=True,
                       text=True, encoding="utf-8", check=False)
    if r.returncode != 0:
        raise RuntimeError(f"gh api {method} {path} failed: {r.stderr.strip()}")
    return json.loads(r.stdout) if r.stdout.strip() else None


def paged(path: str) -> list[dict[str, Any]]:
    r = subprocess.run([gh_exe(), "api", "--paginate", "--slurp", path], capture_output=True, text=True,
                       encoding="utf-8", check=False)
    if r.returncode != 0:
        raise RuntimeError(f"gh api {path} failed: {r.stderr.strip()}")
    return [x for page in json.loads(r.stdout) for x in page]


def issue_body(repo: str, phase: dict[str, Any], item: dict[str, Any], criteria: dict[str, dict[str, Any]]) -> str:
    status = item["status"] + (f" ({item['done_on']})" if item.get("done_on") else "")
    lines = [f"**Phase:** {phase['id']} {phase['name']}", f"**Status:** {status}"]
    if item.get("target"):
        lines.append(f"**Target date:** {item['target']}")
    if item.get("blocked_by"):
        lines.append(f"**Blocked by:** {', '.join(item['blocked_by'])}")
    for cid in item.get("criteria", []):
        c = criteria.get(cid, {})
        lines.append(f"**Acceptance criterion {cid}:** {c.get('text', '')} (test `{c.get('test', '')}`)")
    if item.get("refs"):
        lines.append(f"**Work:** {', '.join(str(r) for r in item['refs'])}")
    lines += ["", f"Synced from [docs/roadmap.yaml](https://github.com/{repo}/blob/main/docs/roadmap.yaml) by "
              "`tools/github_board.py`: change the roadmap, not this issue.", MARK.format(item["id"])]
    return "\n".join(lines)


def desired_state(roadmap: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    repo = roadmap["repository"]
    criteria = {c["id"]: c for c in roadmap.get("acceptance", [])}
    milestones, issues = [], []
    for ph in roadmap["phases"]:
        items = ph.get("items", [])
        ms: dict[str, Any] = {"title": f"{ph['id']} {ph['name']}", "description": ph.get("goal", ""),
                              "state": "closed" if items and all(i["status"] == "done" for i in items) else "open"}
        if ph.get("planned", {}).get("end"):
            ms["due_on"] = f"{ph['planned']['end']}T12:00:00Z"
        milestones.append(ms)
        for it in items:
            labels = ["roadmap"] + ([STATUS_LABEL[it["status"]]] if it["status"] in STATUS_LABEL else [])
            issues.append({"id": it["id"], "milestone": ms["title"], "title": f"{it['id']}: {it['name']}",
                           "body": issue_body(repo, ph, it, criteria), "labels": sorted(labels),
                           "state": "closed" if it["status"] == "done" else "open"})
    return milestones, issues


def sync(roadmap: dict[str, Any], apply: bool) -> list[str]:
    repo = roadmap["repository"]
    base = f"repos/{repo}"
    milestones, issues = desired_state(roadmap)
    log: list[str] = []

    def do(msg: str, method: str, path: str, payload: dict[str, Any]) -> Any:
        log.append(msg)
        if not apply:
            return None
        out = api(method, path, payload)
        time.sleep(0.5)  # stay well under GitHub's secondary rate limit for content creation
        return out

    have_labels = {lab["name"] for lab in paged(f"{base}/labels")}
    for name, (color, desc) in LABELS.items():
        if name not in have_labels:
            do(f"create label {name!r}", "POST", f"{base}/labels", {"name": name, "color": color, "description": desc})

    have_ms = {m["title"].split(" ", 1)[0]: m for m in paged(f"{base}/milestones?state=all")}
    numbers: dict[str, int | None] = {}
    for ms in milestones:
        key = ms["title"].split(" ", 1)[0]
        cur = have_ms.get(key)
        if cur is None:
            out = do(f"create milestone {ms['title']!r}", "POST", f"{base}/milestones", ms)
            numbers[ms["title"]] = out["number"] if out else None
        else:
            numbers[ms["title"]] = cur["number"]
            changed = {k: v for k, v in ms.items() if (cur.get(k) or "") != v and not
                       (k == "due_on" and str(cur.get("due_on") or "")[:10] == v[:10])}
            if changed:
                do(f"update milestone {ms['title']!r}: {sorted(changed)}", "PATCH",
                   f"{base}/milestones/{cur['number']}", changed)

    existing = {}
    for iss in paged(f"{base}/issues?state=all&labels=roadmap&per_page=100"):
        if "pull_request" in iss:
            continue
        for it in issues:
            if MARK.format(it["id"]) in (iss.get("body") or ""):
                existing[it["id"]] = iss
    for it in issues:
        ms_number = numbers.get(it["milestone"])
        payload: dict[str, Any] = {"title": it["title"], "body": it["body"], "labels": it["labels"]}
        if ms_number is not None:
            payload["milestone"] = ms_number
        cur = existing.get(it["id"])
        if cur is None:
            out = do(f"create issue {it['title'][:70]!r}", "POST", f"{base}/issues", payload)
            if it["state"] == "closed":
                number = out["number"] if out else "?"
                do(f"close issue {it['id']} as completed", "PATCH", f"{base}/issues/{number}",
                   {"state": "closed", "state_reason": "completed"})
            continue
        diff = {}
        if cur["title"] != it["title"]:
            diff["title"] = it["title"]
        if (cur.get("body") or "") != it["body"]:
            diff["body"] = it["body"]
        if sorted(lab["name"] for lab in cur.get("labels", [])) != it["labels"]:
            diff["labels"] = it["labels"]
        if ms_number is not None and (cur.get("milestone") or {}).get("number") != ms_number:
            diff["milestone"] = ms_number
        if cur["state"] != it["state"]:
            diff["state"] = it["state"]
            if it["state"] == "closed":
                diff["state_reason"] = "completed"
        if diff:
            do(f"update issue #{cur['number']} ({it['id']}): {sorted(diff)}", "PATCH",
               f"{base}/issues/{cur['number']}", diff)
    return log


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    roadmap = yaml.safe_load((ROOT / "docs" / "roadmap.yaml").read_text(encoding="utf-8"))
    log = sync(roadmap, args.apply)
    print("\n".join(log) or "board already in sync")
    print(f"{len(log)} change(s) {'made' if args.apply else 'planned (dry run; add --apply)'}")


if __name__ == "__main__":
    main()
