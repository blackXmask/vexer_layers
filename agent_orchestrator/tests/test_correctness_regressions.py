"""
Regression tests for the correctness defects found in the deep system review.

Each test corresponds to a defect that shipped and was fixed, so the defect cannot return silently
and the *reason* stays recorded next to the assertion.

Fixed defects
-------------
1. **Governance was bypassable by rewording.** The HITL gate fired off planner keyword matching, so
   "Should we expand to UAE?" required approval while "What is our market position?" auto-approved,
   from identical data. Rephrasing a question removed human oversight.
2. **Hardcoded intelligence shipped in the decision report** - two constant strings plus three
   configured confidences (0.94/0.91/0.96) for every query.
3. **A fabricated IP clearance** ("No trademark or patent blocking conflicts identified") asserted by
   no analysis.
4. **Confidence was an arithmetic mean of constants** - 0.937 for a workflow with an empty evidence
   base - so one failed leg was hidden behind two good ones.
5. **Static market segments were returned for any topic**, so an unrelated report still claimed $12B.
6. **`/workflows/start` was not idempotent**; a client retry paid twice.
7. **The trace endpoint did not exist** despite being documented.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

import agent_orchestrator.api as api_module
from agent_orchestrator.orchestrator import _aggregate_confidence
from agent_orchestrator.agents import (
    MarketIntelligenceAgent,
    _ip_risk_note,
    _legal_confidence,
    _market_report,
    _risk_confidence,
)
from agent_orchestrator.bus import ToolBus
from agent_orchestrator.models import DecisionReport
from agent_orchestrator.tools import ToolRegistry
from business_market_intelligence.service import MarketIntelligenceService


@pytest.fixture
def bus():
    """Isolated bus so a test never inherits another's breaker or audit state."""
    previous = ToolRegistry._bus
    ToolRegistry.set_bus(ToolBus.from_config({}))
    try:
        yield ToolRegistry.get_bus()
    finally:
        ToolRegistry.set_bus(previous)
        api_module._idempotency_registry.clear()


@pytest.fixture
def client():
    return TestClient(api_module.app)


# -------------------------------------------------------------------------------------------
# 1. Governance cannot be bypassed by rewording
# -------------------------------------------------------------------------------------------

#: Phrasings that previously produced *different* governance outcomes from identical data.
PHRASINGS = [
    "Should we expand to UAE?",
    "analyse uae expansion",
    "What is our market position?",
    "same question",
    "hello",
    "What is the weather?",
    "xyz",
]


@pytest.mark.parametrize("query", PHRASINGS)
def test_governance_gate_cannot_be_bypassed_by_rewording(client, query):
    """
    Any query lacking full intelligence coverage must require human approval.

    Regression guard for the worst defect found: the gate was a function of planner keywords, so a
    user could remove human oversight simply by asking differently.
    """
    response = client.post(
        "/workflows/start", json={"query": query, "session_id": str(uuid.uuid4())}
    )
    assert response.status_code == 200, f"{query!r} should still be accepted"
    body = response.json()
    report = body.get("decision_report") or {}
    if report.get("evidence_gaps"):
        assert body["pending_human_approval"] is True, (
            f"{query!r} has evidence gaps {report['evidence_gaps']} but auto-approved"
        )
    assert report.get("data_status") in ("LIVE", "DEGRADED", "UNAVAILABLE")


def test_missing_coverage_forces_approval_and_escalates_risk(client):
    """A query that plans no legal leg must be escalated, not quietly auto-approved."""
    body = client.post(
        "/workflows/start", json={"query": "hello", "session_id": str(uuid.uuid4())}
    ).json()
    report = body["decision_report"]
    assert any("no analysis" in gap for gap in report["evidence_gaps"]), (
        f"missing coverage must be itemised, got {report['evidence_gaps']}"
    )
    assert body["pending_human_approval"] is True
    assert body["artifacts"][0]["risk_level"] in ("MEDIUM", "HIGH", "CRITICAL"), (
        f"missing coverage must escalate risk, got {body['artifacts'][0]['risk_level']}"
    )


# -------------------------------------------------------------------------------------------
# 2 & 3. No fabricated intelligence
# -------------------------------------------------------------------------------------------


def test_market_agent_emits_no_constant_strings():
    """The two constant strings must be gone from the source entirely."""
    import inspect

    source = inspect.getsource(MarketIntelligenceAgent)
    assert "High demand in federal" not in source
    assert "Accelerating adoption of sovereign AI" not in source


def test_market_narrative_is_empty_without_evidence():
    """No signals => no prose. Synthesis renders the gap; it must not be pre-filled."""
    assert _market_report([]) == {"posture": "", "growth": ""}


def test_market_narrative_is_derived_from_real_signals():
    signals = [
        {"source": "Wire", "headline": "A", "timestamp": "2026-09-01", "relevance": 0.8},
        {"source": "Tenders", "headline": "B", "timestamp": "2026-09-20", "relevance": 0.4},
    ]
    report = _market_report(signals)
    assert "2 signal(s)" in report["growth"]
    assert "Wire" in report["posture"] and "Tenders" in report["posture"]
    assert "mean relevance" in report["posture"]


def test_legal_agent_makes_no_ip_clearance_claim():
    """A clearance must never be asserted by an agent that ran no clearance."""
    note = _ip_risk_note({}, {"data_status": "UNAVAILABLE", "detected_regulations": []})
    assert "NOT PERFORMED" in note
    assert "No trademark" not in note and "blocking conflicts" not in note


