"""Knowledge plane (blueprint §1.7): curated YAML in git + machine store in DuckDB (BM25 search, proposals,
findings, evidence, op-call log and AI provenance)."""

from resilink.knowledge.store import KnowledgeStore, ValueRef

__all__ = ["KnowledgeStore", "ValueRef"]
