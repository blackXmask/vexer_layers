"""
Comprehensive Test Suite for Domain 9: Legal, Regulatory & Intellectual Property Intelligence.
Validates:
1. Regulation + IP catalogs configured
2. Regulation detection (word-boundary matching, no 'risk'/'ai' false positives)
3. Obligations, flags and human sign-off policy
4. Cleared case (no triggers)
5. IP clearance scoring and CLEAR/REVIEW/BLOCKED decisions
6. Expiry proximity weighting (near-expiry patents weighted higher)
7. Blended legal risk + report generation
8. REST API endpoints
9. Domain 6 ToolRegistry integration (adapter + RBAC preserved)
"""
import pytest
from fastapi.testclient import TestClient

from legal_regulatory_ip_intelligence import (
    ClearanceDecision,
    ComplianceStatus,
    LegalIntelligenceService,
    Severity,
    analyze_compliance,
)
from legal_regulatory_ip_intelligence.api import app

SCOPE = "EU Defense AI tender for autonomous agent with cross-border data transfer"


@pytest.fixture()
def service():
    return LegalIntelligenceService()


def test_catalogs_configured(service):
    """Regulation catalog (8) and IP landscape (6) are complete and well-formed."""
    regs = service.get_regulations()
    assert len(regs) == 8
    for r in regs:
        assert r["id"] and r["name"] and r["jurisdiction"]
        assert r["severity"] in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
        assert len(r["triggers"]) > 0
        assert len(r["obligations"]) > 0
        assert r["penalty_note"]

    ip_records = service.get_ip_landscape()
    assert len(ip_records) == 6
    for rec in ip_records:
        assert rec["ip_id"] and rec["owner"] and rec["status"]
        assert rec["type"] in {"PATENT", "TRADEMARK"}

    health = service.health()
    assert health["status"] == "HEALTHY"
    assert health["regulation_count"] == 8


def test_regulation_detection_with_word_boundaries(service):
    """Triggers match real words only; 'risk' must not trigger the 'ai' keyword."""
    analysis = service.analyze(SCOPE)
    ids = [r.regulation_id for r in analysis.regulations]
    assert "GDPR" in ids          # cross-border / data transfer
    assert "EU-AI-ACT" in ids    # ai / agent / defense
    assert "EXPORT-CTRL" in ids  # defense
    assert "PROCUREMENT" in ids  # tender

    # word-boundary safety: plain risk-management text does not trigger AI Act
    plain = analyze_compliance("Quarterly risk register review for the finance team")
    triggered = [r.regulation_id for r in plain.regulations]
    assert "EU-AI-ACT" not in triggered


def test_obligations_flags_and_signoff(service):
    """Flagged scope produces obligations, flags and requires human sign-off."""
    analysis = service.analyze(SCOPE)
    assert analysis.compliance_status == ComplianceStatus.FLAGGED
    assert analysis.requires_human_signoff is True
    assert len(analysis.obligations) > 0
    assert len(analysis.compliance_flags) == len(analysis.regulations)
    assert 0.0 < analysis.compliance_score <= 1.0
    # highest severity drives the score (EXPORT-CTRL = CRITICAL = 0.9)
    assert analysis.compliance_score == 0.9
    for reg in analysis.regulations:
        assert reg.authority
        assert reg.matched_triggers


def test_cleared_when_no_triggers(service):
    """Scope with no regulatory triggers is CLEARED without sign-off."""
    analysis = service.analyze("Office stationery supply contract renewal")
    assert analysis.compliance_status == ComplianceStatus.CLEARED
    assert analysis.requires_human_signoff is False
    assert analysis.regulations == []
    assert analysis.compliance_score == 0.0


def test_ip_clearance_decisions(service):
    """Clearance score maps to CLEAR / REVIEW / BLOCKED from config thresholds."""
    # far-expiry active patent -> low conflict -> CLEAR (70.0)
    clear = service.clear_ip(["provenance"])
    assert clear.clearance_decision is ClearanceDecision.CLEAR
    assert clear.clearance_score == 70.0
    assert clear.conflicts[0].ip_id == "EP-3456789B1"
    assert clear.conflicts[0].conflict_score == 0.3
    assert clear.conflicts[0].risk_band is Severity.MEDIUM

    # near-expiry active patent, two overlapping topics -> REVIEW (50.0)
    review = service.clear_ip(["cross-border", "sovereignty"])
    assert review.clearance_decision is ClearanceDecision.REVIEW
    assert review.clearance_score == 50.0
    assert review.conflicts[0].ip_id == "EP-2987654"
    assert review.conflicts[0].risk_band is Severity.HIGH

    # single fully-overlapping topic on near-expiry patent -> BLOCKED (0.0)
    blocked = service.clear_ip(["cross-border"])
    assert blocked.clearance_decision is ClearanceDecision.BLOCKED
    assert blocked.clearance_score == 0.0


