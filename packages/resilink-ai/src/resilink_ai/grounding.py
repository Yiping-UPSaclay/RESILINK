"""Deterministic grounding checker (invariant I1).

Every numeric token in an answer must match a value available from the session's tool outputs (or the user's
own message), allowing faithful rounding. Heuristics (documented, configurable):
* tokens glued to identifiers (S21, rp100, p892881_6242902, o0-i1) are not numbers;
* integers 0..10 and calendar years 1900..2100 are treated as non-data words;
* fractions in [0, 1] are also accepted as percentages (0.478 <-> 47.8 %);
* list lengths in tool outputs count as available values (counts of returned items).
"""

from __future__ import annotations

import contextlib
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

_RANGE = re.compile(r"(?<=\d)\s*[-–]\s*(?=\d)")  # "207,766-227,482" -> "207,766 to 227,482"
_NUM = re.compile(
    r"(?<![\w.\-:/,])"                                  # not glued to identifiers or inside a number
    r"[-+−]?(?:\d{1,3}(?:[,\u202f\u00a0 ]\d{3})+|\d+)"  # integer part, optional thousands groups
    r"(?:[.,]\d+)?"                                     # decimals
    r"(?:\s?(?:%|k\b|K\b|M\b|million|millions|thousand|milliers|mille))?"
    r"(?![\w:/])"
)
_SCALE = {"k": 1e3, "K": 1e3, "thousand": 1e3, "milliers": 1e3, "mille": 1e3, "M": 1e6, "million": 1e6, "millions": 1e6}


@dataclass
class GroundingReport:
    checked: int = 0
    grounded: list[str] = field(default_factory=list)
    ungrounded: list[str] = field(default_factory=list)
    exempt: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.ungrounded

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checked": self.checked, "ungrounded": self.ungrounded, "exempt": self.exempt}


def _parse(token: str) -> tuple[float, int, bool] | None:
    """Return (value, decimals, is_percent) for a numeric token."""
    t = token.strip().replace("−", "-")
    pct = t.endswith("%")
    scale = 1.0
    for word, mult in _SCALE.items():
        if t.endswith(word):
            scale, t = mult, t[: -len(word)].strip()
            break
    t = t.rstrip("%").strip()
    t = re.sub(r"[\u202f\u00a0 ]", "", t)
    if re.fullmatch(r"[-+]?[1-9]\d{0,2}(,\d{3})+(\.\d+)?", t):  # "0,478" is a decimal comma, not thousands
        t = t.replace(",", "")
    elif re.fullmatch(r"[-+]?\d+,\d+", t):
        t = t.replace(",", ".")
    try:
        value = float(t) * scale
    except ValueError:
        return None
    decimals = len(t.split(".")[1]) if "." in t else 0
    return value, decimals, pct


def collect_reference_values(objs: Iterable[Any]) -> list[float]:
    out: list[float] = []

    def walk(o: Any) -> None:
        if isinstance(o, bool) or o is None:
            return
        if isinstance(o, int | float):
            if math.isfinite(o):
                out.append(float(o))
        elif isinstance(o, str):
            for m in _NUM.finditer(o):
                p = _parse(m.group(0))
                if p:
                    out.append(p[0])
            for m in re.finditer(r"\d+(?:\.\d+)?", o):  # digits inside identifiers (rp100 -> 100)
                out.append(float(m.group(0)))
        elif isinstance(o, dict):
            for k, v in o.items():
                for m in re.finditer(r"\d+", str(k)):  # quantile levels in keys (p95, var_95) are part of the schema
                    out.append(float(m.group(0)))
                walk(v)
        elif isinstance(o, list | tuple):
            out.append(float(len(o)))
            for v in o:
                walk(v)

    for obj in objs:
        if isinstance(obj, str):
            with contextlib.suppress(ValueError):
                obj = json.loads(obj)
        walk(obj)
    return out


def _matches(value: float, decimals: int, is_pct: bool, refs: list[float]) -> bool:
    """Grounded if a reference (or reference x 100, for percentages) equals the token after rounding to the
    token's displayed decimals, or lies within 0.6 % of it (rounding to three significant digits)."""
    cands = [value] + ([value / 100.0] if is_pct else [])
    for v in cands:
        for r in refs:
            for rr in (r, r * 100.0):
                if abs(round(rr, decimals) - v) <= 1e-9:
                    return True
                if rr != 0 and abs(v) >= 1 and abs(v - rr) / abs(rr) <= 0.006:
                    return True
    return False


def check(answer: str, reference_objs: Iterable[Any]) -> GroundingReport:
    refs = collect_reference_values(reference_objs)
    rep = GroundingReport()
    answer = _RANGE.sub(" to ", answer)
    for m in _NUM.finditer(answer):
        token = m.group(0).strip()
        parsed = _parse(token)
        if parsed is None:
            continue
        value, decimals, pct = parsed
        if not pct and decimals == 0 and (0 <= value <= 10 or 1900 <= value <= 2100) and not token.endswith(("k", "M")):
            rep.exempt.append(token)
            continue
        rep.checked += 1
        (rep.grounded if _matches(value, decimals, pct, refs) else rep.ungrounded).append(token)
    return rep
