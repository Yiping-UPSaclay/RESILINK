"""Acceptance-criterion bookkeeping for progress tracking (docs/roadmap.yaml, tools/progress.py).

Each acceptance test carries ``@pytest.mark.acceptance("<criterion id>")``. Criteria that are not built yet are
marked ``xfail(strict=True)``: they report "not met" today, and the suite fails as soon as one starts passing,
which is the signal to remove its ``xfail`` and count it as met. When ``RESILINK_PROGRESS_OUT`` is set, the
outcome of every criterion is written there as JSON.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

_OUTCOMES: dict[str, dict[str, str]] = {}


def _status(rep: pytest.TestReport) -> str:
    if hasattr(rep, "wasxfail"):
        return "not_met"
    if rep.passed:
        return "met"
    if "XPASS(strict)" in str(rep.longrepr):
        return "met_remove_xfail"
    return "failing"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    rep = outcome.get_result()
    marker = item.get_closest_marker("acceptance")
    if marker is None or rep.when not in ("setup", "call"):
        return
    if rep.when == "setup" and rep.passed:
        return
    for criterion in marker.args:
        _OUTCOMES[criterion] = {"status": _status(rep), "test": item.nodeid}


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    out = os.environ.get("RESILINK_PROGRESS_OUT")
    if out and _OUTCOMES:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(_OUTCOMES, indent=2, sort_keys=True), encoding="utf-8")
