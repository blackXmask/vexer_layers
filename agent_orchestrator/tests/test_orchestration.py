"""
Comprehensive Test Suite for Domain 6: AI Agents & Agent Orchestration.
Validates:
1. Enterprise Tool Registry (Sandboxing, RBAC, Provenance, Execution Telemetry)
2. Supervisor Agent Planning (LLM Reasoning Decomposition)
3. Multi-Agent Execution (Market, Risk, Legal Agents)
4. True LangGraph Human-in-the-Loop Checkpointing (Interrupt & Resume)
5. SQLite Persistent Checkpointer & State Recovery
6. FastAPI Enterprise REST Gateway (Start, Trace, Approve)
"""
import uuid
import sqlite3
import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from fastapi.testclient import TestClient

from agent_orchestrator.models import AgentRole, OrchestratorState, SubTask, TaskStatus
from agent_orchestrator.orchestrator import build_orchestration_graph, build_checkpoint_serde, router
from agent_orchestrator.tools import ToolRegistry
from agent_orchestrator.bus import ToolBus
from agent_orchestrator.llm_engine import EnterpriseLLMEngine, LLMConfig, ModelProvider
from agent_orchestrator.api import app


@pytest.fixture
def isolated_bus():
    """
    Give the test its own :class:`ToolBus`, then restore the default.

    Since P7 the bus holds all tool-bus state (breakers, audit sink, RBAC). Tests that exercise
    breaker or audit behaviour must not reach into module globals to reset it — they build an
    isolated bus, which is also the seam a worker uses in production. The restore in ``finally``
    keeps the default bus clean for the rest of the module.
    """
    previous = ToolRegistry._bus
    bus = ToolBus.from_config({})
    ToolRegistry.set_bus(bus)
    try:
        yield bus
    finally:
        ToolRegistry.set_bus(previous)


def test_tool_registry_rbac_and_audit():
    """Verify tool sandboxing, RBAC permission enforcement, and execution telemetry."""
    ToolRegistry.clear_audit_trail()

    # Authorized call. Person A's Domain 5 does not exist, so the honest answer is "no data" —
    # the result must SAY that rather than inventing relationships (§19, §41).
    res = ToolRegistry.query_knowledge_graph("Vexer Corp", caller_agent="MARKET_INTELLIGENCE")
    assert res["entity"] == "Vexer Corp"
    assert res["relationships"] == []
    assert res["data_status"] == "UNAVAILABLE"
    assert res["provider"].startswith("unavailable:")

    # Unauthorized call should raise PermissionError
    with pytest.raises(PermissionError):
        ToolRegistry.query_osint_signals("Cyber Signals", caller_agent="LEGAL_REGULATORY")

    # Verify audit telemetry trail
    audit = ToolRegistry.get_audit_trail()
    assert len(audit) == 2
    assert audit[0].status == "SUCCESS"
    assert audit[0].caller_agent == "MARKET_INTELLIGENCE"
    # The audit record itself carries the degraded status, so a reader of the trail can tell.
    assert audit[0].data_status == "UNAVAILABLE"
    assert audit[1].status == "PERMISSION_DENIED"
    assert audit[1].caller_agent == "LEGAL_REGULATORY"


def test_enterprise_llm_reasoning():
    """Verify that the Enterprise LLM engine decomposes inquiries into multi-domain tasks."""
    engine = EnterpriseLLMEngine(config=LLMConfig(provider=ModelProvider.MOCK))
    response = engine.generate(
        system_prompt="Decompose inquiry into subtasks",
        user_prompt="Evaluate EU sovereign AI tender for military defense"
    )
    assert response.parsed_json is not None
    subtasks = response.parsed_json.get("subtasks", [])
    assert len(subtasks) >= 2
    roles = [s["target_agent"] for s in subtasks]
    assert "MARKET_INTELLIGENCE" in roles or "OPPORTUNITY_RISK" in roles or "LEGAL_REGULATORY" in roles


def test_supervisor_planning_and_agent_dispatch():
    """Verify supervisor state graph dispatches to Market, Risk, and Legal agents."""
    graph = build_orchestration_graph(checkpointer=MemorySaver(serde=build_checkpoint_serde()))
    session_id = str(uuid.uuid4())
    state = OrchestratorState(
        session_id=session_id,
        user_query="Evaluate strategic competitor positioning and market trends"
    )
    result = graph.invoke(state, config={"configurable": {"thread_id": session_id}})

    assert result is not None
    assert len(result["tasks"]) >= 1
    # Check that completed tasks carry confidence scores and source citations
    for t in result["tasks"].values():
        if t.status == TaskStatus.COMPLETED:
            assert t.confidence_score > 0.8
            assert len(t.citations) > 0
    assert len(result["artifacts"]) > 0


