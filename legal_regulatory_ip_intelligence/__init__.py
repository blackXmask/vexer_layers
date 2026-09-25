"""
Domain 9: Legal, Regulatory & Intellectual Property Intelligence — Package Exports.
"""
from .config import load_config, reload_config
from .models import (
    ClearanceDecision,
    ComplianceAnalysis,
    ComplianceStatus,
    IntelligenceReport,
    IPAnalysis,
    IPConflict,
    LegalRisk,
    RegulationMatch,
    Severity,
)
from .analytics import (
    analyze_compliance,
    analyze_ip,
    analyze_legal_risk,
    derive_topics,
    match_regulations,
)
from .service import LegalIntelligenceService

__all__ = [
    "load_config",
    "reload_config",
    "ClearanceDecision",
    "ComplianceAnalysis",
    "ComplianceStatus",
    "IntelligenceReport",
    "IPAnalysis",
    "IPConflict",
    "LegalRisk",
    "RegulationMatch",
    "Severity",
    "analyze_compliance",
    "analyze_ip",
    "analyze_legal_risk",
    "derive_topics",
    "match_regulations",
    "LegalIntelligenceService",
]
