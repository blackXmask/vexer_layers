"""
Domain 8 Service Facade — single entry point for Opportunity, Risk &
Requirements Intelligence.

Used in-process by Domain 6's ToolRegistry adapter, or remotely through api.py.
No dependency on agent_orchestrator (one-way integration, no cycles).
"""
import uuid
from typing import Dict, List, Optional

from .config import load_config
from .models import (
    IntelligenceReport,
    OpportunityAssessment,
    RequirementItem,
    RiskAnalysis,
)
from .analytics import assess_opportunity, analyze_risks, parse_requirements


class OpportunityRiskService:
    """Stateless facade over matching + risk analytics. Thread-safe."""

    def __init__(self, config_path: Optional[str] = None):
        if config_path:
            load_config(config_path)  # validates early; cached by loader

    def health(self) -> dict:
        cfg = load_config()
        return {
            "status": "HEALTHY",
            "domain": cfg.get("domain"),
            "version": cfg.get("version"),
            "capability_count": len(cfg.get("capabilities", [])),
            "risk_register_size": len(cfg.get("risk_register", []))
        }

    def get_capabilities(self) -> List[Dict]:
        """The configured capability catalog."""
        return load_config().get("capabilities", [])

    def parse_requirements(self, requirements: List[str]) -> List[RequirementItem]:
        return parse_requirements(requirements)

    def assess(self, rfp_title: str, requirements: List[str]) -> OpportunityAssessment:
        return assess_opportunity(rfp_title, requirements)

    def analyze_risks(self, rfp_title: str, requirements: List[str]) -> RiskAnalysis:
        assessment = assess_opportunity(rfp_title, requirements)
        return analyze_risks(
            rfp_title, requirements,
            gap_count=len(assessment.capability_gaps)
        )

    def build_report(self, rfp_title: str, requirements: List[str]) -> IntelligenceReport:
        """Full report: assessment + risk analysis + parsed requirements,
        with executive summary, mitigations, confidence and citations."""
        report_cfg = load_config().get("report", {})
        max_risks = int(report_cfg.get("max_risks_listed", 5))

        assessment = assess_opportunity(rfp_title, requirements)
        risks = analyze_risks(
            rfp_title, requirements,
            gap_count=len(assessment.capability_gaps)
        )
        parsed = parse_requirements(requirements)
        top_risks = risks.active_risks[:max_risks]

        fit_word = {"GO": "GO", "CONDITIONAL": "conditional GO", "NO_GO": "NO-GO"}[
            assessment.detailed_recommendation
        ]
        summary = (
            f"Fit {assessment.fit_score}% on {len(requirements)} requirements "
            f"({len(assessment.matched_capabilities)} matched, "
            f"{len(assessment.capability_gaps)} gaps) -> {fit_word}. "
            f"Opportunity score {assessment.opportunity_score}/100. "
            f"Risk level {risks.overall_risk_level.value} "
            f"({len(risks.active_risks)} active risks)."
        )
        if top_risks:
            summary += f" Top risk: {top_risks[0].name} [{top_risks[0].owner}]."

        base_conf = float(report_cfg.get("base_confidence", 0.9))
        boost = float(report_cfg.get("per_requirement_confidence_boost", 0.01))
        confidence = round(min(base_conf + len(requirements) * boost, 0.99), 3)

        citations = (
            sorted({m.capability for m in assessment.matches})
            + [r.risk_id for r in top_risks]
            + [load_config().get("domain", "")]
        )

        return IntelligenceReport(
            report_id=f"ori_{uuid.uuid4().hex[:8]}",
            rfp_title=rfp_title,
            executive_summary=summary,
            assessment=assessment,
            risk_analysis=risks,
            requirements=parsed,
            confidence=confidence,
            citations=citations
        )
