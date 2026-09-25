"""
Confidence methodology ``conf-v1`` (mandate §13 — confidence must never be arbitrary).

**Why this exists.** The previous implementation hardcoded confidence values
(``0.91``/``0.94``/``0.96``) in config. Those numbers looked intelligent but meant nothing: no
evidence, no method, no reproducibility. This module replaces them with a documented computation
whose inputs are observable, whose weights are configurable, and whose output is explainable.

FORMULA (``conf-v1``)
    base    = SUM(normalised_weight_i * component_i)   over the *active* components
    penalty = min(contradiction_penalty * contradiction_ratio, penalty_cap)
    value   = clamp(base - penalty, 0.0, 1.0)          when at least one source exists
            = 0.0                                      when there is no evidence at all

Components (each in ``[0, 1]``)
* ``evidence_quality``  mean per-item quality weighted by source reliability
* ``source_agreement``  support/(support+contradict) scaled by *independence*
  (``0.6 + 0.4 * min(independent_sources, 3)/3``) — one source cannot corroborate itself
* ``freshness``         ``exp(-ln2 * age_days / half_life_days)`` from ``published_at`` (falling back
  to ``observed_at``); a neutral ``0.5`` prior is used when age is unknown and is flagged in the
  rationale rather than silently assumed fresh
* ``model_confidence``  the model's self-reported confidence; when absent its weight is re-normalised
  across the remaining components instead of being counted as zero

**Truth is not confidence (§13).** ``truth_status`` derives from evidence *agreement*, never from the
score: no evidence ⇒ ``UNKNOWN``; nobody contradicting ⇒ ``SUPPORTED``; a minority contradicting ⇒
``CONTESTED``; a majority contradicting ⇒ ``UNSUPPORTED``.
"""
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

from .config import (
    DEFAULT_CONFIDENCE_WEIGHTS,
    DEFAULT_CONTRADICTION_PENALTY,
    DEFAULT_FRESHNESS_HALF_LIFE_DAYS,
)
from .contracts import TruthStatus
from .errors import ErrorCode, VexerError

__all__ = [
    "CONFIDENCE_METHODOLOGY_VERSION",
    "ConfidenceScore",
    "EvidenceObservation",
    "Stance",
    "freshness_factor",
    "score_confidence",
]

CONFIDENCE_METHODOLOGY_VERSION = "conf-v1"
_UNKNOWN_AGE_PRIOR = 0.5
_DEFAULT_PENALTY_CAP = 0.6


class Stance(str, Enum):
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    NEUTRAL = "NEUTRAL"


def _aware(value: Optional[datetime], field_name: str) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise VexerError(ErrorCode.VALIDATION_FAILED, f"{field_name} must be timezone-aware (UTC)")
    return value.astimezone(timezone.utc)


def _unit(value: float, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise VexerError(
            ErrorCode.VALIDATION_FAILED,
            f"{field_name} must be numeric, got {value!r}",
            cause=exc,
        ) from exc
    if not 0.0 <= number <= 1.0:
        raise VexerError(
            ErrorCode.VALIDATION_FAILED, f"{field_name} must be within [0, 1], got {number!r}"
        )
    return number


@dataclass(frozen=True)
class EvidenceObservation:
    """One piece of evidence as seen by the scorer (no I/O, no lookups — pure input)."""

    source_id: str
    stance: Stance = Stance.SUPPORTS
    reliability: float = 0.5
    quality: Optional[float] = None
    published_at: Optional[datetime] = None
    observed_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise VexerError(ErrorCode.VALIDATION_FAILED, "source_id must be non-empty")
        object.__setattr__(self, "reliability", _unit(self.reliability, "reliability"))
        if self.quality is not None:
            object.__setattr__(self, "quality", _unit(self.quality, "quality"))
        object.__setattr__(self, "published_at", _aware(self.published_at, "published_at"))
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "observed_at"))
        if not isinstance(self.stance, Stance):
            object.__setattr__(self, "stance", Stance(str(self.stance)))

    @property
    def effective_quality(self) -> float:
        """Quality defaults to the source reliability prior (documented, not invented)."""
        return self.quality if self.quality is not None else self.reliability

    @property
    def reference_time(self) -> Optional[datetime]:
        return self.published_at or self.observed_at


