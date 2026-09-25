"""
Comprehensive Test Suite for Domain 8: Opportunity, Risk & Requirements Intelligence.
Validates:
1. Capability catalog configuration
2. Requirement classification (category + mandatory/optional priority)
3. Fit scoring with GO / CONDITIONAL / NO-GO recommendation paths
4. Risk register triggering (keyword-based)
5. Dynamic capability-gap risk
6. Severity bands and overall risk level
7. Full intelligence report generation
8. REST API endpoints
9. Domain 6 ToolRegistry integration (adapter + RBAC preserved)
"""
import pytest
from fastapi.testclient import TestClient

from opportunity_risk_intelligence import (
    OpportunityRiskService,
    SeverityBand,
    analyze_risks,
)
from opportunity_risk_intelligence.api import app

RFP_TITLE = "EU Defense AI tender with cross-border transfer"
GOOD_REQS = [
    "Data sovereignty",
    "Cloud security",
    "Audit trail",
    "Cross-border transfer mechanism",
    "Budget ceiling",
]


@pytest.fixture()
def service():
    return OpportunityRiskService()


def test_capability_catalog_configured(service):
    """Catalog has 8 capabilities with categories, weights and keywords."""
    caps = service.get_capabilities()
    assert len(caps) >= 8
    for cap in caps:
        assert cap["name"]
        assert cap["category"] in {"TECHNICAL", "LEGAL", "FINANCIAL", "OPERATIONAL", "GOVERNANCE"}
        assert cap["weight"] > 0
        assert len(cap["keywords"]) > 0
    health = service.health()
    assert health["status"] == "HEALTHY"
    assert health["capability_count"] == len(caps)


def test_requirement_classification(service):
    """Requirements get category + priority from config keywords."""
    parsed = service.parse_requirements([
        "Must provide audit logging for all agent decisions",
        "Should support dark mode in the portal",
        "Fixed budget ceiling of 5M EUR",
    ])
    assert parsed[0].category == "GOVERNANCE"
    assert parsed[0].priority.value == "MANDATORY"
    assert parsed[1].priority.value == "OPTIONAL"
    assert parsed[2].category == "FINANCIAL"
    # capability matching fills is_match
    assert parsed[0].is_match is True          # audit -> audit trail capability
    assert parsed[2].is_match is True          # budget -> budget alignment
    assert parsed[1].matched_capability is None


def test_fit_scoring_go_path(service):
    """All requirements matched -> fit 100, GO recommendation."""
    a = service.assess(RFP_TITLE, GOOD_REQS)
    assert a.fit_score == 100.0
    assert a.recommendation == "GO"
    assert a.detailed_recommendation == "GO"
    assert a.matched_capabilities == GOOD_REQS
    assert a.capability_gaps == []
    assert len(a.matches) == 5
    assert 0.0 <= a.opportunity_score <= 100.0
    # category breakdown covers all requirements
    total = sum(v["total"] for v in a.category_breakdown.values())
    assert total == len(GOOD_REQS)


def test_fit_scoring_gap_and_no_go(service):
    """Unmatched requirements produce gaps and NO-GO."""
    a = service.assess("Mystery RFP", ["Quantum blockchain synergy"])
    assert a.fit_score == 0.0
    assert a.recommendation == "NO_GO"
    assert a.detailed_recommendation == "NO_GO"
    assert a.capability_gaps == ["Quantum blockchain synergy"]
    assert a.matches == []


def test_risk_triggering_by_keywords(service):
    """Cross-border + security keywords activate register risks with mitigations."""
    analysis = service.analyze_risks(RFP_TITLE, GOOD_REQS)
    active_ids = [r.risk_id for r in analysis.active_risks]
    assert "R-XBORDER" in active_ids   # transfer/cross-border in title/reqs
    assert "R-CYBER" in active_ids     # 'Cloud security' req
    for risk in analysis.active_risks:
        assert risk.severity_score == round(risk.likelihood * risk.impact, 3)
        assert 0.0 <= risk.severity_score <= 1.0
        assert risk.mitigation
        assert risk.owner
        assert risk.triggered_by
    # dormant risks counted (e.g. R-DELIVERY has no timeline trigger here)
    assert analysis.dormant_risk_count >= 1


def test_dynamic_capability_gap_risk():
    """Gaps activate R-CAPGAP with likelihood scaled by gap count."""
    no_gaps = analyze_risks(RFP_TITLE, GOOD_REQS, gap_count=0)
    assert "R-CAPGAP" not in [r.risk_id for r in no_gaps.active_risks]

    with_gaps = analyze_risks("RFP", ["Unknown requirement A", "Unknown requirement B"], gap_count=2)
    capgap = next(r for r in with_gaps.active_risks if r.risk_id == "R-CAPGAP")
    # base 0.1 + 0.15 * 2 gaps = 0.4 (word-boundary matching never matches 'ai' in 'risk')
    assert capgap.likelihood == pytest.approx(0.4, abs=1e-6)
    assert capgap.triggered_by == ["2 capability gap(s)"]


