"""Stable entity identifiers (blueprint §4.2).

Grammar: ``rid := <layer>.<class>:<ns>:<key>`` — e.g. ``elec.sub_tx:rte:VERDO`` or
``elec.sub_mv_lv:fp:p895123_6245678`` (geometry fingerprint, EPSG:2154 rounded to 1 m).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

RID_PATTERN = r"^[a-z]+\.[a-z0-9_]+:[a-z0-9_]+:[A-Za-z0-9_.\-]+$"
_RID_RE = re.compile(RID_PATTERN)


class InvalidRid(ValueError):
    pass


def make_rid(layer: str, cls: str, ns: str, key: str) -> str:
    rid = f"{layer}.{cls}:{ns}:{key}"
    if not _RID_RE.match(rid):
        raise InvalidRid(rid)
    return rid


def is_rid(value: str) -> bool:
    return bool(_RID_RE.match(value))


def parse_rid(rid: str) -> tuple[str, str, str, str]:
    if not _RID_RE.match(rid):
        raise InvalidRid(rid)
    layer_cls, ns, key = rid.split(":", 2)
    layer, cls = layer_cls.split(".", 1)
    return layer, cls, ns, key


def point_fingerprint(x: float, y: float, resolution_m: float = 1.0) -> str:
    """Fingerprint for point assets without a stable native key (coordinates in a metric CRS)."""
    return f"p{round(x / resolution_m):d}_{round(y / resolution_m):d}"


def line_fingerprint(coords: Sequence[tuple[float, float]], cls: str, length_m: float) -> str:
    """Fingerprint for line assets: class + sorted rounded endpoints + length rounded to 10 m."""
    a = (round(coords[0][0]), round(coords[0][1]))
    b = (round(coords[-1][0]), round(coords[-1][1]))
    lo, hi = sorted([a, b])
    payload = f"{cls}|{lo}|{hi}|{round(length_m / 10.0)}"
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def tool_name(op_name: str) -> str:
    """Op names use dots; LLM tool names may not, so dots become double underscores."""
    return op_name.replace(".", "__")


def op_name_from_tool(name: str) -> str:
    return name.replace("__", ".")
