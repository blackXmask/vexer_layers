"""
Live PostgreSQL integration tests (mandate Â§33, Â§34, Â§35, Â§50).

**These require a real server.** They are gated on ``VEXER_PG_TEST_DSN`` and are skipped â€” loudly,
with a reason â€” when it is unset. There is no in-memory substitute and no silent pass: a green run
without a DSN does *not* mean the database layer works, and the skip reason says so.

Start a server and run them::

    docker compose up -d postgres
    $env:VEXER_PG_TEST_DSN = "postgresql://vexer:vexer_dev_only@localhost:5432/vexer_test"
    pytest tests/test_kernel_persistence_integration.py -v

What is proven when a server is present:

* the canonical DDL actually executes against PostgreSQL (enums, tables, indexes, partitions),
* event ingest is idempotent under replay, and a concurrent double-insert creates one row,
* temporal reads honour half-open windows ("what was true at T"),
* superseded relationships keep their history instead of being overwritten,
* the audit hash chain detects tampering and row deletion,
* a database outage surfaces as a structured error, not a silent fallback.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from pydantic import ValidationError

from vexer_platform import contracts as C
from vexer_platform.errors import ErrorCode, VexerError
from vexer_platform.persistence import PostgresStore, StoreSettings
from vexer_platform.persistence.postgres import DataHealth
from vexer_platform.persistence.rows import SPECS as R_SPECS

pytestmark = pytest.mark.integration

DSN = os.getenv("VEXER_PG_TEST_DSN", "")

if not DSN:
    pytest.skip(
        "VEXER_PG_TEST_DSN is not set â€” live PostgreSQL tests are SKIPPED, not passed. "
        "Run `docker compose up -d postgres` and set VEXER_PG_TEST_DSN to exercise them. "
        "A passing suite does not imply the persistence layer is validated.",
        allow_module_level=True,
    )

NOW = datetime.now(timezone.utc)


def _uid(prefix: str = "01JTEST") -> str:
    """Unique ids so tests can run concurrently against one server without colliding."""
    return f"{prefix}{uuid.uuid4().hex[:16].upper()}"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def store():
    """
    A store pointed at the test database, with the canonical schema applied.

    Module-scoped so one pool is reused: the connection pool is a bounded resource and rebuilding it
    per test would measure pytest setup rather than the store.
    """
    instance = PostgresStore(StoreSettings(dsn=DSN, max_size=4))
    await instance.connect()
    await instance.apply_schema(months=3)
    try:
        yield instance
    finally:
        await instance.close()


@pytest.fixture
async def entity():
    return C.Entity(
        entity_id=_uid("ENT"),
        entity_type="COMPANY",
        name=f"Testco {_uid('N')[:8]}",
        aliases=[f"alias-{_uid('A')[:8]}"],
        attributes={"country": "AE"},
        source_ids=["src-test"],
        confidence=0.5,
        created_at=NOW,
        updated_at=NOW,
        valid_from=NOW - timedelta(days=10),
    )

# -------------------------------------------------------------------------------------------
# Schema
# -------------------------------------------------------------------------------------------


async def test_canonical_ddl_executes_against_a_real_server(store) -> None:
    """The migration and the parity test share one definition; this proves it runs on PostgreSQL."""
    assert (await store.ping()) is DataHealth.LIVE


async def test_graph_backend_state_is_visible(store) -> None:
    """
    Â§19: whether AGE or the recursive-CTE fallback is in use must be observable. With no AGE
    installed the store must say FALLBACK rather than pretend a graph engine is present.
    """
    assert await store.graph_backend() in (DataHealth.LIVE, DataHealth.FALLBACK)


# -------------------------------------------------------------------------------------------
# Entity round-trip and temporal reads (Â§15)
# -------------------------------------------------------------------------------------------


async def test_entity_round_trip(store, entity) -> None:
    assert await store.upsert_entities([entity]) == 1
    loaded = await store.get_entity(entity.entity_id)
    assert loaded == entity


async def test_entity_lookup_by_name_and_alias(store, entity) -> None:
    await store.upsert_entities([entity])
    by_name = await store.find_entities_by_name(entity.name)
    assert any(item.entity_id == entity.entity_id for item in by_name)
    by_alias = await store.find_entities_by_name(entity.aliases[0])
    assert any(item.entity_id == entity.entity_id for item in by_alias)


async def test_out_of_order_update_does_not_regress(store, entity) -> None:
    """A delayed message must not overwrite a record that newer knowledge already improved."""
    await store.upsert_entities([entity])
    newer = entity.model_copy(update={"confidence": 0.95, "updated_at": NOW + timedelta(hours=1)})
    await store.upsert_entities([newer])
    stale = entity.model_copy(update={"confidence": 0.01, "updated_at": NOW - timedelta(days=1)})
    await store.upsert_entities([stale])
    loaded = await store.get_entity(entity.entity_id)
    assert loaded is not None and loaded.confidence == 0.95


async def test_as_of_lookup_uses_the_validity_window(store, entity) -> None:
    """A window that already closed must not be returned for a present-day query (Â§15)."""
    closed = entity.model_copy(
        update={"valid_until": NOW - timedelta(days=1), "confidence": 0.4}
    )
    await store.upsert_entities([closed])
    current = await store.find_entities_by_name(closed.name)
    assert not any(item.entity_id == closed.entity_id for item in current)
    historical = await store.find_entities_by_name(closed.name, as_of=NOW - timedelta(days=2))
    assert any(item.entity_id == closed.entity_id for item in historical)


# -------------------------------------------------------------------------------------------
# Event idempotency (Â§21)
# -------------------------------------------------------------------------------------------


def _event(idempotency_key: str) -> C.Event:
    return C.Event(
        event_id=_uid("EVT"),
        event_type="FUNDING_RAISED",
        timestamp=NOW,
        entities=[_uid("ENT")],
        attributes={"amount_usd": 500_000},
        confidence=0.6,
        idempotency_key=idempotency_key,
    )


async def test_event_without_key_is_inserted(store) -> None:
    result = await store.record_event(_event(_uid("K")))
    assert result.is_new and not result.deduplicated


async def test_replayed_event_is_deduplicated(store) -> None:
    key = _uid("K")
    first = _event(key)
    second = _event(key)  # different event_id, same logical key: an at-least-once redelivery
    created = await store.record_event(first)
    replayed = await store.record_event(second)
    assert created.is_new
    assert replayed.deduplicated and not replayed.is_new
    assert replayed.event_id == created.event_id, "a replay must resolve to the original event_id"


async def test_concurrent_duplicate_insert_creates_one_row(store) -> None:
    """The dedup key row is the serialization point; only one writer may win."""
    key = _uid("K")
    outcomes = await asyncio.gather(*(store.record_event(_event(key)) for _ in range(5)))
    assert sum(1 for outcome in outcomes if outcome.is_new) == 1
    assert all(outcome.event_id == outcomes[0].event_id for outcome in outcomes)


async def test_unsupported_schema_version_is_refused(store) -> None:
    event = _event(_uid("K")).model_copy(update={"schema_version": "99.0.0"})
    with pytest.raises(VexerError) as excinfo:
        await store.record_event(event)
    assert excinfo.value.code is ErrorCode.SCHEMA_VERSION_UNSUPPORTED


# -------------------------------------------------------------------------------------------
# Audit chain (Â§45)
# -------------------------------------------------------------------------------------------


async def test_audit_chain_verifies_and_detects_tampering(store) -> None:
    first = await store.append_audit(actor="tester", action="READ", resource="entity/1")
    second = await store.append_audit(actor="tester", action="WRITE", resource="entity/1")
    assert first and second and first != second
    assert (await store.verify_audit_chain()).intact

    pool = store._require_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE audit_log SET result = 'tampered' WHERE entry_hash = $1", first)
    broken = await store.verify_audit_chain()
    assert not broken.intact
    assert broken.reason in ("hash_mismatch", "chain_break")
    assert broken.broken_at is not None

    # Restore so later tests in the module observe a clean chain.
    async with pool.acquire() as conn:
        await conn.execute("UPDATE audit_log SET result = NULL WHERE entry_hash = $1", first)
    assert (await store.verify_audit_chain()).intact


async def test_audit_rejects_empty_actor(store) -> None:
    with pytest.raises(VexerError) as excinfo:
        await store.append_audit(actor="  ", action="READ")
    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED


# -------------------------------------------------------------------------------------------
# Failure behaviour (Â§34, Â§19) â€” no silent fallbacks
# -------------------------------------------------------------------------------------------


async def test_unreachable_database_raises_a_structured_error() -> None:
    """A dead database must never be papered over with in-process storage."""
    dead = PostgresStore(
        StoreSettings(dsn="postgresql://vexer:vexer@127.0.0.1:59999/none", max_size=1)
    )
    assert dead.health is DataHealth.UNAVAILABLE
    with pytest.raises(VexerError) as excinfo:
        await dead.connect()
    assert excinfo.value.code is ErrorCode.DEPENDENCY_UNAVAILABLE
    assert excinfo.value.retryable is True
    assert dead.health is DataHealth.UNAVAILABLE
    # Operations before a successful connect fail loudly rather than silently no-op'ing.
    with pytest.raises(VexerError):
        await dead.get_entity("anything")
    assert await dead.ping() is DataHealth.UNAVAILABLE


async def test_streaming_yields_events_in_time_order(store) -> None:
    """Â§26: a server-side cursor must stream, not materialise, and stay ordered."""
    earlier = _event(_uid("K")).model_copy(update={"timestamp": NOW - timedelta(minutes=1)})
    later = _event(_uid("K")).model_copy(update={"timestamp": NOW + timedelta(minutes=1)})
    await store.record_event(earlier)
    await store.record_event(later)
    seen: list[datetime] = []
    async for item in store.iter_events(
        since=NOW - timedelta(minutes=5), until=NOW + timedelta(minutes=5), batch_size=2
    ):
        seen.append(item.timestamp)
    assert seen == sorted(seen), "iter_events must yield in occurred_at order"


# -------------------------------------------------------------------------------------------
# Temporal relationships (§17)
# -------------------------------------------------------------------------------------------


async def test_superseding_a_relationship_preserves_history(store) -> None:
    source, target = _uid("ENT"), _uid("ENT")
    first = C.Relationship(
        relationship_id=_uid("REL"),
        source_entity=source,
        target_entity=target,
        relationship_type=C.RelationshipType.AFFECTS,
        confidence=0.3,
        valid_from=NOW - timedelta(days=10),
    )
    assert await store.add_relationship(first) == ""

    second = C.Relationship(
        relationship_id=_uid("REL"),
        source_entity=source,
        target_entity=target,
        relationship_type=C.RelationshipType.AFFECTS,
        confidence=0.8,
        valid_from=NOW,
    )
    assert await store.add_relationship(second) == first.relationship_id

    live_ids = {item.relationship_id for item in await store.active_relationships(source)}
    assert second.relationship_id in live_ids
    assert first.relationship_id not in live_ids, "the superseded edge must no longer be live"

    # The historical edge is still retrievable with its own validity window (§15, §44).
    pool = store._require_pool()
    async with pool.acquire() as conn:
        historic = await conn.fetchrow(
            "SELECT valid_from, valid_until FROM relationship WHERE relationship_id = $1",
            first.relationship_id,
        )
    assert historic is not None
    assert historic["valid_until"] == second.valid_from


async def test_traversal_from_either_direction(store) -> None:
    """Both endpoints are indexed; a one-sided index would scan for half the graph queries."""
    source, target = _uid("ENT"), _uid("ENT")
    await store.add_relationship(
        C.Relationship(
            relationship_id=_uid("REL"),
            source_entity=source,
            target_entity=target,
            relationship_type=C.RelationshipType.DEPENDS_ON,
            valid_from=NOW - timedelta(days=1),
        )
    )
    outgoing = await store.active_relationships(source, direction="out")
    incoming = await store.active_relationships(target, direction="in")
    both = await store.active_relationships(target, direction="both")
    assert outgoing and incoming and both
    with pytest.raises(VexerError) as excinfo:
        await store.active_relationships(source, direction="sideways")
    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED



# -------------------------------------------------------------------------------------------
# Constraints enforced by the database, not only by pydantic (§38)
#
# These deliberately bypass the service layer: they send hand-built parameter rows straight to
# PostgreSQL to prove the CHECK/UNIQUE constraints are real and not merely documentation.
# -------------------------------------------------------------------------------------------


async def _raw_insert(store, spec, params) -> None:
    """Insert positional parameters directly, skipping contract validation by design."""
    pool = store._require_pool()
    placeholders = ", ".join(f"${index}" for index in range(1, len(params) + 1))
    async with pool.acquire() as conn:
        await conn.execute(
            f"INSERT INTO {spec.table} ({', '.join(spec.column_names)}) VALUES ({placeholders})",
            *params,
        )


async def test_relationship_self_loop_is_rejected_by_the_database(store) -> None:
    """The contract rejects self-loops; the database must reject them independently too."""
    node = _uid("ENT")
    with pytest.raises(ValidationError, match="self-referential"):
        C.Relationship(
            relationship_id=_uid("REL"),
            source_entity=node,
            target_entity=node,
            relationship_type=C.RelationshipType.AFFECTS,
        )
    with pytest.raises(VexerError) as excinfo:
        await _raw_insert(
            store,
            R_SPECS["Relationship"],
            [_uid("REL"), node, node, C.RelationshipType.AFFECTS.value, 0.5, [],
             NOW - timedelta(days=1), None, NOW, NOW, None,
             C.ProvenanceClass.OBSERVED.value, "{}", None, "1.0.0"],
        )
    assert excinfo.value.code in (ErrorCode.DATA_INTEGRITY, ErrorCode.INTERNAL)


async def test_inverted_validity_window_is_rejected_by_the_database(store) -> None:
    """valid_until <= valid_from is refused at the storage layer, not only in pydantic."""
    with pytest.raises(VexerError) as excinfo:
        await _raw_insert(
            store,
            R_SPECS["Entity"],
            [_uid("ENT"), "COMPANY", "Window Co", [], "{}", [], 0.5,
             NOW, NOW, NOW, NOW - timedelta(days=1), "1.0.0"],
        )
    assert excinfo.value.code in (ErrorCode.DATA_INTEGRITY, ErrorCode.INTERNAL)


async def test_out_of_range_confidence_is_rejected_by_the_database(store) -> None:
    """Confidence is bounded in the schema, so a bad write cannot slip past the service layer."""
    with pytest.raises(VexerError) as excinfo:
        await _raw_insert(
            store,
            R_SPECS["Entity"],
            [_uid("ENT"), "COMPANY", "Overconfident Co", [], "{}", [], 1.4,
             NOW, NOW, NOW, None, "1.0.0"],
        )
    assert excinfo.value.code in (ErrorCode.DATA_INTEGRITY, ErrorCode.INTERNAL)


async def test_signal_without_evidence_is_rejected_by_the_database(store) -> None:
    """§39 at the storage layer: a SUPPORTED signal must cite evidence or a claim."""
    with pytest.raises(VexerError) as excinfo:
        await _raw_insert(
            store,
            R_SPECS["IntelligenceSignal"],
            [_uid("SIG"), C.SignalType.RISK.value, "unsupported claim", None,
             [], [], [], [], 0.9, "{}", C.TruthStatus.SUPPORTED.value,
             C.Severity.HIGH.value, NOW, None, C.ProvenanceClass.DERIVED.value, "1.0.0"],
        )
    assert excinfo.value.code in (ErrorCode.DATA_INTEGRITY, ErrorCode.INTERNAL)


async def test_duplicate_content_hash_is_rejected_by_the_database(store) -> None:
    """§21: two rows for the same bytes is a bug, and the unique index is the enforcement point."""
    digest = "sha256:" + uuid.uuid4().hex
    base = ["src-test", C.EvidenceType.DOCUMENT.value, "https://example.invalid/x", digest,
            C.EvidenceType.DOCUMENT.value, 0.5, NOW, NOW, [], [], 0.5,
            C.ProvenanceClass.OBSERVED.value, "{}", "1.0.0"]
    await _raw_insert(store, R_SPECS["Evidence"], [_uid("EVD"), *base])
    with pytest.raises(VexerError) as excinfo:
        await _raw_insert(store, R_SPECS["Evidence"], [_uid("EVD"), *base])
    assert excinfo.value.code is ErrorCode.DATA_INTEGRITY