def test_true_human_in_the_loop_interrupt_and_resume():
    """Verify LangGraph interrupt freezes graph state and resumes upon human decision."""
    saver = MemorySaver()
    graph = build_orchestration_graph(checkpointer=saver)

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    state = OrchestratorState(
        session_id=thread_id,
        user_query="Defense tender for autonomous agent system with cross-border transfer"
    )

    # 1. First execution should run agents and halt at the human gate
    res1 = graph.invoke(state, config=config)
    assert res1.get("pending_human_approval") is True

    snapshot = graph.get_state(config)
    assert snapshot.next == ("human_gate",)
    assert len(snapshot.tasks) > 0
    assert len(snapshot.tasks[0].interrupts) > 0
    interrupt_payload = snapshot.tasks[0].interrupts[0].value
    assert "Governance Officer" in interrupt_payload["prompt"]

    # 2. External reviewer approves the workflow
    res2 = graph.invoke(
        Command(resume={"approved": True, "feedback": "Approved by Senior Legal Counsel"}),
        config=config
    )
    assert res2.get("is_completed") is True
    assert res2.get("human_approved") is True
    assert "Approved by Senior Legal Counsel" in res2.get("final_response")


def test_sqlite_persistent_checkpointing(tmp_path):
    """Verify that agent graph states survive across SQLite checkpointer re-instantiations."""
    db_file = tmp_path / "test_checkpoints.db"
    conn1 = sqlite3.connect(str(db_file), check_same_thread=False)
    saver1 = SqliteSaver(conn1, serde=build_checkpoint_serde())
    graph1 = build_orchestration_graph(checkpointer=saver1)

    thread_id = f"sess_{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}}

    state = OrchestratorState(
        session_id=thread_id,
        user_query="Defense tender with cross-border data transfer"
    )
    graph1.invoke(state, config=config)
    conn1.close()

    # Re-open database with a new connection and new graph instance
    conn2 = sqlite3.connect(str(db_file), check_same_thread=False)
    saver2 = SqliteSaver(conn2, serde=build_checkpoint_serde())
    graph2 = build_orchestration_graph(checkpointer=saver2)

    # Verify state was persisted on disk and graph is waiting at human_gate
    persisted_state = graph2.get_state(config)
    assert persisted_state is not None
    assert persisted_state.next == ("human_gate",)
    assert len(persisted_state.tasks[0].interrupts) > 0

    # Resume on the new instance
    res = graph2.invoke(
        Command(resume={"approved": True, "feedback": "Resumed from persistent disk state"}),
        config=config
    )
    assert res.get("is_completed") is True
    conn2.close()


def test_fastapi_enterprise_endpoints():
    """Verify REST gateway lifecycle: trigger -> inspect interrupt -> approve -> finalized."""
    client = TestClient(app)

    # 1. Health endpoint
    h = client.get("/health")
    assert h.status_code == 200
    assert h.json()["status"] == "HEALTHY"

    # 2. Start workflow that triggers human gate
    res_start = client.post("/workflows/start", json={
        "query": "EU Defense AI RFP with cross-border data transfer and high risk"
    })
    assert res_start.status_code == 200
    data_start = res_start.json()
    sess_id = data_start["session_id"]
    assert data_start["pending_human_approval"] is True
    assert data_start["interrupt_details"] is not None

    # 3. Approve via governance API
    res_approve = client.post("/workflows/approve", json={
        "session_id": sess_id,
        "approved": True,
        "feedback": "Approved for official RFP bidding",
        "reviewer_id": "chief-compliance-officer"
    })
    assert res_approve.status_code == 200
    data_approve = res_approve.json()
    assert data_approve["is_completed"] is True
    assert data_approve["pending_human_approval"] is False
    assert "chief-compliance-officer" in data_approve["final_response"]

    # 4. Check tool audit trail
    res_audit = client.get("/audit/tools")
    assert res_audit.status_code == 200
    body = res_audit.json()
    # P7: the endpoint returns a structured envelope, and declares whether the audit trail is
    # per-process or shared, so a reader cannot mistake a worker-local fragment for the real trail.
    assert body["count"] >= 0
    assert isinstance(body["records"], list)
    assert body["bus"]["breaker_distribution"] == "single-process"
    assert body["bus"]["audit_distribution"] == "single-process"



# ---------------------------------------------------------------------------
# Production hardening: fail-closed governance, failure isolation, auth, limits
# ---------------------------------------------------------------------------

