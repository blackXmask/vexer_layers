"""
Contract ↔ row mapping behaviour (mandate §38).

The mapping is pure Python, so it is fully testable **without a database** — which is deliberate:
the encoding rules, the parameterisation guarantees and the failure modes are all verifiable in
every CI run, while live-server behaviour lives in ``test_kernel_persistence_integration.py``.

What is proven here:

* every contract round-trips through ``to_params``/``from_row`` unchanged,
* the ``timestamp`` → ``occurred_at`` rename is applied in exactly one direction and never leaks,
* jsonb, ``text[]`` and enum columns encode/decode to the right wire types,
* generated statements are fully parameterised — values are never interpolated (§38 injection),
* malformed input fails loudly rather than writing something subtly wrong.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from vexer_platform import contracts as C
from vexer_platform.persistence import rows as R

NOW = datetime(2026, 5, 4, 12, 30, tzinfo=timezone.utc)


def _samples() -> list:
    """One fully-populated instance of every persisted contract."""
    return [
        C.Entity(
            entity_id="01J0000000000000000000001",
            entity_type="COMPANY",
            name="Acme Corp",
            aliases=["Acme", "ACME"],
            attributes={"country": "AE", "employees": 120},
            source_ids=["src-1", "src-2"],
            confidence=0.82,
            created_at=NOW,
            updated_at=NOW,
            valid_from=NOW,
        ),
        C.Evidence(
            evidence_id="01J0000000000000000000002",
            source_id="src-1",
            source_type=C.EvidenceType.NEWS,
            uri="https://example.invalid/a",
            content_hash="sha256:abc123",
            evidence_type=C.EvidenceType.NEWS,
            reliability=0.7,
            published_at=NOW - timedelta(days=2),
            retrieved_at=NOW,
            supports=["claim-1"],
            confidence=0.6,
            attributes={"lang": "en"},
        ),
        C.Event(
            event_id="01J0000000000000000000003",
            event_type="FUNDING_RAISED",
            timestamp=NOW,
            entities=["01J0000000000000000000001"],
            source_ids=["src-1"],
            evidence_ids=["01J0000000000000000000002"],
            attributes={"amount_usd": 1_000_000},
            confidence=0.75,
            geo={"lat": 25.2, "lon": 55.27},
            idempotency_key="idem-1",
        ),
        C.Relationship(
            relationship_id="01J0000000000000000000004",
            source_entity="01J0000000000000000000001",
            target_entity="01J0000000000000000000005",
            relationship_type=C.RelationshipType.CREATES_OPPORTUNITY,
            confidence=0.4,
            evidence_ids=["01J0000000000000000000002"],
            valid_from=NOW,
            created_at=NOW,
            recorded_at=NOW,
            attributes={"basis": "tender"},
        ),
        C.CausalLink(
            causal_id="01J0000000000000000000006",
            cause="01J0000000000000000000003",
            effect="01J0000000000000000000005",
            relationship_type=C.RelationshipType.CAUSES,
            confidence=0.3,
            evidence_ids=["01J0000000000000000000002"],
            timestamp=NOW,
            mechanism="funding enabled the bid",
        ),
        C.Claim(
            claim_id="01J0000000000000000000007",
            subject="Acme Corp",
            predicate="entered_market",
            object="UAE",
            confidence=0.55,
            truth_status=C.TruthStatus.CONTESTED,
            supported_by=["01J0000000000000000000002"],
            contradicted_by=["01J0000000000000000000008"],
            contradicts=["01J0000000000000000000009"],
            valid_from=NOW,
            attributes={"region": "Gulf"},
        ),
        C.IntelligenceSignal(
            signal_id="01J0000000000000000000010",
            signal_type=C.SignalType.OPPORTUNITY,
            headline="Acme expands into UAE",
            detail="funding followed a tender win",
            entities=["01J0000000000000000000001"],
            events=["01J0000000000000000000003"],
            evidence=["01J0000000000000000000002"],
            confidence=0.5,
            confidence_components={"evidence": 0.7, "freshness": 0.9},
            truth_status=C.TruthStatus.SUPPORTED,
            severity=C.Severity.MEDIUM,
            timestamp=NOW,
        ),
        C.ProvenanceRecord(
            provenance_id="01J0000000000000000000011",
            stage="ANALYSIS",
            source_ids=["src-1"],
            evidence_ids=["01J0000000000000000000002"],
            derived_from=["01J0000000000000000000012"],
            pipeline="vexer.correlate",
            pipeline_version="1.2.0",
            model="claude",
            model_version="2026-05",
            transformation="entity_resolution",
            recorded_by="worker-3",
            recorded_at=NOW,
            provenance_class=C.ProvenanceClass.DERIVED,
        ),
    ]

SAMPLES = _samples()
IDS = [type(instance).__name__ for instance in SAMPLES]


# -------------------------------------------------------------------------------------------
# Round-trip
# -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("instance", SAMPLES, ids=IDS)
def test_round_trip_is_lossless(instance) -> None:
    """A contract must survive encode → decode unchanged, or persistence is lossy."""
    spec = R.spec_for(instance)
    row = dict(zip(spec.column_names, R.to_params(instance, spec)))
    assert R.from_row(type(instance), row, spec) == instance


@pytest.mark.parametrize("instance", SAMPLES, ids=IDS)
def test_param_count_matches_column_count(instance) -> None:
    spec = R.spec_for(instance)
    assert len(R.to_params(instance, spec)) == len(spec.column_names)


def test_timestamp_is_stored_as_occurred_at() -> None:
    """
    The one documented rename. It must apply to storage while the contract keeps ``timestamp``,
    otherwise a reader would have to know about the physical name.
    """
    event = next(s for s in SAMPLES if isinstance(s, C.Event))
    spec = R.SPECS["Event"]
    assert "timestamp" in spec.fields
    assert "occurred_at" in spec.column_names
    assert "timestamp" not in spec.column_names
    params = dict(zip(spec.column_names, R.to_params(event, spec)))
    assert params["occurred_at"] == event.timestamp
    assert isinstance(params["occurred_at"], datetime)


# -------------------------------------------------------------------------------------------
# Wire encoding
# -------------------------------------------------------------------------------------------


def test_json_columns_encode_to_sorted_strings() -> None:
    """jsonb parameters are JSON *strings*; a raw dict would fail deep inside the driver."""
    event = next(s for s in SAMPLES if isinstance(s, C.Event))
    spec = R.SPECS["Event"]
    params = dict(zip(spec.column_names, R.to_params(event, spec)))
    assert isinstance(params["attributes"], str)
    assert json.loads(params["attributes"]) == {"amount_usd": 1_000_000}
    # Sorted keys: a stable encoding keeps payload hashes in event_dedup reproducible.
    assert params["attributes"] == '{"amount_usd": 1000000}'


def test_json_decoding_accepts_text_and_parsed_values() -> None:
    """asyncpg returns jsonb as text; an already-parsed dict must decode identically."""
    as_text = R._decode('{"country": "AE"}', "json")
    as_dict = R._decode({"country": "AE"}, "json")
    assert as_text == as_dict == {"country": "AE"}


def test_array_columns_encode_to_lists_of_str() -> None:
    entity = next(s for s in SAMPLES if isinstance(s, C.Entity))
    spec = R.SPECS["Entity"]
    params = dict(zip(spec.column_names, R.to_params(entity, spec)))
    assert params["aliases"] == ["Acme", "ACME"]
    assert params["source_ids"] == ["src-1", "src-2"]


def test_enum_columns_encode_to_plain_values() -> None:
    """Enums must reach the driver as their string value, not as a Python Enum object."""
    event = next(s for s in SAMPLES if isinstance(s, C.Event))
    spec = R.SPECS["Event"]
    params = dict(zip(spec.column_names, R.to_params(event, spec)))
    assert params["provenance_class"] == C.ProvenanceClass.OBSERVED.value
    assert isinstance(params["provenance_class"], str)


def test_none_passes_through_as_sql_null() -> None:
    event = C.Event(event_type="X", timestamp=NOW)
    spec = R.SPECS["Event"]
    params = dict(zip(spec.column_names, R.to_params(event, spec)))
    assert params["geo"] is None
    assert params["provenance_id"] is None


def test_non_datetime_in_a_timestamp_column_is_rejected() -> None:
    """A wrong type must fail at the boundary, not be coerced into a misleading value."""
    with pytest.raises(TypeError):
        R._encode("2026-05-04", "ts")


def test_malformed_row_fails_loudly() -> None:
    """A row that violates a contract invariant must raise, never be silently coerced."""
    spec = R.SPECS["Entity"]
    row = dict(zip(spec.column_names, R.to_params(SAMPLES[0], spec)))
    row["confidence"] = 1.5
    with pytest.raises(ValidationError):
        R.from_row(C.Entity, row, spec)


def test_unknown_model_has_no_spec() -> None:
    class NotPersisted(C.Entity):
        pass

    with pytest.raises(KeyError, match="no TableSpec"):
        R.spec_for(NotPersisted)


# -------------------------------------------------------------------------------------------
# Statement safety (§38: no injection on the write path)
# -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name,spec", sorted(R.SPECS.items()))
def test_insert_is_fully_parameterised(name, spec) -> None:
    """Every value must be a placeholder; a literal value in the SQL is an injection surface."""
    statement = R.insert_statement(spec)
    assert statement.count("$") >= len(spec.columns)
    for fragment in ("'", '"', "%s", "?"):
        assert fragment not in statement, f"{name}: literal {fragment!r} in generated SQL"
    assert statement.startswith(f"INSERT INTO {spec.table} (")


@pytest.mark.parametrize("name,spec", sorted(R.SPECS.items()))
def test_json_placeholders_are_casted(name, spec) -> None:
    """jsonb parameters need an explicit cast; asyncpg sends them as text otherwise."""
    statement = R.insert_statement(spec)
    for index, column in enumerate(spec.columns, start=1):
        expected = f"${index}::jsonb" if column.kind == "json" else f"${index}"
        assert expected in statement, f"{name}: placeholder for {column.name} is wrong"


@pytest.mark.parametrize("name,spec", sorted(R.SPECS.items()))
def test_upsert_requires_explicit_conflict_target(name, spec) -> None:
    """Which columns are authoritative on conflict is a domain decision, never inferred."""
    with pytest.raises(ValueError, match="explicit conflict target"):
        R.upsert_statement(spec, conflict=(), updates=("confidence",))


def test_upsert_only_touches_declared_columns() -> None:
    spec = R.SPECS["Evidence"]
    statement = R.upsert_statement(
        spec, conflict=("evidence_id",), updates=("attributes", "confidence")
    )
    assert "ON CONFLICT (evidence_id) DO UPDATE SET" in statement
    assert statement.count("EXCLUDED.") == 2
    # The immutable parts of evidence must not appear as update assignments.
    assert "reliability = EXCLUDED.reliability" not in statement
    assert "supports = EXCLUDED.supports" not in statement


def test_select_casts_jsonb_columns() -> None:
    """Every jsonb column must be cast back to text, and no other column may be cast."""
    for name, spec in R.SPECS.items():
        projection = R.select_columns(spec)
        for column in spec.columns:
            fragment = f"{column.name}::text AS {column.name}"
            if column.kind == "json":
                assert fragment in projection, f"{name}: {column.name} is not cast to text"
            else:
                assert "::" not in projection.split(column.name)[1].split(",")[0], (
                    f"{name}: {column.name} should not be cast"
                )


def test_select_projection_covers_every_column() -> None:
    for name, spec in R.SPECS.items():
        projection = R.select_columns(spec)
        for column in spec.column_names:
            assert column in projection, f"{name}: column {column} missing from projection"

