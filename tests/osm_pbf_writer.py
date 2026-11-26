"""Write tiny OpenStreetMap PBF files for offline tests (https://wiki.openstreetmap.org/wiki/PBF_Format).

One OSMHeader block and one OSMData block holding DenseNodes and Ways, zlib-compressed, no metadata.
"""

from __future__ import annotations

import zlib
from pathlib import Path


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _zz(n: int) -> int:  # zigzag for sint64
    return (n << 1) ^ (n >> 63)


def _key(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _bytes(field: int, data: bytes) -> bytes:
    return _key(field, 2) + _varint(len(data)) + data


def _uint(field: int, n: int) -> bytes:
    return _key(field, 0) + _varint(n)


def _packed(field: int, values: list[int]) -> bytes:
    return _bytes(field, b"".join(_varint(v) for v in values))


def _delta(values: list[int]) -> list[int]:
    prev, out = 0, []
    for v in values:
        out.append(_zz(v - prev))
        prev = v
    return out


def _block(kind: bytes, payload: bytes) -> bytes:
    blob = _uint(2, len(payload)) + _bytes(3, zlib.compress(payload))
    header = _bytes(1, kind) + _uint(3, len(blob))
    return len(header).to_bytes(4, "big") + header + blob


def write_pbf(path: Path, nodes: list[tuple[int, float, float, dict[str, str]]],
              ways: list[tuple[int, list[int], dict[str, str]]], *, timestamp: int = 1_795_000_000) -> Path:
    """``nodes``: (id, lat, lon, tags); ``ways``: (id, node refs, tags)."""
    strings: list[bytes] = [b""]

    def sid(s: str) -> int:
        b = s.encode()
        if b not in strings:
            strings.append(b)
        return strings.index(b)

    keys_vals: list[int] = []
    for _, _, _, tags in nodes:
        for k, v in tags.items():
            keys_vals += [sid(k), sid(v)]
        keys_vals.append(0)
    dense = (_packed(1, _delta([n[0] for n in nodes]))
             + _packed(8, _delta([round(n[1] * 1e7) for n in nodes]))  # granularity 100 nanodegrees
             + _packed(9, _delta([round(n[2] * 1e7) for n in nodes]))
             + _packed(10, keys_vals))
    group = _bytes(2, dense)
    for wid, refs, tags in ways:
        way = (_uint(1, wid) + _packed(2, [sid(k) for k in tags]) + _packed(3, [sid(v) for v in tags.values()])
               + _packed(8, _delta(refs)))
        group += _bytes(3, way)
    table = b"".join(_bytes(1, s) for s in strings)
    data = _bytes(1, table) + _bytes(2, group)
    header = (_bytes(4, b"OsmSchema-V0.6") + _bytes(4, b"DenseNodes") + _bytes(16, b"resilink-tests")
              + _uint(32, timestamp))
    Path(path).write_bytes(_block(b"OSMHeader", header) + _block(b"OSMData", data))
    return Path(path)