def test_fail_closed_governance_when_agent_fails(monkeypatch):
    """A failed agent must FORCE human approval - never auto-approve an incomplete analysis."""
    from agent_orchestrator.agents import LegalRegulatoryAgent

    def _boom(self, subtask):
        raise RuntimeError("legal agent crashed")

    monkeypatch.setattr(LegalRegulatoryAgent, "execute", _boom)

    graph = build_orchestration_graph(checkpointer=MemorySaver(serde=build_checkpoint_serde()))
    sid = str(uuid.uuid4())
    result = graph.invoke(
        OrchestratorState(
            session_id=sid,
            user_query="EU Defense AI tender with cross-border data transfer"
        ),
        config={"configurable": {"thread_id": sid}}
    )

    # FAIL-CLOSED: gate enforced, workflow not auto-completed
    assert result["pending_human_approval"] is True
    assert result["is_completed"] is False

    legal = [t for t in result["tasks"].values() if t.target_agent == AgentRole.LEGAL_REGULATORY]
    assert legal and legal[0].status == TaskStatus.FAILED

    artifact = result["artifacts"][0]
    assert artifact.risk_level == "HIGH"
    assert artifact.requires_human_approval is True
    assert any("Incomplete analysis" in r for r in artifact.recommendations)
    assert "FAILED" in artifact.summary


def test_domain_failure_isolated_and_falls_back(monkeypatch, isolated_bus):
    """A crashing domain is isolated: the failure is audited and the caller sees no fabricated data."""
    import business_market_intelligence.service as d7_service

    def _boom(self, topic, limit=None):
        raise RuntimeError("D7 market store unreachable")

    breaker = isolated_bus._breakers.breaker("market")
    breaker.reset()
    monkeypatch.setattr(d7_service.MarketIntelligenceService, "get_signals", _boom)

    ToolRegistry.clear_audit_trail()
    signals = ToolRegistry.query_osint_signals("defense AI", caller_agent="MARKET_INTELLIGENCE")

    # No invented signals: the caller gets an empty list and an explicit FAILED status.
    assert signals == []
    record = ToolRegistry.get_audit_trail()[0]
    assert record.provider == "error:market"
    assert record.data_status == "FAILED"
    # The tool call itself succeeded; the downstream dependency did not. Both facts are recorded.
    assert record.status == "SUCCESS"
    assert breaker.state == "closed"  # one failure is below the threshold
    breaker.reset()


def test_circuit_breaker_opens_and_short_circuits(monkeypatch, isolated_bus):
    """After N consecutive failures the breaker opens and skips the domain entirely."""
    import business_market_intelligence.service as d7_service

    def _boom(self, topic, limit=None):
        raise RuntimeError("still down")

    breaker = isolated_bus._breakers.breaker("market")
    breaker.reset()
    monkeypatch.setattr(d7_service.MarketIntelligenceService, "get_signals", _boom)

    for _ in range(3):
        ToolRegistry.query_osint_signals("x", caller_agent="MARKET_INTELLIGENCE")
    assert breaker.state == "open"

    # While open the domain is not called at all, and the reason is visible in the audit trail.
    ToolRegistry.clear_audit_trail()
    ToolRegistry.query_osint_signals("x", caller_agent="MARKET_INTELLIGENCE")
    record = ToolRegistry.get_audit_trail()[0]
    assert record.provider == "circuit-open:market"
    assert record.data_status == "DEGRADED"

    breaker.reset()


def test_audit_log_is_bounded():
    """Audit trail is capped so long-running servers cannot leak memory."""
    previous = ToolRegistry._bus
    ToolRegistry.set_bus(ToolBus.from_config({"tools": {"audit": {"max_records": 25}}}))
    try:
        ToolRegistry.clear_audit_trail()
        for _ in range(200):
            ToolRegistry.query_osint_signals("x", caller_agent="MARKET_INTELLIGENCE")
        assert len(ToolRegistry.get_audit_trail()) == 25
        # A truncated audit reports itself as degraded rather than looking complete (§19).
        health = ToolRegistry.bus_health()
        assert health["audit_dropped"] == 175
        assert health["data_status"] == "DEGRADED"
    finally:
        ToolRegistry.set_bus(previous)


def test_api_key_authentication(monkeypatch):
    """When auth is enabled every route except /health requires a valid X-API-Key."""
    from agent_orchestrator import auth

    monkeypatch.setattr(auth, "configured_api_key", lambda: "s3cret")
    client = TestClient(app)

    assert client.get("/health").status_code == 200                      # probe stays open
    body = {"query": "EU defense AI tender with data transfer"}
    assert client.post("/workflows/start", json=body).status_code == 401  # missing key
    assert client.post(
        "/workflows/start", json=body, headers={"X-API-Key": "wrong"}
    ).status_code == 401                                                # wrong key
    assert client.post(
        "/workflows/start", json=body, headers={"X-API-Key": "s3cret"}
    ).status_code == 200                                                # valid key


