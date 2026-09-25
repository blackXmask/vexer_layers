"""
Confidence methodology tests (§13): determinism, documented monotonicity, decay, contradiction
penalties, independence scaling and the hard rule that no evidence means zero confidence.
"""
from datetime import datetime, timedelta, timezone

import pytest

from vexer_platform.confidence import (
    CONFIDENCE_METHODOLOGY_VERSION,
    EvidenceObservation,
    Stance,
    freshness_factor,
    score_confidence,
)
from vexer_platform.contracts import TruthStatus
from vexer_platform.errors import VexerError

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _obs(source: str, *, stance=Stance.SUPPORTS, reliability=0.9, age_days=0.0) -> EvidenceObservation:
    return EvidenceObservation(
        source_id=source,
        stance=stance,
        reliability=reliability,
        quality=reliability,
        published_at=NOW - timedelta(days=age_days),
    )


def test_no_evidence_yields_zero_confidence_and_unknown_truth():
    score = score_confidence([], now=NOW)
    assert score.value == 0.0
    assert score.truth_status is TruthStatus.UNKNOWN
    assert score.evidence_count == 0
    assert score.components == {}          # no fabricated component values
    assert score.methodology_version == CONFIDENCE_METHODOLOGY_VERSION


def test_supported_case_is_explainable_and_deterministic():
    evidence = [_obs("regulator.ae"), _obs("tender-board.ae")]
    first = score_confidence(evidence, now=NOW)
    second = score_confidence(evidence, now=NOW)
    assert first.to_dict() == second.to_dict()   # pure function: same inputs, same output
    assert first.truth_status is TruthStatus.SUPPORTED
    assert first.independent_sources == 2
    assert first.value > 0.8
    assert "evidence_quality" in first.components and "freshness" in first.components
    assert first.components["contradiction_penalty"] == 0.0
    assert CONFIDENCE_METHODOLOGY_VERSION in first.rationale()


def test_contradicting_evidence_lowers_confidence_and_changes_truth_status():
    unanimous = score_confidence([_obs("a"), _obs("b"), _obs("c"), _obs("d")], now=NOW)
    minority = score_confidence(
        [_obs("a"), _obs("b"), _obs("c"), _obs("d", stance=Stance.CONTRADICTS)], now=NOW
    )
    majority = score_confidence(
        [
            _obs("a", stance=Stance.CONTRADICTS),
            _obs("b", stance=Stance.CONTRADICTS),
            _obs("c", stance=Stance.CONTRADICTS),
            _obs("d"),
        ],
        now=NOW,
    )
    assert minority.value < unanimous.value
    assert majority.value < minority.value
    assert unanimous.truth_status is TruthStatus.SUPPORTED
    assert minority.truth_status is TruthStatus.CONTESTED      # 25% contradict -> contested
    assert majority.truth_status is TruthStatus.UNSUPPORTED    # 75% contradict -> unsupported
    assert minority.components["contradiction_penalty"] < 0.0


def test_more_independent_sources_raise_agreement_component():
    one_source = score_confidence([_obs("solo"), _obs("solo")], now=NOW)
    three_sources = score_confidence([_obs("a"), _obs("b"), _obs("c")], now=NOW)
    assert three_sources.components["source_agreement"] > one_source.components["source_agreement"]
    assert three_sources.independent_sources == 3


def test_freshness_decays_with_age_and_clamps_future_dates():
    fresh = score_confidence([_obs("a", age_days=0)], now=NOW)
    stale = score_confidence([_obs("a", age_days=365)], now=NOW)
    assert fresh.components["freshness"] > stale.components["freshness"]
    assert stale.value < fresh.value

    future = score_confidence([_obs("a", age_days=-10)], now=NOW)
    assert future.components["freshness"] == pytest.approx(1.0)   # never a negative age


def test_freshness_factor_half_life_is_exactly_one_half():
    assert freshness_factor(0, half_life_days=90) == pytest.approx(1.0)
    assert freshness_factor(90, half_life_days=90) == pytest.approx(0.5)
    assert freshness_factor(180, half_life_days=90) == pytest.approx(0.25)
    with pytest.raises(VexerError):
        freshness_factor(1, half_life_days=0)


def test_unknown_age_uses_documented_neutral_prior():
    score = score_confidence([EvidenceObservation(source_id="a", reliability=0.8)], now=NOW)
    assert score.components["freshness"] == pytest.approx(0.5)   # documented prior, not 1.0


def test_neutral_evidence_never_counts_as_support():
    score = score_confidence([_obs("a", stance=Stance.NEUTRAL)], now=NOW)
    assert score.truth_status is TruthStatus.UNKNOWN
    assert score.contradiction_ratio == 0.0
    assert score.components["source_agreement"] == 0.0


def test_model_confidence_is_optional_and_renormalises_weights():
    without_model = score_confidence([_obs("a")], now=NOW)
    with_model = score_confidence([_obs("a")], now=NOW, model_confidence=0.2)
    assert "model_confidence" not in without_model.components
    assert with_model.components["model_confidence"] == pytest.approx(0.2)
    # A low model confidence must drag the blended score down, not be ignored.
    assert with_model.value < without_model.value


def test_invalid_inputs_raise_structured_errors():
    with pytest.raises(VexerError):
        EvidenceObservation(source_id="a", reliability=1.5)

    with pytest.raises(VexerError):
        EvidenceObservation(source_id="a", published_at=datetime(2026, 9, 25, 12, 0))  # naïve

    with pytest.raises(VexerError):
        EvidenceObservation(source_id="")

    with pytest.raises(VexerError):
        score_confidence([_obs("a")], now=NOW, model_confidence=2.0)

    with pytest.raises(VexerError):
        score_confidence([_obs("a")], now=NOW, weights={"unrelated_component": 1.0})


def test_score_stays_within_unit_interval_even_with_extreme_penalty_request():
    score = score_confidence(
        [_obs("a"), _obs("b", stance=Stance.CONTRADICTS)],
        now=NOW,
        contradiction_penalty=5.0,
        penalty_cap=0.6,
    )
    assert 0.0 <= score.value <= 1.0
    assert score.components["contradiction_penalty"] == pytest.approx(-0.6)


def test_more_contradictions_never_increase_confidence():
    values = [
        score_confidence(
            [_obs("a"), _obs("b")] + [_obs(f"c{i}", stance=Stance.CONTRADICTS) for i in range(k)],
            now=NOW,
        ).value
        for k in range(4)
    ]
    assert values == sorted(values, reverse=True)
    assert values[0] > values[-1]

