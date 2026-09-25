"""
Kernel contract tests (§11–§15, §39): versioning, validation, JSON-safety, provenance classes,
contradiction preservation, and the rule that *unknown* can never masquerade as *supported*.
"""
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from vexer_platform.contracts import (
    SCHEMA_VERSION,
    Claim,
    CausalLink,
    Entity,
    Event,
    Evidence,
    EvidenceType,
    IntelligenceSignal,
    ProvenanceClass,
    ProvenanceRecord,
    Relationship,
    RelationshipType,
    Severity,
    SignalType,
    TruthStatus,
    assert_schema_supported,
)
from vexer_platform.errors import ErrorCode, VexerError
from vexer_platform.ids import content_hash, new_ulid

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def test_entity_requires_aware_timestamps_and_orders_validity():
    with pytest.raises(ValidationError):
        Entity(entity_type="COMPANY", name="Vexer", created_at=datetime(2026, 9, 25, 12, 0))  # naïve

    with pytest.raises(ValidationError):
        Entity(
            entity_type="COMPANY",
            name="Vexer",
            valid_from=NOW,
            valid_until=NOW - timedelta(days=1),
        )

    entity = Entity(entity_type="COMPANY", name="Vexer", valid_from=NOW)
    assert entity.entity_id and entity.schema_version == SCHEMA_VERSION
    assert entity.valid_until is None       # open-ended = "still true as far as we know"
    assert entity.created_at.tzinfo is not None


def test_entities_generate_unique_ids_and_json_safe_dumps():
    import json

    first = Entity(entity_type="COMPANY", name="Vexer")
    second = Entity(entity_type="COMPANY", name="Vexer")
    assert first.entity_id != second.entity_id   # distinct records keep distinct ids

    payload = first.to_json_dict()
    assert payload["schema_version"] == SCHEMA_VERSION
    assert isinstance(payload["created_at"], str)   # ISO-8601 string, not a datetime object
    json.dumps(payload)                             # must survive strict serialisation


def test_evidence_rejects_hash_without_algorithm_and_self_contradiction():
    digest = content_hash("tender document body")
    with pytest.raises(ValidationError):
        Evidence(source_id="src-1", source_type=EvidenceType.TENDER, content_hash="deadbeef")

    with pytest.raises(ValidationError):
        Evidence(
            source_id="src-1",
            source_type=EvidenceType.TENDER,
            supports=["claim-1"],
            contradicts=["claim-1"],
        )

    evidence = Evidence(
        source_id="src-1",
        source_type=EvidenceType.TENDER,
        content_hash=digest,
        reliability=0.9,
        retrieved_at=NOW,
    )
    assert evidence.content_hash.startswith("sha256:")
    assert 0.0 <= evidence.reliability <= 1.0


def test_relationship_rejects_self_edges_and_supports_supersession():
    with pytest.raises(ValidationError):
        Relationship(
            source_entity="e1", target_entity="e1", relationship_type=RelationshipType.AFFECTS
        )

    original = Relationship(
        source_entity="regulation-eu-ai-act",
        target_entity="entity-uae-expansion",
        relationship_type=RelationshipType.INCREASES_RISK,
        confidence=0.7,
        valid_from=NOW,
    )
    replacement = Relationship(
        source_entity="regulation-eu-ai-act",
        target_entity="entity-uae-expansion",
        relationship_type=RelationshipType.INCREASES_RISK,
        confidence=0.2,
        valid_from=NOW + timedelta(days=30),
        supersedes=original.relationship_id,
    )
    # History is preserved: the superseded edge keeps its own validity window.
    assert original.valid_until is None
    assert replacement.valid_from > original.valid_from
    assert replacement.supersedes == original.relationship_id