def test_severity_bands_and_overall_level():
    """Band thresholds come from config; overall = highest active severity."""
    analysis = analyze_risks(
        "EU Defense AI tender with cross-border transfer",
        ["Cloud security", "Data sovereignty", "Cross-border transfer mechanism"],
        gap_count=0,
    )
    bands = {"LOW": 0.25, "MEDIUM": 0.45, "HIGH": 0.65}
    for risk in analysis.active_risks:
        s = risk.severity_score
        expected = ("CRITICAL" if s >= bands["HIGH"]
                    else "HIGH" if s >= bands["MEDIUM"]
                    else "MEDIUM" if s >= bands["LOW"]
                    else "LOW")
        assert risk.severity_band.value == expected
    assert analysis.overall_risk_score == max(r.severity_score for r in analysis.active_risks)
    assert isinstance(analysis.overall_risk_level, SeverityBand)
    # empty input -> no active risks, LOW
    empty = analyze_risks("plain title", [])
    assert empty.active_risks == []
    assert empty.overall_risk_level == SeverityBand.LOW


def test_intelligence_report_generation(service):
    """Report carries summary, assessment, risks, confidence, citations."""
    report = service.build_report(RFP_TITLE, GOOD_REQS)
    assert report.report_id.startswith("ori_")
    assert "Fit 100.0%" in report.executive_summary
    assert report.assessment.fit_score == 100.0
    assert len(report.risk_analysis.active_risks) > 0
    assert len(report.requirements) == len(GOOD_REQS)
    assert 0.0 < report.confidence <= 1.0
    assert len(report.citations) > 0
    # mitigations present for every active risk in report
    for risk in report.risk_analysis.active_risks:
        assert risk.mitigation


def test_rest_api_endpoints():
    """Verify Domain 8 REST gateway lifecycle."""
    client = TestClient(app)

    h = client.get("/health")
    assert h.status_code == 200
    assert h.json()["status"] == "HEALTHY"

    r = client.get("/capabilities")
    assert r.status_code == 200 and len(r.json()) >= 8

    r = client.post("/requirements/parse", json={"requirements": ["Must provide audit logging"]})
    assert r.status_code == 200
    assert r.json()[0]["category"] == "GOVERNANCE"

    r = client.post("/opportunity/assess", json={"rfp_title": RFP_TITLE, "requirements": GOOD_REQS})
    assert r.status_code == 200
    assert r.json()["fit_score"] == 100.0
    assert r.json()["recommendation"] == "GO"

    r = client.post("/risks/analyze", json={"rfp_title": RFP_TITLE, "requirements": GOOD_REQS})
    assert r.status_code == 200
    assert len(r.json()["active_risks"]) > 0

    r = client.post("/report", json={"rfp_title": RFP_TITLE, "requirements": GOOD_REQS})
    assert r.status_code == 200
    report = r.json()
    assert report["confidence"] > 0
    assert report["assessment"]["opportunity_score"] > 0


def test_domain6_toolregistry_integration():
    """Domain 6's ToolRegistry delegates evaluate_rfp_fit to Domain 8 with RBAC preserved."""
    from agent_orchestrator.tools import ToolRegistry

    ToolRegistry.clear_audit_trail()

    # Authorized call -> delegated to Domain 8
    result = ToolRegistry.evaluate_rfp_fit(RFP_TITLE, GOOD_REQS, caller_agent="OPPORTUNITY_RISK")

    # legacy Domain 6 contract keys
    assert result["rfp_title"] == RFP_TITLE
    assert result["fit_score"] == 100.0
    assert result["matched_capabilities"] == GOOD_REQS
    assert result["capability_gaps"] == []
    assert result["recommendation"] == "GO"
    # Domain 8 enrichment
    assert "opportunity_score" in result
    assert "detailed_recommendation" in result

    audit = ToolRegistry.get_audit_trail()
    assert audit[0].tool_name == "evaluate_rfp_fit"
    assert audit[0].arguments.get("provider") == "opportunity"

    # RBAC still enforced before any integration call happens
    with pytest.raises(PermissionError):
        ToolRegistry.evaluate_rfp_fit("x", ["y"], caller_agent="MARKET_INTELLIGENCE")
    assert ToolRegistry.get_audit_trail()[-1].status == "PERMISSION_DENIED"



def test_api_key_authentication(monkeypatch):
    """When auth is enabled every route except /health requires a valid X-API-Key."""
    from opportunity_risk_intelligence import auth

    monkeypatch.setattr(auth, "configured_api_key", lambda: "s3cret")
    client = TestClient(app)

    assert client.get("/health").status_code == 200
    assert client.get("/capabilities").status_code == 401
    assert client.get("/capabilities", headers={"X-API-Key": "s3cret"}).status_code == 200


def test_input_validation_limits():
    """Requirement payloads are bounded."""
    client = TestClient(app)
    assert client.post("/report", json={"requirements": ["x" * 900]}).status_code == 422
    assert client.post("/report", json={
        "rfp_title": "t", "requirements": ["Data sovereignty"] * 150
    }).status_code == 422

