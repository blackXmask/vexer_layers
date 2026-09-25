"""
Domain 9 Analytics Engine.

Deterministic, explainable legal / regulatory / IP intelligence:
- Word-boundary regulation matching ('ai' matches 'AI system', never 'risk')
- Obligation extraction + compliance flags + sign-off policy
- IP clearance: topic overlap x status weight x expiry proximity
- Clearance decisions (CLEAR / REVIEW / BLOCKED)
- Blended legal risk score (compliance + IP conflict)
All regulations, IP records, weights and thresholds come from config.json.
"""
import re
from datetime import date
from typing import List

from .config import load_config
from .models import (
    ClearanceDecision,
    ComplianceAnalysis,
    ComplianceStatus,
    IPAnalysis,
    IPConflict,
    LegalRisk,
    RegulationMatch,
    Severity,
)


def _hit(text: str, keyword: str) -> bool:
    """Word-boundary match so short keywords cannot false-positive."""
    pattern = r"\b" + re.escape(keyword.lower()) + r"\b"
    return re.search(pattern, text.lower()) is not None


def _any_hit(text: str, keywords: List[str]) -> bool:
    return any(_hit(text, k) for k in keywords)


def _severity_score(severity: str) -> float:
    scores = load_config().get("scoring", {}).get("severity_scores", {})
    return float(scores.get(severity, 0.25))


def match_regulations(scope_text: str) -> List[RegulationMatch]:
    """Returns every regulation whose trigger keywords appear in the scope."""
    matches: List[RegulationMatch] = []
    for reg in load_config().get("regulations", []):
        hits = [t for t in reg.get("triggers", []) if _hit(scope_text, t)]
        if not hits:
            continue
        matches.append(RegulationMatch(
            regulation_id=reg.get("id", ""),
            name=reg.get("name", ""),
            jurisdiction=reg.get("jurisdiction", ""),
            severity=Severity(reg.get("severity", "LOW")),
            authority=reg.get("authority", ""),
            matched_triggers=hits,
            obligations=list(reg.get("obligations", [])),
            penalty_note=reg.get("penalty_note", "")
        ))
    matches.sort(key=lambda r: _severity_score(r.severity.value), reverse=True)
    return matches


def analyze_compliance(scope_text: str) -> ComplianceAnalysis:
    """Regulatory detection, obligations, flags and sign-off requirement."""
    scoring = load_config().get("scoring", {})
    signoff_severities = set(scoring.get("signoff_severities", ["HIGH", "CRITICAL"]))

    matches = match_regulations(scope_text)
    obligations: List[str] = []
    flags: List[str] = []
    for m in matches:
        obligations.extend(m.obligations)
        flags.append(f"{m.name} ({m.severity.value}): {m.obligations[0]}" if m.obligations
                     else f"{m.name} ({m.severity.value}) detected")

    compliance_score = max((_severity_score(m.severity.value) for m in matches), default=0.0)
    requires_signoff = any(m.severity.value in signoff_severities for m in matches)

    return ComplianceAnalysis(
        scope=scope_text,
        compliance_status=ComplianceStatus.FLAGGED if matches else ComplianceStatus.CLEARED,
        requires_human_signoff=requires_signoff,
        compliance_score=round(compliance_score, 3),
        regulations=matches,
        obligations=obligations,
        compliance_flags=flags
    )


def _months_until(expires: str, evaluation_date: date) -> int:
    """Whole months from evaluation_date to the expiry date (negative = past)."""
    try:
        year, month, day = (int(p) for p in expires.split("-"))
    except (ValueError, AttributeError):
        return 0
    return (year - evaluation_date.year) * 12 + (month - evaluation_date.month)


def _expiry_factor(expires: str, evaluation_date: date, warning_months: int, far_factor: float) -> float:
    months = _months_until(expires, evaluation_date)
    if 0 <= months <= warning_months:
        return 1.0
    return far_factor


def _topic_overlap(topic: str, record: dict) -> float:
    haystack = " ".join(
        [record.get("title", ""), record.get("note", "")]
        + list(record.get("claims_topics", []))
    ).lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", topic.lower()) if len(t) > 2]
    if not tokens:
        return 0.0
    return round(sum(1 for t in tokens if t in haystack) / len(tokens), 3)