@dataclass(frozen=True)
class ConfidenceScore:
    """Explainable score: value + components + agreement + rationale (JSON-safe via to_dict)."""

    value: float
    components: Dict[str, float] = field(default_factory=dict)
    truth_status: TruthStatus = TruthStatus.UNKNOWN
    evidence_count: int = 0
    independent_sources: int = 0
    contradiction_ratio: float = 0.0
    methodology_version: str = CONFIDENCE_METHODOLOGY_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "value": round(self.value, 4),
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "truth_status": self.truth_status.value,
            "evidence_count": self.evidence_count,
            "independent_sources": self.independent_sources,
            "contradiction_ratio": round(self.contradiction_ratio, 4),
            "methodology_version": self.methodology_version,
        }

    def rationale(self) -> str:
        parts = ", ".join(f"{k}={v:.3f}" for k, v in sorted(self.components.items()))
        return (
            f"{self.methodology_version}: {self.evidence_count} evidence item(s), "
            f"{self.independent_sources} independent source(s), {parts}; "
            f"contradiction_ratio={self.contradiction_ratio:.3f} -> {self.value:.3f} "
            f"({self.truth_status.value})"
        )


def freshness_factor(
    age_days: float, half_life_days: float = DEFAULT_FRESHNESS_HALF_LIFE_DAYS
) -> float:
    """Exponential freshness decay: 1.0 = fresh, 0.5 = one half-life old."""
    if half_life_days <= 0:
        raise VexerError(ErrorCode.VALIDATION_FAILED, "half_life_days must be positive")
    if age_days < 0:
        age_days = 0.0  # future-dated evidence counts as fresh, never as negative age
    return float(math.exp(-math.log(2) * (age_days / half_life_days)))


def score_confidence(
    evidence: Sequence[EvidenceObservation],
    *,
    now: Optional[datetime] = None,
    model_confidence: Optional[float] = None,
    weights: Optional[Dict[str, float]] = None,
    contradiction_penalty: float = DEFAULT_CONTRADICTION_PENALTY,
    half_life_days: float = DEFAULT_FRESHNESS_HALF_LIFE_DAYS,
    penalty_cap: float = _DEFAULT_PENALTY_CAP,
) -> ConfidenceScore:
    """Compute ``conf-v1`` confidence from evidence observations.

    Pure function: no I/O, no hidden clock reads (``now`` is injectable for deterministic tests).
    Returns ``0.0`` / ``UNKNOWN`` when there is no evidence — the platform never fabricates
    confidence for an unsupported statement.
    """
    moment = _aware(now, "now") or datetime.now(timezone.utc)
    items: List[EvidenceObservation] = list(evidence)
    if not items:
        return ConfidenceScore(
            value=0.0,
            components={},
            truth_status=TruthStatus.UNKNOWN,
            evidence_count=0,
            independent_sources=0,
            contradiction_ratio=0.0,
        )

    supporting = [i for i in items if i.stance is Stance.SUPPORTS]
    contradicting = [i for i in items if i.stance is Stance.CONTRADICTS]
    decisive = len(supporting) + len(contradicting)

    quality = sum(i.effective_quality * i.reliability for i in items) / len(items)
    agreement_raw = (len(supporting) / decisive) if decisive else 0.0
    sources = {i.source_id for i in items}
    independence = 0.6 + 0.4 * (min(len(sources), 3) / 3.0)
    agreement = agreement_raw * independence

    ages = [
        max((moment - i.reference_time).total_seconds() / 86400.0, 0.0)
        for i in items
        if i.reference_time is not None
    ]
    freshness = (
        sum(freshness_factor(age, half_life_days) for age in ages) / len(ages)
        if ages
        else _UNKNOWN_AGE_PRIOR
    )

    components: Dict[str, float] = {
        "evidence_quality": max(min(quality, 1.0), 0.0),
        "source_agreement": max(min(agreement, 1.0), 0.0),
        "freshness": max(min(freshness, 1.0), 0.0),
    }
    if model_confidence is not None:
        components["model_confidence"] = _unit(model_confidence, "model_confidence")

    configured = weights or DEFAULT_CONFIDENCE_WEIGHTS
    active_weights = {key: max(float(configured.get(key, 0.0)), 0.0) for key in components}
    weight_total = sum(active_weights.values())
    if weight_total <= 0:
        raise VexerError(
            ErrorCode.VALIDATION_FAILED,
            "no active confidence weights: provide weights covering the supplied components",
            details={"components": sorted(components)},
        )
    base = sum(components[key] * (active_weights[key] / weight_total) for key in components)

    contradiction_ratio = (len(contradicting) / decisive) if decisive else 0.0
    penalty = min(max(contradiction_penalty, 0.0) * contradiction_ratio, max(penalty_cap, 0.0))
    value = max(min(base - penalty, 1.0), 0.0)

    if not decisive:
        truth = TruthStatus.UNKNOWN
    elif contradiction_ratio == 0.0:
        truth = TruthStatus.SUPPORTED
    elif contradiction_ratio <= 0.5:
        truth = TruthStatus.CONTESTED
    else:
        truth = TruthStatus.UNSUPPORTED

    components["contradiction_penalty"] = -penalty
    return ConfidenceScore(
        value=value,
        components=components,
        truth_status=truth,
        evidence_count=len(items),
        independent_sources=len(sources),
        contradiction_ratio=contradiction_ratio,
    )


