"""
Domain 9: Legal, Regulatory & Intellectual Property Intelligence — Data Models.
"""
from enum import Enum
from typing import Any, Dict, List
from pydantic import BaseModel, Field


class ComplianceStatus(str, Enum):
    CLEARED = "CLEARED"
    FLAGGED = "FLAGGED"


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ClearanceDecision(str, Enum):
    CLEAR = "CLEAR"
    REVIEW = "REVIEW"
    BLOCKED = "BLOCKED"


class RegulationMatch(BaseModel):
    regulation_id: str
    name: str
    jurisdiction: str
    severity: Severity
    authority: str
    matched_triggers: List[str] = Field(default_factory=list)
    obligations: List[str] = Field(default_factory=list)
    penalty_note: str = ""


class ComplianceAnalysis(BaseModel):
    scope: str
    compliance_status: ComplianceStatus = ComplianceStatus.CLEARED
    requires_human_signoff: bool = False
    compliance_score: float = 0.0
    regulations: List[RegulationMatch] = Field(default_factory=list)
    obligations: List[str] = Field(default_factory=list)
    compliance_flags: List[str] = Field(default_factory=list)

    def to_domain6_legal_check(self) -> Dict[str, Any]:
        """Maps to the dict shape consumed by Domain 6's ToolRegistry.check_legal_compliance
        (legacy keys preserved; Domain 9 enrichment fields added)."""
        return {
            "compliance_status": self.compliance_status.value,
            "detected_regulations": [r.name for r in self.regulations],
            "compliance_flags": list(self.compliance_flags),
            "requires_human_signoff": self.requires_human_signoff,
            # Domain 9 enrichment
            "compliance_score": round(self.compliance_score, 3),
            "obligations": list(self.obligations),
            # mode="json" keeps enums (Severity) as plain strings so LangGraph
            # checkpoints stay serializable under strict msgpack mode.
            "regulations": [r.model_dump(mode="json") for r in self.regulations]
        }


class IPConflict(BaseModel):
    ip_id: str
    ip_type: str
    title: str
    owner: str
    status: str
    territory: str = ""
    expires: str = ""
    overlap_score: float = 0.0
    conflict_score: float = 0.0
    risk_band: Severity = Severity.LOW
    note: str = ""


class IPAnalysis(BaseModel):
    topics: List[str] = Field(default_factory=list)
    conflicts: List[IPConflict] = Field(default_factory=list)
    clearance_score: float = 100.0
    clearance_decision: ClearanceDecision = ClearanceDecision.CLEAR


class LegalRisk(BaseModel):
    compliance_score: float = 0.0
    ip_conflict_score: float = 0.0
    legal_risk_score: float = 0.0
    legal_risk_level: Severity = Severity.LOW
    drivers: List[str] = Field(default_factory=list)


class IntelligenceReport(BaseModel):
    report_id: str
    subject: str
    executive_summary: str
    compliance: ComplianceAnalysis
    ip_analysis: IPAnalysis
    legal_risk: LegalRisk
    confidence: float = 0.0
    citations: List[str] = Field(default_factory=list)
