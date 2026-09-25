"""
Domain 8 Analytics Engine.

Deterministic, explainable scoring:
- Word-boundary keyword matching (no substring false positives, e.g. 'ai' vs 'risk')
- Requirement classification (category + mandatory/optional priority)
- Capability catalog fitting (unweighted fit + capability-weighted fit)
- Risk register triggering (keyword triggers + dynamic capability-gap risk)
- Severity bands (LOW / MEDIUM / HIGH / CRITICAL) and opportunity scoring
All thresholds, weights and registers come from config.json.
"""
import re
from typing import Dict, List, Optional

from .config import load_config
from .models import (
    CapabilityMatch,
    OpportunityAssessment,
    Priority,
    RequirementItem,
    RiskAnalysis,
    RiskItem,
    SeverityBand,
)

_BUILTIN_FALLBACK_CATEGORY = "GENERAL"


def _hit(text: str, keyword: str) -> bool:
    """Word-boundary match: 'ai' matches 'AI tender' but not 'risk'."""
    pattern = r"\b" + re.escape(keyword.lower()) + r"\b"
    return re.search(pattern, text.lower()) is not None


def _any_hit(text: str, keywords: List[str]) -> bool:
    return any(_hit(text, k) for k in keywords)


def classify_requirement(text: str) -> RequirementItem:
    """Classifies a requirement into category + priority using config keywords."""
    cfg = load_config().get("requirements", {})
    category = _BUILTIN_FALLBACK_CATEGORY
    for cat, keywords in cfg.get("category_keywords", {}).items():
        if _any_hit(text, keywords):
            category = cat
            break
    optional = _any_hit(text, cfg.get("optional_keywords", []))
    priority = Priority.OPTIONAL if optional else Priority(
        cfg.get("default_priority", "MANDATORY")
    )
    return RequirementItem(text=text, category=category, priority=priority)


def match_capability(text: str) -> Optional[CapabilityMatch]:
    """Returns the first capability from the catalog matching this text, if any."""
    for cap in load_config().get("capabilities", []):
        if _any_hit(text, cap.get("keywords", [])):
            return CapabilityMatch(
                requirement=text,
                capability=cap.get("name", ""),
                category=cap.get("category", _BUILTIN_FALLBACK_CATEGORY),
                weight=float(cap.get("weight", 1.0))
            )
    return None


def parse_requirements(requirements: List[str]) -> List[RequirementItem]:
    """Classifies and capability-matches each requirement."""
    items = []
    for text in requirements:
        item = classify_requirement(text)
        match = match_capability(text)
        if match:
            item.matched_capability = match.capability
            item.is_match = True
        items.append(item)
    return items


def assess_opportunity(rfp_title: str, requirements: List[str]) -> OpportunityAssessment:
    """Fit scoring + recommendation + category breakdown + opportunity score."""
    scoring = load_config().get("scoring", {})
    go_threshold = float(scoring.get("go_threshold", 70.0))
    cond_threshold = float(scoring.get("conditional_threshold", 50.0))

    parsed = parse_requirements(requirements)
    matches = [m for m in (match_capability(r) for r in requirements) if m]
    matched_reqs = [m.requirement for m in matches]
    gaps = [r for r in requirements if r not in matched_reqs]

    total = max(len(requirements), 1)
    fit_score = (len(matched_reqs) / total) * 100.0

    total_weight = sum(m.weight for m in matches) + sum(
        1.0 for r in requirements if r not in matched_reqs
    )
    matched_weight = sum(m.weight for m in matches)
    weighted_fit = (matched_weight / max(total_weight, 1e-9)) * 100.0

    if fit_score >= go_threshold:
        recommendation, detailed = "GO", "GO"
    elif fit_score >= cond_threshold:
        recommendation, detailed = "NO_GO", "CONDITIONAL"
    else:
        recommendation, detailed = "NO_GO", "NO_GO"

    breakdown: Dict[str, Dict[str, int]] = {}
    for item in parsed:
        entry = breakdown.setdefault(item.category, {"total": 0, "matched": 0})
        entry["total"] += 1
        if item.is_match:
            entry["matched"] += 1

    # risk-aware opportunity score (blended by config weights)
    analysis = analyze_risks(rfp_title, requirements, gap_count=len(gaps))
    fit_weight = float(scoring.get("opportunity_fit_weight", 0.7))
    risk_weight = float(scoring.get("opportunity_risk_weight", 0.3))
    opportunity_score = (
        fit_weight * fit_score
        + risk_weight * (100.0 * (1.0 - analysis.overall_risk_score))
    )

    return OpportunityAssessment(
        rfp_title=rfp_title,
        fit_score=round(fit_score, 2),
        weighted_fit_score=round(weighted_fit, 2),
        recommendation=recommendation,
        detailed_recommendation=detailed,
        matched_capabilities=matched_reqs,
        capability_gaps=gaps,
        matches=matches,
        category_breakdown=breakdown,
        opportunity_score=round(opportunity_score, 2),
        active_risk_count=len(analysis.active_risks)
    )


def _band_for(score: float) -> SeverityBand:
    bands = load_config().get("scoring", {}).get("risk_bands", {})
    low = float(bands.get("LOW", 0.25))
    medium = float(bands.get("MEDIUM", 0.45))
    high = float(bands.get("HIGH", 0.65))
    if score >= high:
        return SeverityBand.CRITICAL
    if score >= medium:
        return SeverityBand.HIGH
    if score >= low:
        return SeverityBand.MEDIUM
    return SeverityBand.LOW



def analyze_risks(rfp_title: str, requirements: List[str], gap_count: int = 0) -> RiskAnalysis:
    """Triggers register risks by keyword; dynamic gap risk scales with gap count."""
    scoring = load_config().get("scoring", {})
    corpus = " ".join([rfp_title] + list(requirements))

    active: List[RiskItem] = []
    dormant = 0
    for risk in load_config().get("risk_register", []):
        if risk.get("dynamic_gaps"):
            if gap_count <= 0:
                dormant += 1
                continue
            likelihood = min(
                float(risk.get("base_likelihood", 0.1))
                + float(scoring.get("gap_likelihood_increment", 0.15)) * gap_count,
                0.95
            )
            impact = float(risk.get("impact", scoring.get("gap_impact", 0.7)))
            triggered = [f"{gap_count} capability gap(s)"]
        else:
            hits = [t for t in risk.get("triggers", []) if _hit(corpus, t)]
            if not hits:
                dormant += 1
                continue
            likelihood = float(risk.get("likelihood", 0.3))
            impact = float(risk.get("impact", 0.5))
            triggered = hits

        severity = round(likelihood * impact, 3)
        active.append(RiskItem(
            risk_id=risk.get("risk_id", ""),
            name=risk.get("name", ""),
            category=risk.get("category", "GENERAL"),
            likelihood=round(likelihood, 3),
            impact=round(impact, 3),
            severity_score=severity,
            severity_band=_band_for(severity),
            active=True,
            triggered_by=triggered,
            mitigation=risk.get("mitigation", ""),
            owner=risk.get("owner", "")
        ))

    active.sort(key=lambda r: r.severity_score, reverse=True)
    band_counts: Dict[str, int] = {}
    for r in active:
        band_counts[r.severity_band.value] = band_counts.get(r.severity_band.value, 0) + 1

    overall_score = active[0].severity_score if active else 0.0
    return RiskAnalysis(
        active_risks=active,
        dormant_risk_count=dormant,
        overall_risk_score=overall_score,
        overall_risk_level=_band_for(overall_score),
        band_counts=band_counts
    )

