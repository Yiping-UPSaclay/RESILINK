"""Results store: one immutable directory per content-addressed run id (atomic publish)."""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pandas as pd

from resilink.io import geoparquet as gpq
from resilink.io.hashing import canonical_json, sha256_file
from resilink.io.paths import data_root


class RunWriter:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.tables: dict[str, dict[str, Any]] = {}

    def table(self, name: str, df: pd.DataFrame) -> None:
        rel = f"{name}.parquet"
        gpq.write_table(df, self.tmp / rel)
        self.tables[name] = {"path": rel, "rows": len(df), "sha256": sha256_file(self.tmp / rel)}

    def json(self, name: str, obj: Any) -> None:
        (self.tmp / f"{name}.json").write_text(canonical_json(obj), encoding="utf-8")


class ResultsStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or data_root()) / "results"
        self.root.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    def exists(self, run_id: str) -> bool:
        return (self.run_dir(run_id) / "manifest.json").exists()

    @contextmanager
    def writer(self, run_id: str) -> Iterator[RunWriter]:
        final = self.run_dir(run_id)
        tmp = Path(tempfile.mkdtemp(prefix=f".{run_id}-", dir=self.root))
        w = RunWriter(tmp)
        try:
            yield w
            if final.exists():
                shutil.rmtree(final)
            tmp.rename(final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    def manifest(self, run_id: str) -> dict[str, Any]:
        return json.loads((self.run_dir(run_id) / "manifest.json").read_text(encoding="utf-8"))

    def read(self, run_id: str, table: str) -> pd.DataFrame:
        return gpq.read_table(self.run_dir(run_id) / f"{table}.parquet")

    def has_table(self, run_id: str, table: str) -> bool:
        return (self.run_dir(run_id) / f"{table}.parquet").exists()

    def list_runs(self) -> list[dict[str, Any]]:
        out = []
        for m in sorted(self.root.glob("run-*/manifest.json")):
            data = json.loads(m.read_text(encoding="utf-8"))
            out.append({"run_id": data["run_id"], "experiment": data["config"]["experiment"],
                        "release": data["config"]["release"], "created": data.get("finished_at")})
        return out
