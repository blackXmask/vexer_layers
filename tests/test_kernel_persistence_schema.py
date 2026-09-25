"""
Contract ↔ schema parity (mandate §38, §50, RULE "no invented claims").

These tests are the reason ``vexer_platform.persistence.schema`` exists as a single source of truth.
They run **without a database** — deliberately — so schema drift is caught in every CI run, not only
when someone remembers to stand up PostgreSQL.

What is proven here:

* every pydantic contract field has a physical column (a contract change without a migration fails),
* every column is accounted for by a contract field or explicitly declared database-managed,
* enum vocabularies in the DDL equal the contract enum members,
* contract invariants that matter for storage (no self-loops, validity windows, evidence citation)
  are also enforced as database constraints, so a writer bypassing pydantic cannot violate them,
* the generated DDL is well-formed enough for Alembic to execute in offline mode.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from vexer_platform import contracts as C
from vexer_platform.persistence import rows as R
from vexer_platform.persistence import schema as S

# -------------------------------------------------------------------------------------------
# Minimal SQL introspection
#
# Parsing is used only in tests, against a schema we generate ourselves, to compare structure with
# the contracts. Runtime code never parses SQL.
# -------------------------------------------------------------------------------------------

_CREATE_TABLE = re.compile(
    r"CREATE TABLE IF NOT EXISTS\s+(?P<name>\w+)\s*\((?P<body>.*?)\n\)\s*(PARTITION BY[^;]*)?;",
    re.IGNORECASE | re.DOTALL,
)
_CREATE_TYPE = re.compile(
    r"CREATE TYPE\s+(?P<name>\w+)\s+AS ENUM\s*\((?P<values>[^)]*)\)", re.IGNORECASE | re.DOTALL
)
_CONSTRAINT = re.compile(
    r"CONSTRAINT\s+(?P<name>\w+)\s+(?P<body>.*?)(?=,\s*\n|\)\s*;|$)", re.DOTALL
)
_LINE_COMMENT = re.compile(r"--[^\n]*")


def _table_body(name: str) -> str:
    for statement in S.TABLES_DDL:
        match = _CREATE_TABLE.search(statement)
        if match and match.group("name") == name:
            return _LINE_COMMENT.sub("", match.group("body"))
    raise AssertionError(f"table {name} not found in schema.TABLES_DDL")


def _ddl_columns(name: str) -> set[str]:
    """Column names declared for ``name`` in the canonical DDL."""
    body = _table_body(name)
    chunks: list[str] = []
    depth = 0
    current: list[str] = []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            chunks.append("".join(current))
            current = []
        else:
            current.append(char)
    chunks.append("".join(current))
    result: set[str] = set()
    for chunk in chunks:
        text = chunk.strip()
        if not text or text.upper().startswith(("CONSTRAINT", "PRIMARY KEY", "UNIQUE", "CHECK")):
            continue
        result.add(text.split()[0].strip('"'))
    return result


def _ddl_text(name: str) -> str:
    """The full ``CREATE TABLE`` statement for ``name``, upper-cased for substring assertions.

    Some guarantees (bounded confidence, a dedup primary key) are expressed as *inline* column
    CHECKs rather than named table constraints, so they are only visible in the whole statement.
    """
    for statement in S.TABLES_DDL:
        match = _CREATE_TABLE.search(statement)
        if match and match.group("name") == name:
            return _LINE_COMMENT.sub("", statement).upper()
    raise AssertionError(f"table {name} not found in schema.TABLES_DDL")


def _ddl_constraints(name: str) -> dict[str, str]:
    return {
        match.group("name"): " ".join(match.group("body").split()).upper()
        for match in _CONSTRAINT.finditer(_table_body(name))
    }


def _ddl_enums() -> dict[str, tuple[str, ...]]:
    enums: dict[str, tuple[str, ...]] = {}
    for statement in S.enum_definitions():
        match = _CREATE_TYPE.search(statement)
        if match:
            enums[match.group("name")] = tuple(
                value.strip().strip("'") for value in match.group("values").split(",")
            )
    return enums


# -------------------------------------------------------------------------------------------
# Contract → column coverage
# -------------------------------------------------------------------------------------------

PERSISTED_MODELS = [
    C.Entity,
    C.Evidence,
    C.Event,
    C.Relationship,
    C.CausalLink,
    C.Claim,
    C.IntelligenceSignal,
    C.ProvenanceRecord,
]


@pytest.mark.parametrize("model", PERSISTED_MODELS, ids=lambda m: m.__name__)
def test_every_contract_field_has_a_column(model) -> None:
    """A field added to a contract but not to the schema must fail here, not in production."""
    spec = R.spec_for(model)
    contract_fields = set(model.model_fields)
    mapped_fields = set(spec.fields)
    assert mapped_fields == contract_fields, (
        f"{model.__name__}: unmapped contract fields {sorted(contract_fields - mapped_fields)}; "
        f"phantom mapped fields {sorted(mapped_fields - contract_fields)}"
    )


@pytest.mark.parametrize("model", PERSISTED_MODELS, ids=lambda m: m.__name__)
def test_every_column_is_accounted_for(model) -> None:
    """No column may exist without a contract field unless explicitly declared db-managed."""
    spec = R.spec_for(model)
    declared = _ddl_columns(spec.table)
    mapped = set(spec.column_names)
    extra_allowed = set(spec.db_managed)
    assert mapped <= declared, f"{spec.table}: mapped columns missing from DDL: {sorted(mapped - declared)}"
    assert declared <= (mapped | extra_allowed), (
        f"{spec.table}: DDL columns with no contract field and not declared db_managed: "
        f"{sorted(declared - mapped - extra_allowed)}"
    )


def test_every_declared_table_is_known() -> None:
    created = {_CREATE_TABLE.search(s).group("name") for s in S.TABLES_DDL}
    assert created == set(S.TABLES), "S.TABLES and TABLES_DDL disagree"


def test_every_spec_table_exists_in_ddl() -> None:
    for name, spec in R.SPECS.items():
        # _ddl_columns raises AssertionError when the table is absent, so this asserts existence
        # and that at least one column was parsed.
        assert _ddl_columns(spec.table), f"{name} -> {spec.table} missing or empty in DDL"


# -------------------------------------------------------------------------------------------
# Vocabulary parity
# -------------------------------------------------------------------------------------------


def test_enum_values_match_contract_members() -> None:
    """The DDL vocabulary is closed; a contract enum change without a schema change fails here."""
    enums = _ddl_enums()
    expected = {
        "vx_evidence_type": C.EvidenceType,
        "vx_relationship_type": C.RelationshipType,
        "vx_provenance_class": C.ProvenanceClass,
        "vx_truth_status": C.TruthStatus,
        "vx_severity": C.Severity,
        "vx_signal_type": C.SignalType,
    }
    assert set(enums) == set(expected) == set(S.ENUM_VALUES)
    for type_name, enum_cls in expected.items():
        assert enums[type_name] == tuple(member.value for member in enum_cls), (
            f"{type_name} vocabulary differs from {enum_cls.__name__}"
        )


@pytest.mark.parametrize("name,spec", sorted(R.SPECS.items()))
def test_enum_columns_reference_declared_types(name, spec) -> None:
    """Every ``enum``-kind column must point at a declared type, not at free text."""
    body = _table_body(spec.table)
    for column in spec.columns:
        if column.kind != "enum":
            continue
        assert re.search(rf"\b{column.name}\s+vx_\w+", body), (
            f"{spec.table}.{column.name} is not a declared enum column"
        )


# -------------------------------------------------------------------------------------------
# Invariants duplicated as database constraints (§38: writers must not bypass validation)
# -------------------------------------------------------------------------------------------


def test_temporal_window_constraints_exist() -> None:
    """Validity windows are half-open; the database refuses inverted windows too."""
    for table, constraint in (
        ("entity", "entity_window_ck"),
        ("relationship", "relationship_window_ck"),
        ("claim", "claim_window_ck"),
    ):
        constraints = _ddl_constraints(table)
        assert constraint in constraints, f"{table} is missing {constraint}"
        assert "VALID_UNTIL IS NULL OR VALID_UNTIL > VALID_FROM" in constraints[constraint]


def test_no_self_loop_constraints_exist() -> None:
    for table in ("relationship", "causal_link"):
        constraints = _ddl_constraints(table)
        assert any("SELFLOOP" in name.upper() for name in constraints), f"{table} allows self-loops"


def test_signal_must_cite_evidence_constraint_exists() -> None:
    """§39: a non-UNKNOWN truth status must cite evidence or claims, enforced in the database."""
    constraints = _ddl_constraints("intelligence_signal")
    assert "signal_cites_evidence_ck" in constraints
    assert "CARDINALITY(EVIDENCE) > 0" in constraints["signal_cites_evidence_ck"]


def test_evidence_hash_uniqueness_is_enforced() -> None:
    """Dedup by content hash must be a database guarantee, not an application convention."""
    joined = "\n".join(S.INDEXES)
    assert "UNIQUE INDEX" in joined
    assert "evidence_content_hash_ix ON evidence (content_hash)" in joined


def test_confidence_columns_are_bounded() -> None:
    """Confidence is a bounded [0,1] quantity; an out-of-range write must be rejected."""
    for table in ("entity", "evidence", "event", "relationship", "causal_link", "claim",
                  "intelligence_signal", "source_registry"):
        text = _ddl_text(table)
        bounded = (
            "CONFIDENCE >= 0.0 AND CONFIDENCE <= 1.0" in text
            or "RELIABILITY >= 0.0 AND RELIABILITY <= 1.0" in text
        )
        assert bounded, f"{table} does not bound its confidence/reliability column"


# -------------------------------------------------------------------------------------------
# Structural guarantees (§23, §24, §26)
# -------------------------------------------------------------------------------------------


def test_event_table_is_partitioned() -> None:
    statement = next(s for s in S.TABLES_DDL if "CREATE TABLE IF NOT EXISTS event " in s)
    assert "PARTITION BY RANGE (occurred_at)" in statement


def test_idempotency_is_not_on_the_partitioned_table() -> None:
    """
    A unique constraint on a partitioned table must include the partition key, so event idempotency
    deliberately lives in a separate unpartitioned table. If someone "simplifies" this by adding
    UNIQUE to ``event.idempotency_key``, PostgreSQL rejects the DDL at deploy time — this test fails
    first, with a readable reason.
    """
    assert "idempotency_key" in _ddl_columns("event")
    dedup = _ddl_text("event_dedup")
    assert "IDEMPOTENCY_KEY" in dedup and "PRIMARY KEY" in dedup, (
        "event_dedup must constrain the idempotency key (it is the global dedup point)"
    )
    assert "ON event (idempotency_key)" not in "\n".join(S.INDEXES), (
        "idempotency must not be indexed on the partitioned table"
    )


def test_partitions_cover_a_forward_window() -> None:
    statements = S.partition_ddl(3)
    assert len(statements) == 3
    assert all("PARTITION OF event" in s for s in statements)
    with pytest.raises(ValueError):
        S.partition_ddl(0)
    # Determinism: the same anchor must produce identical boundaries.
    anchor = datetime(2026, 3, 17, 9, 30, tzinfo=timezone.utc)
    assert S.partition_ddl(2, now=anchor) == S.partition_ddl(2, now=anchor)


def test_partition_month_arithmetic_rolls_over_the_year() -> None:
    """Month arithmetic must not depend on dateutil, and must handle year boundaries."""
    from vexer_platform.persistence.schema import _add_months

    march = datetime(2026, 3, 1)
    assert _add_months(march, 10) == datetime(2027, 1, 1)
    assert _add_months(datetime(2026, 12, 1), 1) == datetime(2027, 1, 1)
    assert _add_months(datetime(2026, 1, 1), -1) == datetime(2025, 12, 1)


def test_iter_ddl_ordering_is_dependency_correct() -> None:
    """Enums before tables, tables before indexes, indexes before partitions."""
    ddl = S.iter_ddl(months=2)
    enum_at = next(i for i, s in enumerate(ddl) if "CREATE TYPE" in s)
    entity_at = next(i for i, s in enumerate(ddl) if "CREATE TABLE IF NOT EXISTS entity " in s)
    index_at = next(i for i, s in enumerate(ddl) if "entity_name_lower_ix" in s)
    partition_at = next(i for i, s in enumerate(ddl) if "PARTITION OF event" in s)
    assert enum_at < entity_at < index_at < partition_at


def test_every_access_path_has_an_index() -> None:
    """§24: the documented query paths must be index-backed, not full scans."""
    joined = "\n".join(S.INDEXES)
    for fragment in (
        "entity (lower(name))",
        "event (occurred_at DESC)",
        "event USING GIN (entities)",
        "evidence USING GIN (supports)",
        "relationship (source_entity, valid_from DESC)",
        "relationship (target_entity, valid_from DESC)",
        "provenance_edge (child_id)",
    ):
        assert fragment in joined, f"missing index for access path: {fragment}"


def test_temporal_live_lookups_use_partial_indexes() -> None:
    joined = "\n".join(S.INDEXES)
    assert joined.count("WHERE valid_until IS NULL") >= 2, (
        "the 'currently valid' access path must be a partial index, not a filtered scan"
    )


def test_audit_log_is_append_only_and_time_ordered() -> None:
    """§45: BRIN on the append-only, physically ordered log; hash chain columns present."""
    joined = "\n".join(S.INDEXES)
    assert "audit_occurred_brin_ix ON audit_log USING BRIN (occurred_at)" in joined
    columns = _ddl_columns("audit_log")
    assert {"prev_hash", "entry_hash", "actor", "action", "authorization"} <= columns


def test_ddl_statements_are_terminated() -> None:
    """Each statement must be a complete, terminable unit for the migration runner."""
    for statement in S.DDL:
        body = statement.strip()
        assert body.endswith(";"), f"unterminated statement: {body[:60]}"


def test_no_credential_material_is_embedded_in_the_schema() -> None:
    """§29: the DDL must not hardcode users, passwords or hosts."""
    joined = "\n".join(S.DDL).lower()
    for forbidden in ("password", "secret", "postgresql://", "api_key", "token"):
        assert forbidden not in joined, f"schema DDL contains {forbidden!r}"

