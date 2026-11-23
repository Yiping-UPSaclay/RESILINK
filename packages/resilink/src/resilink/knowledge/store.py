"""DuckDB-backed machine knowledge store.

Short-lived connections per call (DuckDB allows one writer process per file; short connections let the CLI,
Streamlit and the MCP server take turns). Curated knowledge stays in git (``knowledge/*.yaml``) and is
re-indexed here for search; proposals are the human-approval queue (invariant I2).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import duckdb
import pandas as pd
from pydantic import BaseModel, Field

from resilink.io.duck import load_extension
from resilink.io.hashing import canonical_json, stable_hash
from resilink.io.paths import data_root, load_yaml, resolve

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kg_entities (kid VARCHAR PRIMARY KEY, kind VARCHAR, ref VARCHAR, label VARCHAR,
    text VARCHAR, attrs_json VARCHAR, tier VARCHAR);
CREATE TABLE IF NOT EXISTS op_calls (call_id VARCHAR PRIMARY KEY, ts TIMESTAMP, principal VARCHAR,
    session_id VARCHAR, op VARCHAR, input_sha VARCHAR, output_sha VARCHAR, input_json VARCHAR, output_json VARCHAR);
CREATE TABLE IF NOT EXISTS proposals (proposal_id VARCHAR PRIMARY KEY, kind VARCHAR, title VARCHAR,
    payload_json VARCHAR, status VARCHAR, created_at TIMESTAMP, created_by VARCHAR, provenance_id VARCHAR,
    reviewer VARCHAR, decided_at TIMESTAMP, decision_note VARCHAR);
CREATE TABLE IF NOT EXISTS findings (finding_id VARCHAR PRIMARY KEY, statement VARCHAR, value_refs_json VARCHAR,
    run_ids_json VARCHAR, release_id VARCHAR, status VARCHAR, stale BOOLEAN, proposal_id VARCHAR);
CREATE TABLE IF NOT EXISTS evidence (evidence_id VARCHAR PRIMARY KEY, quantity VARCHAR, value DOUBLE, low DOUBLE,
    high DOUBLE, unit VARCHAR, conditions VARCHAR, doc_id VARCHAR, page INTEGER, quote VARCHAR,
    quote_verified BOOLEAN, status VARCHAR, reviewer VARCHAR, reviewed_at TIMESTAMP, provenance_id VARCHAR);
CREATE TABLE IF NOT EXISTS documents (doc_id VARCHAR PRIMARY KEY, title VARCHAR, url VARCHAR, licence VARCHAR,
    tier VARCHAR, sha256 VARCHAR, pages INTEGER);
CREATE TABLE IF NOT EXISTS chunks (chunk_id VARCHAR PRIMARY KEY, doc_id VARCHAR, page INTEGER, text VARCHAR);
CREATE TABLE IF NOT EXISTS ai_provenance (provenance_id VARCHAR PRIMARY KEY, session_id VARCHAR, role VARCHAR,
    provider VARCHAR, model VARCHAR, effort VARCHAR, prompt_sha VARCHAR, tool_calls_json VARCHAR,
    usage_json VARCHAR, cost_usd DOUBLE, stop_reason VARCHAR, started_at TIMESTAMP, ended_at TIMESTAMP,
    approved_by VARCHAR);
"""