def test_claim_preserves_contradictions_instead_of_overwriting():
    claim_a = Claim(
        subject="Company X",
        predicate="entered",
        object="Market Y",
        truth_status=TruthStatus.SUPPORTED,
        supported_by=["ev-1"],
        confidence=0.8,
        valid_from=NOW,
    )
    claim_b = Claim(
        subject="Company X",
        predicate="exited",
        object="Market Y",
        truth_status=TruthStatus.SUPPORTED,
        supported_by=["ev-2"],
        confidence=0.6,
        valid_from=NOW,
        contradicts=[claim_a.claim_id],
    )
    # Both claims survive; the conflict is represented, not resolved by deletion (§14).
    assert claim_b.contradicts == [claim_a.claim_id]
    assert claim_a.claim_id != claim_b.claim_id

    with pytest.raises(ValidationError):
        Claim(subject="A", predicate="p", object="b", truth_status=TruthStatus.SUPPORTED)

    with pytest.raises(ValidationError):
        Claim(
            subject="A",
            predicate="p",
            object="b",
            truth_status=TruthStatus.CONTESTED,
            supported_by=["ev-1"],          # contested needs a contradicting side as well
        )


def test_signal_truth_status_requires_citations():
    with pytest.raises(ValidationError):
        IntelligenceSignal(
            signal_type=SignalType.RISK,
            headline="Regulatory exposure",
            truth_status=TruthStatus.SUPPORTED,     # cannot be supported without evidence
        )

    signal = IntelligenceSignal(
        signal_type=SignalType.RISK,
        headline="Regulatory exposure",
        truth_status=TruthStatus.SUPPORTED,
        evidence=["ev-1", "ev-2"],
        confidence=0.71,
        severity=Severity.HIGH,
    )
    assert signal.confidence_components == {}
    assert signal.provenance_class is ProvenanceClass.DERIVED

    unknown = IntelligenceSignal(
        signal_type=SignalType.ANOMALY,
        headline="Unverified anomaly",
        provenance_class=ProvenanceClass.UNKNOWN,
    )
    assert unknown.truth_status is TruthStatus.UNKNOWN
    assert unknown.confidence == 0.0


def test_causal_link_requires_distinct_nodes_and_causal_relation_type():
    link = CausalLink(
        cause="ev-1", effect="ev-2", relationship_type=RelationshipType.CAUSES, confidence=0.4
    )
    assert link.provenance_class is ProvenanceClass.HYPOTHESIS

    with pytest.raises(ValidationError):
        CausalLink(cause="ev-1", effect="ev-1")

    with pytest.raises(ValidationError):
        CausalLink(cause="ev-1", effect="ev-2", relationship_type=RelationshipType.DEPENDS_ON)


def test_provenance_record_carries_pipeline_and_model_attribution():
    record = ProvenanceRecord(
        stage="ANALYSIS",
        derived_from=["ev-1"],
        pipeline="vexer-domain6",
        pipeline_version="2.1.0",
        model="gpt-4o-mini",
        model_version="2026-08",
        transformation="synthesis.conf-v1",
        recorded_by="agent_orchestrator",
    )
    assert record.provenance_id
    assert record.model == "gpt-4o-mini"
    assert record.recorded_at.tzinfo is not None


def test_schema_version_negotiation_fails_loudly():
    assert_schema_supported("1.0.0")
    assert_schema_supported("1.9.3")

    with pytest.raises(VexerError) as excinfo:
        assert_schema_supported("2.0.0")
    assert excinfo.value.code is ErrorCode.SCHEMA_VERSION_UNSUPPORTED

    with pytest.raises(VexerError):
        assert_schema_supported("not-a-version")

    with pytest.raises(VexerError):
        assert_schema_supported("")


def test_event_carries_geo_idempotency_and_stays_serialisable():
    event = Event(
        event_type="TENDER_PUBLISHED",
        timestamp=NOW,
        entities=["ent-1"],
        evidence_ids=["ev-1"],
        confidence=0.66,
        geo={"lat": 24.4539, "lon": 54.3773},
        idempotency_key="a" * 32,
        attributes={"country": "AE"},
    )
    assert new_ulid() != event.event_id
    assert event.to_json_dict()["attributes"] == {"country": "AE"}

