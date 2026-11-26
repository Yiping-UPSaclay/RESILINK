"""Operation Registry projections: CLI-style calls, agent loop (scripted fake model), MCP server."""

from __future__ import annotations

import asyncio

import pytest

from resilink.domain.vocab import Tier
from resilink.ops import REGISTRY, SideEffect, call, load_builtin_ops
from resilink.ops.recording import make_context
from resilink_ai.gateway import AgentSession
from resilink_ai.testing import FakeClient, last_tool_results

load_builtin_ops()


@pytest.fixture()
def human():
    return make_context("human:test", tier=Tier.INTERNAL, autonomy=SideEffect.ADMIN)


def test_ops_roundtrip_and_explanation(small_run, human):
    m = call("experiments.metrics", {"run_id": small_run.run_id, "metric": "eal", "sector": "all"}, human)
    assert m.call_id.startswith("call-") and m.output.rows[0]["value"] > 0
    top = call("experiments.top_assets", {"run_id": small_run.run_id, "scenario_id": "flood_rp100", "layer": "telecom"}, human)
    assert top.output.assets and top.output.assets[0]["p_down"] >= top.output.assets[-1]["p_down"]
    rid = top.output.assets[0]["rid"]
    ex = call("experiments.trace_explain", {"run_id": small_run.run_id, "scenario_id": "flood_rp100", "rid": rid}, human)
    assert ex.output.steps[0]["rid"] == rid


def test_findings_need_human_approval(small_run, human):
    agent = make_context("agent:analyst", tier=Tier.INTERNAL, autonomy=SideEffect.PROPOSE)
    p = call("findings.propose", {"statement": "EAL is positive", "value_refs": [{"run_id": small_run.run_id, "table": "metrics"}],
                                  "run_ids": [small_run.run_id], "release_id": "synthetic-2026.11.0"}, agent)
    assert p.output.status == "proposed"
    d = call("proposals.decide", {"proposal_id": p.output.proposal_id, "approve": True}, human)
    assert d.output.status == "approved"
    assert len(human.extras["knowledge"].findings()) >= 1


def test_views_render_validates_value_refs(small_run, human):
    good = {"view": {"title": "EAL by sector", "charts": [{"kind": "bar", "title": "EAL", "x": "sector", "y": "value",
             "data": {"run_id": small_run.run_id, "table": "metrics", "filters": {"metric": "eal"}}}]}}
    assert call("views.render", good, human).output.valid
    bad = {"view": {"title": "x", "charts": [{"kind": "bar", "title": "t", "x": "nope", "y": "value",
            "data": {"run_id": small_run.run_id, "table": "metrics"}}]}}
    out = call("views.render", bad, human).output
    assert not out.valid and "nope" in out.errors[0]


def test_agent_loop_grounded_answer_with_provenance(small_run):
    def answer(req):
        rows = last_tool_results(req)[0]["output"]["rows"]
        v = next(r for r in rows if r["sector"] == "all")["value"]
        return {"text": f"The expected annual loss is {v:,.0f} people-hours per year."}

    fake = FakeClient([{"tools": [("experiments__metrics", {"run_id": small_run.run_id, "metric": "eal"})]}, answer])
    s = AgentSession("copilot", client=fake)
    turn = s.ask("What is the EAL?")
    assert turn.grounding.ok and turn.grounding.checked == 1
    assert turn.tool_calls[0].ok and turn.tool_calls[0].call_id
    prov = s.knowledge.provenance(s.session_id)
    assert len(prov) == 1 and prov.iloc[0]["model"] == "claude-opus-5-5"
    req = fake.messages.requests[0]
    assert req["output_config"] == {"effort": "medium"} and req["thinking"] == {"type": "adaptive"}
    assert req["fallbacks"] == "default"
    assert [t["name"] for t in req["tools"]] == sorted(t["name"] for t in req["tools"])  # cache-stable order


def test_agent_hallucination_is_flagged_and_admin_tools_unavailable():
    fake = FakeClient([{"tools": [("proposals__decide", {"proposal_id": "x", "approve": True})]},
                       {"text": "Approved. The EAL is 999,999 people-hours."}])
    s = AgentSession("copilot", client=fake)
    turn = s.ask("approve it")
    assert not turn.tool_calls[0].ok and "not available" in turn.tool_calls[0].error
    assert not turn.grounding.ok and "999,999" in turn.grounding.ungrounded


def test_mcp_server_projects_the_registry():
    from resilink_mcp.server import build_server

    srv = build_server()
    tools = asyncio.run(srv.list_tools())
    expected = {s.tool_name for s in REGISTRY.allowed(max_side_effect=SideEffect.COMPUTE, tier=Tier.INTERNAL)}
    assert {t.name for t in tools} == expected
    res = asyncio.run(srv.call_tool("hazards__list_scenarios", {}))
    assert "flood_rp100" in str(res)


def test_staged_tables_lists_real_data_by_clearance(human):
    from datetime import UTC, datetime

    from resilink.etl.staging import StagedTable, staging_dir
    from resilink.io.hashing import canonical_json

    d = staging_dir("optest", human.data)
    d.mkdir(parents=True, exist_ok=True)
    for table, tier in (("grid_cells", "public"), ("nodes", "internal")):
        rec = StagedTable(table=table, territory="optest", path=f"{table}.parquet", rows=3, sha256="0" * 64,
                          sensitivity=tier, adapter="x@1.0.0", snapshots=["s@1#abc"], licences={"s": "LO 2.0"},
                          qa={"cells": 3}, staged_at=datetime.now(UTC))
        (d / f"{table}.json").write_text(canonical_json(rec), encoding="utf-8")
    public = make_context("agent:steward", tier=Tier.PUBLIC, autonomy=SideEffect.READ)
    out = call("etl.staged_tables", {"territory": "optest"}, public).output
    assert [t.table for t in out.tables] == ["grid_cells"] and out.withheld == 1
    assert out.tables[0].qa == {"cells": 3}
    full = call("etl.staged_tables", {"territory": "optest"}, human).output
    assert {t.table for t in full.tables} == {"grid_cells", "nodes"} and full.withheld == 0
