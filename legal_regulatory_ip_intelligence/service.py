"""
Domain 9 Service Facade — single entry point for Legal, Regulatory & IP Intelligence.

Used in-process by Domain 6's ToolRegistry adapter, or remotely through api.py.
No dependency on agent_orchestrator (one-way integration, no cycles).
"""
import uuid
from typing import List, Optional

from .config import load_config
from .models import (
    ComplianceAnalysis,
    IntelligenceReport,
    IPAnalysis,
    LegalRisk,
)
from .analytics import (
    analyze_compliance,
    analyze_ip,
    analyze_legal_risk,
    derive_topics,
)


class LegalIntelligenceService:
    """Stateless facade over regulatory + IP analytics. Thread-safe."""

    def __init__(self, config_path: Optional[str] = None):
        if config_path:
            load_config(config_path)  # validates early; cached by loader

    def health(self) -> dict:
        cfg = load_config()
        return {
            "status": "HEALTHY",
            "domain": cfg.get("domain"),
            "version": cfg.get("version"),
            "regulation_count": len(cfg.get("regulations", [])),
            "ip_record_count": len(cfg.get("ip_landscape", []))
        }

    def get_regulations(self) -> List[dict]:
        return load_config().get("regulations", [])

    def get_ip_landscape(self) -> List[dict]:
        return load_config().get("ip_landscape", [])

    def analyze(self, scope_text: str) -> ComplianceAnalysis:
        return analyze_compliance(scope_text)

    def clear_ip(self, topics: List[str] = None, subject_text: str = "") -> IPAnalysis:
        resolved = topics if topics is not None else derive_topics(subject_text)
        return analyze_ip(resolved)

    def legal_risk(self, scope_text: str, topics: List[str] = None) -> LegalRisk:
        return analyze_legal_risk(scope_text, topics)

    def build_report(self, subject: str, topics: List[str] = None) -> IntelligenceReport:
        """Full legal intelligence report: compliance + IP clearance + legal risk."""
        report_cfg = load_config().get("report", {})
        max_conflicts = int(report_cfg.get("max_conflicts_listed", 5))

        compliance = analyze_compliance(subject)
        resolved_topics = topics if topics is not None else derive_topics(subject)
        ip = analyze_ip(resolved_topics)
        risk = analyze_legal_risk(subject, resolved_topics)

        summary = (
            f"Compliance {compliance.compliance_status.value} with "
            f"{len(compliance.regulations)} regulations triggered "
            f"({len(compliance.obligations)} obligations). "
            f"IP clearance {ip.clearance_score}/100 -> {ip.clearance_decision.value} "
            f"({len(ip.conflicts)} potential conflicts). "
            f"Legal risk {risk.legal_risk_level.value} (score {risk.legal_risk_score})."
        )
        if compliance.requires_human_signoff:
            summary += " Human sign-off REQUIRED before commitment."

        base_conf = float(report_cfg.get("base_confidence", 0.9))
        citations = (
            [r.regulation_id for r in compliance.regulations]
            + [c.ip_id for c in ip.conflicts[:max_conflicts]]
            + [load_config().get("domain", "")]
        )

        return IntelligenceReport(
            report_id=f"lri_{uuid.uuid4().hex[:8]}",
            subject=subject,
            executive_summary=summary,
            compliance=compliance,
            ip_analysis=ip,
            legal_risk=risk,
            confidence=round(min(base_conf + 0.01 * len(compliance.regulations), 0.99), 3),
            citations=citations
        )