def test_input_validation_limits():
    """Request models reject out-of-range input before any work is done."""
    client = TestClient(app)
    assert client.post("/workflows/start", json={"query": "x"}).status_code == 422
    assert client.post("/workflows/start", json={"query": "a" * 5000}).status_code == 422
    assert client.post(
        "/workflows/approve",
        json={"session_id": "s" * 200, "approved": True}
    ).status_code == 422



# ---------------------------------------------------------------------------
# Person A integration seams (Domains 1, 4, 5) - see INTEGRATION.md
# ---------------------------------------------------------------------------

class _FakeKG:
    def query_entity(self, entity_name):
        return {
            "entity": entity_name,
            "relationships": [{"relation": "SUPPLIES", "target": "EU-MoD"}],
            "verified_facts": [{"fact": "real graph fact", "confidence": 0.99}],
        }


class _FakeDocs:
    def search(self, query, limit=5):
        return {"documents": [{"title": "ISO27001 certificate", "snippet": "valid until 2027",
                               "source": "internal", "score": 0.93}]}


class _FakeOrg:
    def get_company_context(self, topic):
        return {"capabilities": ["sovereign deployment"], "certifications": ["ISO27001"]}


def test_person_a_seams_inactive_by_default():
    """
    With flags off and packages absent, seams report an explicit gap.

    The behaviour changed in P7: these used to return a static "builtin" payload (invented
    relationships and a fixed regulation list). They now return empty results tagged
    ``UNAVAILABLE``, which is the difference between a visible gap and invented intelligence.
    """
    ToolRegistry.clear_audit_trail()

    docs = ToolRegistry.search_documents("security", caller_agent="MARKET_INTELLIGENCE")
    ctx = ToolRegistry.query_company_context("defense AI", caller_agent="MARKET_INTELLIGENCE")
    kg = ToolRegistry.query_knowledge_graph("Vexer Corp", caller_agent="MARKET_INTELLIGENCE")

    assert docs["documents"] == []
    assert ctx["context"] == {}
    assert kg["relationships"] == []
    for result in (docs, ctx, kg):
        assert result["data_status"] == "UNAVAILABLE"
    providers = [r.provider for r in ToolRegistry.get_audit_trail()]
    assert providers == [
        "unavailable:documents",
        "unavailable:org",
        "unavailable:knowledge_graph",
    ]


def test_person_a_seams_activate_when_provider_present():
    """When Person A's service is resolvable, results and provider tags flow through."""
    fakes = {
        "knowledge_graph": _FakeKG(),
        "documents": _FakeDocs(),
        "org": _FakeOrg(),
    }
    previous = ToolRegistry._bus
    ToolRegistry.set_bus(
        ToolBus.from_config({}, service_resolver=lambda key: fakes.get(key))
    )
    try:
        ToolRegistry.clear_audit_trail()
        kg = ToolRegistry.query_knowledge_graph("Vexer Corp", caller_agent="MARKET_INTELLIGENCE")
        docs = ToolRegistry.search_documents("security", caller_agent="MARKET_INTELLIGENCE")
        ctx = ToolRegistry.query_company_context("defense AI", caller_agent="MARKET_INTELLIGENCE")

        assert kg["verified_facts"][0]["fact"] == "real graph fact"
        assert docs["documents"][0]["title"] == "ISO27001 certificate"
        assert ctx["context"]["certifications"] == ["ISO27001"]
        for result in (kg, docs, ctx):
            assert result["data_status"] == "LIVE"

        providers = [r.provider for r in ToolRegistry.get_audit_trail()]
        assert providers == ["knowledge_graph", "documents", "org"]
    finally:
        ToolRegistry.set_bus(previous)


def test_person_a_seam_failure_falls_back_safely():
    """A crashing Person A service must not break the workflow, and must not be disguised."""
    class _Broken:
        def query_entity(self, entity_name):
            raise RuntimeError("graph down")

    previous = ToolRegistry._bus
    ToolRegistry.set_bus(
        ToolBus.from_config(
            {}, service_resolver=lambda key: _Broken() if key == "knowledge_graph" else None
        )
    )
    try:
        ToolRegistry.clear_audit_trail()
        kg = ToolRegistry.query_knowledge_graph("Vexer Corp", caller_agent="MARKET_INTELLIGENCE")
        # The call still returns a well-formed result; it is explicitly empty and FAILED.
        assert kg["entity"] == "Vexer Corp"
        assert kg["relationships"] == []
        record = ToolRegistry.get_audit_trail()[0]
        assert record.provider == "error:knowledge_graph"
        assert record.data_status == "FAILED"
    finally:
        ToolRegistry.set_bus(previous)