class ValueRef(BaseModel):
    """The only way a generated view or answer may show a number (invariant I1)."""

    run_id: str | None = None
    call_id: str | None = None
    table: Literal["metrics", "cell_stats", "node_stats", "sample_losses", "exposure"] | None = None
    column: str | None = None
    filters: dict[str, str] = Field(default_factory=dict)
    agg: Literal["none", "sum", "mean", "max", "count"] = "none"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class KnowledgeStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_root() / "knowledge" / "knowledge.duckdb")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._con() as con:
            con.execute(_SCHEMA)

    @contextmanager
    def _con(self) -> Iterator[duckdb.DuckDBPyConnection]:
        con = duckdb.connect(str(self.path))
        try:
            yield con
        finally:
            con.close()

    # ---- curated knowledge -> searchable entities ---------------------------------------------------
    def refresh_curated(self) -> int:
        rows: list[tuple[str, str, str, str, str, str, str]] = []
        rules = load_yaml("knowledge/dependency_rules.yaml")
        for r in rules["rules"]:
            text = f"{r['dependent']} depends on {r['supporter']} via {r['mechanism']} ({r.get('group_mode')}). {r.get('rationale', '')} backup {r.get('backup_h')}"
            rows.append((f"rule:{r['id']}", "dependency_rule", r["id"], f"{r['dependent']} <- {r['supporter']}", text, canonical_json(r), "public"))
        assumptions = load_yaml("knowledge/assumptions.yaml")
        for a in assumptions["assumptions"]:
            rows.append((f"assumption:{a['id']}", "assumption", a["id"], a["title"], f"{a['title']}. {a['statement']} {a.get('rationale', '')}", canonical_json(a), "public"))
        onto = load_yaml("knowledge/ontology.yaml")
        for layer, classes in onto["classes"].items():
            for cls, desc in classes.items():
                rows.append((f"class:{layer}.{cls}", "class", f"{layer}.{cls}", f"{layer}.{cls}", desc, "{}", "public"))
        frag = load_yaml("configs/fragility/v1.yaml")
        for c in frag["curves"]:
            rows.append((f"fragility:{c['asset_class']}", "fragility_curve", c["assumption_id"],
                         f"flood fragility of {c['asset_class']}",
                         f"flood depth fragility {c['asset_class']} median {c['median']} m beta {c['beta']} grade {c['grade']}",
                         canonical_json(c), "public"))
        with self._con() as con:
            con.execute("DELETE FROM kg_entities WHERE kind IN ('dependency_rule','assumption','class','fragility_curve')")
            con.executemany("INSERT INTO kg_entities VALUES (?,?,?,?,?,?,?)", rows)
            load_extension(con, "fts")
            con.execute("PRAGMA create_fts_index('kg_entities', 'kid', 'label', 'text', overwrite=1)")
        return len(rows)

    def search(self, query: str, limit: int = 8) -> pd.DataFrame:
        with self._con() as con:
            load_extension(con, "fts")
            try:
                return con.execute(
                    """SELECT kid, kind, ref, label, text, score FROM (
                         SELECT *, fts_main_kg_entities.match_bm25(kid, ?) AS score FROM kg_entities)
                       WHERE score IS NOT NULL ORDER BY score DESC LIMIT ?""",
                    [query, limit],
                ).df()
            except duckdb.Error:
                return pd.DataFrame(columns=["kid", "kind", "ref", "label", "text", "score"])

    def entity_count(self) -> int:
        with self._con() as con:
            return int(con.execute("SELECT count(*) FROM kg_entities").fetchone()[0])  # type: ignore[index]

    # ---- op-call log (grounding targets, I1 / I6) -----------------------------------------------------
    def record_call(self, op: str, inp: BaseModel, out: BaseModel, principal: str, session_id: str | None) -> str:
        in_json, out_json = canonical_json(inp), canonical_json(out)
        call_id = "call-" + uuid.uuid4().hex[:12]
        with self._con() as con:
            con.execute("INSERT INTO op_calls VALUES (?,?,?,?,?,?,?,?,?)", [
                call_id, _now(), principal, session_id, op, stable_hash(in_json), stable_hash(out_json), in_json, out_json])
        return call_id

    def get_call(self, call_id: str) -> dict[str, Any] | None:
        with self._con() as con:
            row = con.execute("SELECT op, output_json FROM op_calls WHERE call_id = ?", [call_id]).fetchone()
        return None if row is None else {"op": row[0], "output": json.loads(row[1])}

    # ---- proposals (human gate, I2) ---------------------------------------------------------------------
    def add_proposal(self, kind: str, title: str, payload: dict[str, Any], created_by: str,
                     provenance_id: str | None = None) -> str:
        pid = "prop-" + uuid.uuid4().hex[:10]
        with self._con() as con:
            con.execute("INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
                pid, kind, title, canonical_json(payload), "proposed", _now(), created_by, provenance_id, None, None, None])
        return pid

    def list_proposals(self, status: str | None = None) -> pd.DataFrame:
        with self._con() as con:
            if status:
                return con.execute("SELECT * FROM proposals WHERE status = ? ORDER BY created_at", [status]).df()
            return con.execute("SELECT * FROM proposals ORDER BY created_at").df()

    def decide(self, proposal_id: str, approve: bool, reviewer: str, note: str = "") -> str:
        status = "approved" if approve else "rejected"
        with self._con() as con:
            row = con.execute("SELECT kind, payload_json, status FROM proposals WHERE proposal_id = ?", [proposal_id]).fetchone()
            if row is None:
                raise KeyError(proposal_id)
            if row[2] != "proposed":
                raise ValueError(f"{proposal_id} already {row[2]}")
            con.execute("UPDATE proposals SET status=?, reviewer=?, decided_at=?, decision_note=? WHERE proposal_id=?",
                        [status, reviewer, _now(), note, proposal_id])
            if approve and row[0] == "finding":
                p = json.loads(row[1])
                con.execute("INSERT INTO findings VALUES (?,?,?,?,?,?,?,?)", [
                    "find-" + uuid.uuid4().hex[:10], p["statement"], canonical_json(p.get("value_refs", [])),
                    canonical_json(p.get("run_ids", [])), p.get("release_id"), "accepted", False, proposal_id])
        return status

    def findings(self) -> pd.DataFrame:
        with self._con() as con:
            return con.execute("SELECT * FROM findings").df()

    # ---- AI provenance (I6) -----------------------------------------------------------------------------
    def record_provenance(self, record: dict[str, Any]) -> str:
        pid = record.get("provenance_id") or "prov-" + uuid.uuid4().hex[:12]
        with self._con() as con:
            con.execute("INSERT INTO ai_provenance VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
                pid, record.get("session_id"), record.get("role"), record.get("provider"), record.get("model"),
                record.get("effort"), record.get("prompt_sha"), canonical_json(record.get("tool_calls", [])),
                canonical_json(record.get("usage", {})), float(record.get("cost_usd", 0.0)), record.get("stop_reason"),
                record.get("started_at"), record.get("ended_at"), record.get("approved_by")])
        return pid

    def provenance(self, session_id: str | None = None) -> pd.DataFrame:
        with self._con() as con:
            if session_id:
                return con.execute("SELECT * FROM ai_provenance WHERE session_id = ? ORDER BY started_at", [session_id]).df()
            return con.execute("SELECT * FROM ai_provenance ORDER BY started_at").df()


def default_store() -> KnowledgeStore:
    return KnowledgeStore()


def curated_path(name: str) -> Path:
    return resolve(f"knowledge/{name}")
