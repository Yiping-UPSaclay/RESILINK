"""Minimal reader for the header of an OpenStreetMap PBF file (format: https://wiki.openstreetmap.org/wiki/PBF_Format).

A PBF file is a sequence of blocks: a 4-byte big-endian length, a ``BlobHeader`` and a ``Blob`` (protobuf). The first
block is an ``OSMHeader`` whose ``HeaderBlock`` lists the features a reader must support (a format change is drift)
and, for Geofabrik extracts, the replication timestamp of the data. Only the standard library is used.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def fields(buf: bytes) -> Iterator[tuple[int, int | bytes]]:
    """(field number, value) pairs of a protobuf message: ints for varint/fixed fields, bytes for the rest."""
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        num, wire = key >> 3, key & 7
        v: int | bytes
        if wire == 0:
            v, i = _varint(buf, i)
        elif wire == 2:
            n, i = _varint(buf, i)
            v, i = buf[i:i + n], i + n
        elif wire == 1:
            v, i = int.from_bytes(buf[i:i + 8], "little"), i + 8
        elif wire == 5:
            v, i = int.from_bytes(buf[i:i + 4], "little"), i + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        yield num, v


def zigzag(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def read_header(path: Path) -> dict[str, Any]:
    with Path(path).open("rb") as fh:
        n = int.from_bytes(fh.read(4), "big")
        blob_header = dict(fields(fh.read(n)))
        if blob_header.get(1) != b"OSMHeader":
            raise ValueError(f"{path}: the first block is not an OSMHeader")
        blob = dict(fields(fh.read(int(blob_header[3]))))  # type: ignore[arg-type]
    if 1 in blob:
        data = bytes(blob[1])  # type: ignore[arg-type]
    elif 3 in blob:
        data = zlib.decompress(bytes(blob[3]))  # type: ignore[arg-type]
    else:
        raise ValueError(f"{path}: unsupported header compression")
    out: dict[str, Any] = {"required_features": [], "optional_features": []}
    for k, v in fields(data):
        if k == 1 and isinstance(v, bytes):  # HeaderBBox in nanodegrees: left, right, top, bottom
            b = {kk: zigzag(int(vv)) / 1e9 for kk, vv in fields(v)}
            out["bbox"] = [b.get(1), b.get(4), b.get(2), b.get(3)]  # lon_min, lat_min, lon_max, lat_max
        elif k == 4 and isinstance(v, bytes):
            out["required_features"].append(v.decode())
        elif k == 5 and isinstance(v, bytes):
            out["optional_features"].append(v.decode())
        elif k == 16 and isinstance(v, bytes):
            out["writingprogram"] = v.decode()
        elif k == 32 and isinstance(v, int):
            out["replication_timestamp"] = datetime.fromtimestamp(v, UTC).isoformat()
        elif k == 33 and isinstance(v, int):
            out["replication_sequence"] = v
    return out