def analyze_ip(topics: List[str]) -> IPAnalysis:
    """Frees each topic against the IP landscape and scores the clearance."""
    cfg = load_config().get("ip_clearance", {})
    try:
        evaluation_date = date.fromisoformat(str(cfg.get("evaluation_date", "2026-01-01")))
    except ValueError:
        evaluation_date = date(2026, 1, 1)
    warning_months = int(cfg.get("expiry_warning_months", 24))
    far_factor = float(cfg.get("far_expiry_factor", 0.3))
    status_weights = cfg.get("status_weights", {})

    conflicts: List[IPConflict] = []
    for record in load_config().get("ip_landscape", []):
        overlap = max((_topic_overlap(t, record) for t in topics), default=0.0)
        if overlap <= 0:
            continue
        status_weight = float(status_weights.get(record.get("status", ""), 0.3))
        factor = _expiry_factor(record.get("expires", ""), evaluation_date, warning_months, far_factor)
        conflict_score = round(overlap * status_weight * factor, 3)
        if conflict_score <= 0:
            continue
        if conflict_score >= 0.6:
            band = Severity.HIGH
        elif conflict_score >= 0.3:
            band = Severity.MEDIUM
        else:
            band = Severity.LOW
        conflicts.append(IPConflict(
            ip_id=record.get("ip_id", ""),
            ip_type=record.get("type", ""),
            title=record.get("title", ""),
            owner=record.get("owner", ""),
            status=record.get("status", ""),
            territory=record.get("territory", ""),
            expires=record.get("expires", ""),
            overlap_score=overlap,
            conflict_score=conflict_score,
            risk_band=band,
            note=record.get("note", "")
        ))

    conflicts.sort(key=lambda c: c.conflict_score, reverse=True)
    total = sum(c.conflict_score for c in conflicts)
    clearance = round(100.0 * (1.0 - min(1.0, total / max(len(topics), 1))), 2)

    decisions = cfg.get("decisions", {})
    clear_at = float(decisions.get("CLEAR", 70.0))
    review_at = float(decisions.get("REVIEW", 40.0))
    if clearance >= clear_at:
        decision = ClearanceDecision.CLEAR
    elif clearance >= review_at:
        decision = ClearanceDecision.REVIEW
    else:
        decision = ClearanceDecision.BLOCKED

    return IPAnalysis(
        topics=list(topics),
        conflicts=conflicts,
        clearance_score=clearance,
        clearance_decision=decision
    )


_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "must", "shall", "will",
    "have", "has", "are", "was", "were", "our", "its", "system", "systems", "based",
    "using", "into", "over", "than", "such", "also", "data", "agent", "platform"
}


def derive_topics(text: str, limit: int = 5) -> List[str]:
    """Extracts candidate IP topics from free text (explicit topics are preferred)."""
    tokens = re.findall(r"[a-z][a-z\-]{3,}", text.lower())
    seen: List[str] = []
    for token in tokens:
        if token in _STOPWORDS or token in seen:
            continue
        seen.append(token)
        if len(seen) >= limit:
            break
    return seen


def analyze_legal_risk(scope_text: str, topics: List[str] = None) -> LegalRisk:
    """Blends compliance severity with IP conflict exposure into legal risk."""
    cfg = load_config().get("scoring", {}).get("legal_risk_weights", {})
    compliance_weight = float(cfg.get("compliance", 0.6))
    ip_weight = float(cfg.get("ip_conflict", 0.4))

    compliance = analyze_compliance(scope_text)
    ip = analyze_ip(topics if topics is not None else derive_topics(scope_text))
    ip_conflict_score = round((100.0 - ip.clearance_score) / 100.0, 3)
    legal_risk = round(compliance_weight * compliance.compliance_score
                       + ip_weight * ip_conflict_score, 3)

    drivers: List[str] = []
    if compliance.regulations:
        top = compliance.regulations[0]
        drivers.append(f"{top.name} ({top.severity.value}) - {top.matched_triggers}")
    if ip.conflicts:
        drivers.append(f"IP conflict: {ip.conflicts[0].ip_id} held by {ip.conflicts[0].owner}")

    if legal_risk >= 0.65:
        level = Severity.CRITICAL
    elif legal_risk >= 0.5:
        level = Severity.HIGH
    elif legal_risk >= 0.3:
        level = Severity.MEDIUM
    else:
        level = Severity.LOW

    return LegalRisk(
        compliance_score=compliance.compliance_score,
        ip_conflict_score=ip_conflict_score,
        legal_risk_score=legal_risk,
        legal_risk_level=level,
        drivers=drivers
    )

