"""
Domain 8: Opportunity, Risk & Requirements Intelligence — Data Models.
"""
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class Priority(str, Enum):
    MANDATORY = "MANDATORY"
    OPTIONAL = "OPTIONAL"


class SeverityBand(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RequirementItem(BaseModel):
    text: str
    category: str = "GENERAL"
    priority: Priority = Priority.MANDATORY
    matched_capability: Optional[str] = None
    is_match: bool = False


class CapabilityMatch(BaseModel):
    requirement: str
    capability: str
    category: str
    weight: float = 1.0


class OpportunityAssessment(BaseModel):
    rfp_title: str
    fit_score: float = 0.0
    weighted_fit_score: float = 0.0
    recommendation: str = "NO_GO"            # legacy contract: GO | NO_GO
    detailed_recommendation: str = "NO_GO"    # GO | CONDITIONAL | NO_GO
    matched_capabilities: List[str] = Field(default_factory=list)  # legacy: matched req strings
    capability_gaps: List[str] = Field(default_factory=list)       # legacy: unmatched req strings
    matches: List[CapabilityMatch] = Field(default_factory=list)
    category_breakdown: Dict[str, Dict[str, int]] = Field(default_factory=dict)
    opportunity_score: float = 0.0
    active_risk_count: int = 0

    def to_domain6_rfp_fit(self) -> Dict[str, Any]:
        """Maps to the dict shape consumed by Domain 6's ToolRegistry.evaluate_rfp_fit
        (legacy keys preserved; Domain 8 enrichment fields added)."""
        return {
            "rfp_title": self.rfp_title,
            "fit_score": round(self.fit_score, 2),
            "matched_capabilities": self.matched_capabilities,
            "capability_gaps": self.capability_gaps,
            "recommendation": self.recommendation,
            # Domain 8 enrichment
            "detailed_recommendation": self.detailed_recommendation,
            "weighted_fit_score": round(self.weighted_fit_score, 2),
            "opportunity_score": round(self.opportunity_score, 2),
            "category_breakdown": self.category_breakdown
        }


class RiskItem(BaseModel):
    risk_id: str
    name: str
    category: str
    likelihood: float
    impact: float
    severity_score: float
    severity_band: SeverityBand
    active: bool = True
    triggered_by: List[str] = Field(default_factory=list)
    mitigation: str = ""
    owner: str = ""


class RiskAnalysis(BaseModel):
    active_risks: List[RiskItem] = Field(default_factory=list)
    dormant_risk_count: int = 0
    overall_risk_score: float = 0.0
    overall_risk_level: SeverityBand = SeverityBand.LOW
    band_counts: Dict[str, int] = Field(default_factory=dict)


class IntelligenceReport(BaseModel):
    report_id: str
    rfp_title: str
    executive_summary: str
    assessment: OpportunityAssessment
    risk_analysis: RiskAnalysis
    requirements: List[RequirementItem] = Field(default_factory=list)
    confidence: float = 0.0
    citations: List[str] = Field(default_factory=list)