def test_person_a_seam_tools_enforce_rbac():
    """New seam tools honour the same RBAC rules as every other tool."""
    for tool, args in (
        ("search_documents", ("q",)),
        ("query_company_context", ("q",)),
    ):
        with pytest.raises(PermissionError):
            getattr(ToolRegistry, tool)(*args, caller_agent="LEGAL_REGULATORY")
        assert ToolRegistry.get_audit_trail()[-1].status == "PERMISSION_DENIED"


# ---------------------------------------------------------------------------
# Flat decision contract + planner coverage / ordering (Phase 1 & 2)
# ---------------------------------------------------------------------------

def test_decision_report_flat_contract():
    """One query -> flat {query, market, risk, legal, decision} synthesis report."""
    from agent_orchestrator.config import load_config

    graph = build_orchestration_graph(checkpointer=MemorySaver(serde=build_checkpoint_serde()))
    sid = str(uuid.uuid4())
    query = "Expand to UAE? market risk legal compliance"
    result = graph.invoke(
        OrchestratorState(session_id=sid, user_query=query),
        config={"configurable": {"thread_id": sid}}
    )

    report = result["decision_report"]
    assert report.query == query
    for field in ("market", "risk", "legal", "decision"):
        assert getattr(report, field), f"decision report field '{field}' must not be empty"

    cfg = load_config()["orchestrator"]["decision_report"]
    allowed = {rule["decision"] for rule in cfg["decision_matrix"]} | {cfg["verdict_fallback"]}
    assert report.decision in allowed
    assert report.decision_reason
    assert 0.0 < report.confidence <= 1.0
    assert report.citations
    assert report.requires_human_approval == result["artifacts"][0].requires_human_approval
    assert set(report.contributor_agents) == {
        AgentRole.MARKET_INTELLIGENCE.value,
        AgentRole.OPPORTUNITY_RISK.value,
        AgentRole.LEGAL_REGULATORY.value,
    }


def test_decision_report_flows_through_rest_gateway():
    """The flat decision report is part of the enterprise REST contract."""
    client = TestClient(app)
    query = "Expand to UAE? market risk legal compliance"

    res = client.post("/workflows/start", json={"query": query})
    assert res.status_code == 200
    report = res.json()["decision_report"]
    assert report is not None
    assert report["query"] == query
    assert all(report[field] for field in ("market", "risk", "legal", "decision"))


def test_rule_based_planner_covers_expansion_query():
    """Spec headline query plans all three domain agents (rule-based planner)."""
    graph = build_orchestration_graph(checkpointer=MemorySaver(serde=build_checkpoint_serde()))
    sid = str(uuid.uuid4())
    result = graph.invoke(
        OrchestratorState(session_id=sid, user_query="Should we expand to UAE?"),
        config={"configurable": {"thread_id": sid}}
    )

    roles = {task.target_agent for task in result["tasks"].values()}
    assert roles == {
        AgentRole.MARKET_INTELLIGENCE,
        AgentRole.OPPORTUNITY_RISK,
        AgentRole.LEGAL_REGULATORY,
    }
    assert sorted(task.sequence for task in result["tasks"].values()) == [0, 1, 2]

    report = result["decision_report"]
    assert report.legal and report.legal != "Not evaluated"   # UAE matched a real regulation
    assert "compliance" in report.decision.lower()
    assert report.requires_human_approval is True             # HIGH severity regulation forces sign-off


def test_router_honours_planner_sequence():
    """Router executes the planner's declared order, not a hard-coded role order."""
    tasks = {
        "t_legal": SubTask(
            task_id="t_legal", target_agent=AgentRole.LEGAL_REGULATORY,
            description="Legal review", sequence=0
        ),
        "t_market": SubTask(
            task_id="t_market", target_agent=AgentRole.MARKET_INTELLIGENCE,
            description="Market review", sequence=1
        ),
    }
    assert router(OrchestratorState(session_id="seq", user_query="q", tasks=tasks)) == "legal"

    tasks["t_legal"].status = TaskStatus.COMPLETED
    assert router(OrchestratorState(session_id="seq", user_query="q", tasks=tasks)) == "market"

    tasks["t_market"].status = TaskStatus.COMPLETED
    assert router(OrchestratorState(session_id="seq", user_query="q", tasks=tasks)) == "synthesis"