def test_ip_expiry_and_status_weighting(service):
    """Expired/pending records weigh less than active near-expiry records."""
    ip = service.clear_ip(["audit", "logging"])
    expired = next(c for c in ip.conflicts if c.ip_id == "US-9911223")
    assert expired.status == "EXPIRED"
    assert expired.conflict_score < 0.1
    assert expired.risk_band is Severity.LOW

    # no matching topic -> no conflicts at all
    empty = service.clear_ip(["quantum-blockchain"])
    assert empty.conflicts == []
    assert empty.clearance_score == 100.0
    assert empty.clearance_decision is ClearanceDecision.CLEAR


def test_legal_risk_and_report(service):
    """Blended legal risk + full report with citations and confidence."""
    report = service.build_report(SCOPE, ["provenance"])
    assert report.report_id.startswith("lri_")
    assert report.compliance.compliance_status is ComplianceStatus.FLAGGED
    assert report.ip_analysis.clearance_decision is ClearanceDecision.CLEAR
    assert report.legal_risk.legal_risk_level is Severity.CRITICAL
    assert 0.0 <= report.legal_risk.legal_risk_score <= 1.0
    assert "Human sign-off REQUIRED" in report.executive_summary
    assert 0.0 < report.confidence <= 1.0
    assert "GDPR" in report.citations
    assert "EU-AI-ACT" in report.citations
    assert len(report.legal_risk.drivers) > 0


def test_rest_api_endpoints():
    """Verify Domain 9 REST gateway lifecycle."""
    client = TestClient(app)

    h = client.get("/health")
    assert h.status_code == 200
    assert h.json()["status"] == "HEALTHY"

    assert len(client.get("/regulations").json()) == 8
    assert len(client.get("/ip-landscape").json()) == 6

    r = client.post("/compliance/analyze", json={"scope": SCOPE})
    assert r.status_code == 200
    body = r.json()
    assert body["compliance_status"] == "FLAGGED"
    assert body["requires_human_signoff"] is True
    assert len(body["obligations"]) > 0

    r = client.post("/ip/clearance", json={"topics": ["cross-border"]})
    assert r.status_code == 200
    assert r.json()["clearance_decision"] == "BLOCKED"

    r = client.post("/legal/risk", json={"scope": SCOPE, "topics": ["provenance"]})
    assert r.status_code == 200
    assert r.json()["legal_risk_level"] in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}

    r = client.post("/report", json={"subject": SCOPE, "topics": ["provenance"]})
    assert r.status_code == 200
    assert r.json()["confidence"] > 0


def test_domain6_toolregistry_integration():
    """Domain 6's ToolRegistry delegates check_legal_compliance to Domain 9 with RBAC preserved."""
    from agent_orchestrator.tools import ToolRegistry

    ToolRegistry.clear_audit_trail()

    result = ToolRegistry.check_legal_compliance(SCOPE, caller_agent="LEGAL_REGULATORY")

    # legacy Domain 6 contract keys
    assert result["compliance_status"] == "FLAGGED"
    assert "General Data Protection Regulation" in result["detected_regulations"]
    assert "EU AI Act" in result["detected_regulations"]
    assert len(result["compliance_flags"]) > 0
    assert result["requires_human_signoff"] is True
    # Domain 9 enrichment
    assert "obligations" in result
    assert "compliance_score" in result
    assert result["compliance_score"] == 0.9
    ids = [r["regulation_id"] for r in result["regulations"]]
    assert "GDPR" in ids and "EU-AI-ACT" in ids

    audit = ToolRegistry.get_audit_trail()
    assert audit[0].tool_name == "check_legal_compliance"
    assert audit[0].arguments.get("provider") == "legal"

    # RBAC still enforced before any integration call happens
    with pytest.raises(PermissionError):
        ToolRegistry.check_legal_compliance(SCOPE, caller_agent="MARKET_INTELLIGENCE")
    assert ToolRegistry.get_audit_trail()[-1].status == "PERMISSION_DENIED"



def test_domain6_contract_is_json_safe(service):
    """to_domain6_legal_check must not leak Enum objects into LangGraph checkpoints."""
    payload = service.analyze(SCOPE).to_domain6_legal_check()
    assert payload["regulations"]
    for reg in payload["regulations"]:
        assert isinstance(reg["severity"], str)          # plain string, not Severity enum
        assert not hasattr(reg["severity"], "value")
    import json
    json.dumps(payload)  # must be JSON-serializable for strict checkpointing


def test_api_key_authentication(monkeypatch):
    """When auth is enabled every route except /health requires a valid X-API-Key."""
    from legal_regulatory_ip_intelligence import auth

    monkeypatch.setattr(auth, "configured_api_key", lambda: "s3cret")
    client = TestClient(app)

    assert client.get("/health").status_code == 200
    assert client.get("/regulations").status_code == 401
    assert client.get("/regulations", headers={"X-API-Key": "s3cret"}).status_code == 200