def test_configured_ip_note_is_respected_when_supplied():
    note = _ip_risk_note({"ip_risk_note": "Operator-reviewed position"}, {"data_status": "UNAVAILABLE"})
    assert note == "Operator-reviewed position"


# -------------------------------------------------------------------------------------------
# 4. Confidence is derived, conservative and labelled
# -------------------------------------------------------------------------------------------


def test_confidence_is_the_minimum_not_the_mean():
    """
    A report must not be more confident than its least confident leg.

    The old arithmetic mean of 0.94/0.91/0.96 produced 0.937 while the evidence base was empty.
    """
    assert _aggregate_confidence([0.9, 0.9, 0.2], ["measured"] * 3) == 0.2


def test_heuristic_and_prior_legs_are_capped():
    assert _aggregate_confidence([0.95], ["heuristic"]) == 0.4
    assert _aggregate_confidence([0.95], ["prior-no-evidence"]) == 0.5
    assert _aggregate_confidence([0.95], ["insufficient-evidence"]) == 0.3


def test_measured_legs_pass_through():
    assert _aggregate_confidence([0.82, 0.74], ["measured", "measured"]) == 0.74


def test_empty_evidence_is_zero_confidence():
    assert _aggregate_confidence([], []) == 0.0


def test_legal_confidence_reflects_whether_a_check_actually_ran():
    assert _legal_confidence({"data_status": "UNAVAILABLE"}) < 0.3
    assert _legal_confidence({"data_status": "LIVE", "compliance_status": "FLAGGED"}) > 0.8
    assert _legal_confidence({"data_status": "LIVE", "compliance_status": "UNKNOWN"}) < 0.6


def test_risk_confidence_caps_a_perfect_keyword_match():
    """100% keyword overlap is not certainty, so confidence must stay below 1.0."""
    assert _risk_confidence({"data_status": "LIVE", "fit_score": 100.0}, {}) < 1.0
    assert _risk_confidence({"data_status": "FALLBACK", "fit_score": 100.0}, {}) < 0.5



def test_report_exposes_provenance_fields(client):
    """The report must say which capabilities answered and what was missing."""
    body = client.post(
        "/workflows/start", json={"query": "Should we expand to UAE?", "session_id": str(uuid.uuid4())}
    ).json()
    report = body["decision_report"]
    assert report["providers"], "a report must name the capabilities that answered"
    assert set(report["providers"]) <= {
        "market", "opportunity", "legal", "knowledge_graph", "documents", "org", "osint"
    }
    assert report["data_status"] in ("LIVE", "DEGRADED", "UNAVAILABLE")
    assert report["confidence_basis"] in ("measured", "mixed", "none")
    assert isinstance(report["evidence_gaps"], list)


def test_decision_report_defaults_are_honest():
    """A default-constructed report must not claim live data or measured confidence."""
    report = DecisionReport(query="q")
    assert report.data_status == "UNAVAILABLE"
    assert report.confidence_basis == "none"
    assert report.providers == []
    assert report.evidence_gaps == []


# -------------------------------------------------------------------------------------------
# 5. Static segments are no longer served for any topic
# -------------------------------------------------------------------------------------------


def test_segments_are_topic_scoped():
    service = MarketIntelligenceService()
    assert service.get_segments("sovereign AI platforms")
    assert service.get_segments("TOTALLY UNRELATED TOPIC") == [], (
        "an unrelated topic must not be handed a $12B TAM"
    )


def test_segments_declare_they_are_estimates():
    service = MarketIntelligenceService()
    for segment in service.get_segments("defense autonomy"):
        assert segment.is_estimate is True
        assert segment.source
        assert segment.data_status in ("DEGRADED", "FALLBACK")


def test_segments_without_a_topic_are_flagged_fallback():
    service = MarketIntelligenceService()
    for segment in service.get_segments():
        assert segment.data_status == "FALLBACK"
        assert segment.topic_relevance is None


# -------------------------------------------------------------------------------------------
# 6 & 7. Idempotency and the trace endpoint
# -------------------------------------------------------------------------------------------


def test_start_is_idempotent_with_a_key(client):
    key = f"k-{uuid.uuid4().hex[:8]}"
    payload = {"query": "Should we expand to UAE?", "idempotency_key": key}
    first = client.post("/workflows/start", json=payload).json()
    second = client.post("/workflows/start", json=payload).json()
    assert first["session_id"] == second["session_id"], "a retry must not create a second workflow"
    assert second["replayed"] is True


def test_distinct_keys_create_distinct_workflows(client):
    a = client.post(
        "/workflows/start", json={"query": "q one", "idempotency_key": f"k{uuid.uuid4().hex[:6]}"}
    )
    b = client.post(
        "/workflows/start", json={"query": "q two", "idempotency_key": f"k{uuid.uuid4().hex[:6]}"}
    )
    assert a.json()["session_id"] != b.json()["session_id"]


def test_trace_endpoint_exists_and_explains_the_run(client):
    """The README and test suite described a Trace endpoint that did not exist."""
    started = client.post(
        "/workflows/start", json={"query": "Should we expand to UAE?", "session_id": str(uuid.uuid4())}
    ).json()
    trace = client.get(f"/workflows/{started['session_id']}/trace")
    assert trace.status_code == 200
    body = trace.json()
    assert body["query"] == "Should we expand to UAE?"
    assert body["agents"], "the trace must list the agents that ran"
    for agent in body["agents"]:
        assert "confidence_basis" in agent and "data_status" in agent
    assert "tool_audit" in body


def test_trace_for_an_unknown_session_is_404(client):
    assert client.get("/workflows/definitely-not-a-real-session/trace").status_code == 404

