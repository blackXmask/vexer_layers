"""
Contract ↔ column mapping (mandate §11, §21, §38).

This module is the *only* place that knows how a pydantic contract becomes a database row. Keeping
it declarative (a table of field/column/kind triples) buys three things that matter more than the
mapping itself:

1. **Drift becomes a test failure.** ``tests/test_kernel_persistence_schema.py`` asserts that every
   contract field has a column and that no column is unaccounted for, against the same DDL the
   migration executes. Adding a field to a contract without migrating it now fails CI instead of
   raising "unexpected keyword argument" in production.
2. **Round-trip tests need no database.** :func:`to_params` / :func:`from_row` are pure functions,
   so the mapping (including the ``timestamp`` → ``occurred_at`` rename) is verifiable on any
   machine, including one without PostgreSQL.
3. **Writers cannot bypass validation silently** (§38). The statement builders emit one
   parameterised statement per table — no string interpolation of values on the write path, so the
   write path is not an injection surface.

**Kinds** describe the wire encoding only:

=========  ==================================================================================
``text``   ``str``
``float``  ``float`` in ``[0, 1]`` (the contracts already bound these)
``ts``     timezone-aware ``datetime`` → ``timestamptz``
``json``   ``json.dumps`` → ``jsonb`` (read back with ``json.loads``)
``enum``   ``str``-Enum member value → native PG ``ENUM``
``arr``    ``list[str]`` → ``text[]`` (never JSONB: GIN containment, see ``schema``)
=========  ==================================================================================
"""
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Type

from pydantic import BaseModel

from .. import contracts as C

__all__ = [
    "BY_MODEL",
    "COLUMN_OVERRIDES",
    "Column",
    "SPECS",
    "TableSpec",
    "from_row",
    "insert_statement",
    "select_columns",
    "spec_for",
    "to_params",
    "upsert_statement",
]

#: Contract field → column rename, with the reason recorded. ``timestamp`` is a type keyword in
#: PostgreSQL and this column sits on the partitioned ``event`` table, so the physical name is
#: ``occurred_at`` while the contract keeps the clearer domain name.
COLUMN_OVERRIDES: Dict[str, str] = {"timestamp": "occurred_at"}


@dataclass(frozen=True)
class Column:
    """One persisted contract field."""

    field: str          # attribute name on the pydantic model
    name: str           # physical column name
    kind: str           # text | float | ts | json | enum | arr


@dataclass(frozen=True)
class TableSpec:
    """How one contract maps onto one table."""

    model: Type[BaseModel]
    table: str
    columns: Sequence[Column]
    #: Columns the database manages itself (``ingested_at``). Declared so the parity test can prove
    #: they are intentional rather than forgotten contract fields.
    db_managed: Sequence[str] = ()

    @property
    def column_names(self) -> List[str]:
        return [column.name for column in self.columns]

    @property
    def fields(self) -> List[str]:
        return [column.field for column in self.columns]


def _c(field: str, kind: str) -> Column:
    return Column(field=field, name=COLUMN_OVERRIDES.get(field, field), kind=kind)


_TAIL = (_c("schema_version", "text"),)

SPECS: Dict[str, TableSpec] = {
    "Entity": TableSpec(
        model=C.Entity,
        table="entity",
        columns=(
            _c("entity_id", "text"),
            _c("entity_type", "text"),
            _c("name", "text"),
            _c("aliases", "arr"),
            _c("attributes", "json"),
            _c("source_ids", "arr"),
            _c("confidence", "float"),
            _c("created_at", "ts"),
            _c("updated_at", "ts"),
            _c("valid_from", "ts"),
            _c("valid_until", "ts"),
            *_TAIL,
        ),
    ),
    "Evidence": TableSpec(
        model=C.Evidence,
        table="evidence",
        columns=(
            _c("evidence_id", "text"),
            _c("source_id", "text"),
            _c("source_type", "enum"),
            _c("uri", "text"),
            _c("content_hash", "text"),
            _c("evidence_type", "enum"),
            _c("reliability", "float"),
            _c("published_at", "ts"),
            _c("retrieved_at", "ts"),
            _c("supports", "arr"),
            _c("contradicts", "arr"),
            _c("confidence", "float"),
            _c("provenance_class", "enum"),
            _c("attributes", "json"),
            *_TAIL,
        ),
    ),
    "Event": TableSpec(
        model=C.Event,
        table="event",
        columns=(
            _c("event_id", "text"),
            _c("timestamp", "ts"),
            _c("event_type", "text"),
            _c("entities", "arr"),
            _c("source_ids", "arr"),
            _c("evidence_ids", "arr"),
            _c("attributes", "json"),
            _c("confidence", "float"),
            _c("provenance_id", "text"),
            _c("provenance_class", "enum"),
            _c("geo", "json"),
            _c("idempotency_key", "text"),
            *_TAIL,
        ),
        db_managed=("ingested_at",),
    ),
    "Relationship": TableSpec(
        model=C.Relationship,
        table="relationship",
        columns=(
            _c("relationship_id", "text"),
            _c("source_entity", "text"),
            _c("target_entity", "text"),
            _c("relationship_type", "enum"),
            _c("confidence", "float"),
            _c("evidence_ids", "arr"),
            _c("valid_from", "ts"),
            _c("valid_until", "ts"),
            _c("created_at", "ts"),
            _c("recorded_at", "ts"),
            _c("provenance_id", "text"),
            _c("provenance_class", "enum"),
            _c("attributes", "json"),
            _c("supersedes", "text"),
            *_TAIL,
        ),
    ),
    "CausalLink": TableSpec(
        model=C.CausalLink,
        table="causal_link",
        columns=(
            _c("causal_id", "text"),
            _c("cause", "text"),
            _c("effect", "text"),
            _c("relationship_type", "enum"),
            _c("confidence", "float"),
            _c("evidence_ids", "arr"),
            _c("timestamp", "ts"),
            _c("mechanism", "text"),
            _c("provenance_id", "text"),
            _c("provenance_class", "enum"),
            *_TAIL,
        ),
    ),
    "Claim": TableSpec(
        model=C.Claim,
        table="claim",
        columns=(
            _c("claim_id", "text"),
            _c("subject", "text"),
            _c("predicate", "text"),
            _c("object", "text"),
            _c("confidence", "float"),
            _c("truth_status", "enum"),
            _c("supported_by", "arr"),
            _c("contradicted_by", "arr"),
            _c("contradicts", "arr"),
            _c("valid_from", "ts"),
            _c("valid_until", "ts"),
            _c("provenance_id", "text"),
            _c("provenance_class", "enum"),
            _c("attributes", "json"),
            *_TAIL,
        ),
    ),
    "IntelligenceSignal": TableSpec(
        model=C.IntelligenceSignal,
        table="intelligence_signal",
        columns=(
            _c("signal_id", "text"),
            _c("signal_type", "enum"),
            _c("headline", "text"),
            _c("detail", "text"),
            _c("entities", "arr"),
            _c("events", "arr"),
            _c("evidence", "arr"),
            _c("claims", "arr"),
            _c("confidence", "float"),
            _c("confidence_components", "json"),
            _c("truth_status", "enum"),
            _c("severity", "enum"),
            _c("timestamp", "ts"),
            _c("provenance_id", "text"),
            _c("provenance_class", "enum"),
            *_TAIL,
        ),
    ),
    "ProvenanceRecord": TableSpec(
        model=C.ProvenanceRecord,
        table="provenance_record",
        columns=(
            _c("provenance_id", "text"),
            _c("stage", "text"),
            _c("source_ids", "arr"),
            _c("evidence_ids", "arr"),
            _c("derived_from", "arr"),
            _c("pipeline", "text"),
            _c("pipeline_version", "text"),
            _c("model", "text"),
            _c("model_version", "text"),
            _c("transformation", "text"),
            _c("recorded_by", "text"),
            _c("recorded_at", "ts"),
            _c("provenance_class", "enum"),
            *_TAIL,
        ),
    ),
}

#: Models that are persisted. ``Record`` itself is abstract and has no table.
BY_MODEL: Dict[Type[BaseModel], TableSpec] = {spec.model: spec for spec in SPECS.values()}


def spec_for(model: Any) -> TableSpec:
    """Resolve a :class:`TableSpec` from a model class or instance; fail loudly if unmapped."""
    cls = model if isinstance(model, type) else type(model)
    try:
        return BY_MODEL[cls]
    except KeyError as exc:
        raise KeyError(
            f"{cls.__name__} has no TableSpec: add one to vexer_platform.persistence.rows.SPECS "
            "and a matching table to schema.TABLES_DDL"
        ) from exc



def _encode(value: Any, kind: str) -> Any:
    """Contract value → wire value. ``None`` always passes through as SQL NULL."""
    if value is None:
        return None
    if kind == "json":
        # json.dumps rather than str(dict): a raw dict would reach asyncpg as an unhandled type
        # and fail at the driver level instead of at the boundary this module controls.
        return json.dumps(value, sort_keys=True, default=str)
    if kind == "enum":
        return value.value if isinstance(value, Enum) else str(value)
    if kind == "arr":
        return [str(item) for item in value]
    if kind == "ts":
        if not isinstance(value, datetime):
            raise TypeError(f"expected datetime for a ts column, got {type(value).__name__}")
        return value
    return value


def _decode(value: Any, kind: str) -> Any:
    """Wire value → contract value (the inverse of :func:`_encode`)."""
    if value is None:
        return None
    if kind == "json":
        # asyncpg returns jsonb as str by default; an already-parsed value is accepted too so a
        # future JSONB codec change does not break this mapping.
        return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value
    if kind == "arr":
        return list(value)
    return value


def to_params(instance: BaseModel, spec: Optional[TableSpec] = None) -> List[Any]:
    """Encode a contract instance into positional parameters ordered like ``spec.columns``."""
    resolved = spec or spec_for(instance)
    return [_encode(getattr(instance, column.field), column.kind) for column in resolved.columns]


def from_row(model: Type[BaseModel], row: Mapping[str, Any], spec: Optional[TableSpec] = None) -> Any:
    """
    Rebuild a contract instance from a database row.

    ``row`` only needs ``Mapping`` access (an asyncpg ``Record`` qualifies), so this works with the
    real driver and with a plain test fixture — no driver import needed.
    """
    resolved = spec or spec_for(model)
    kwargs: Dict[str, Any] = {}
    for column in resolved.columns:
        if column.name in row:
            kwargs[column.field] = _decode(row[column.name], column.kind)
        elif column.field in row:
            kwargs[column.field] = _decode(row[column.field], column.kind)
    return model.model_validate(kwargs)


def select_columns(spec: TableSpec) -> str:
    """``SELECT`` list, with the jsonb columns cast back to text so the mapping stays driver-agnostic.

    asyncpg has no default jsonb codec, so ``jsonb`` arrives as ``str``; selecting explicitly keeps
    that contract visible in the SQL instead of implied by driver configuration.
    """
    parts = []
    for column in spec.columns:
        if column.kind == "json":
            parts.append(f"{column.name}::text AS {column.name}")
        else:
            parts.append(column.name)
    return ", ".join(parts)


def _placeholders(spec: TableSpec) -> str:
    """``$n`` placeholders in column order, with explicit ``::jsonb`` casts."""
    return ", ".join(
        f"${index}" + ("::jsonb" if column.kind == "json" else "")
        for index, column in enumerate(spec.columns, start=1)
    )


def insert_statement(spec: TableSpec) -> str:
    """Plain ``INSERT`` for the mapped columns."""
    columns = ", ".join(spec.column_names)
    return f"INSERT INTO {spec.table} ({columns}) VALUES ({_placeholders(spec)})"


def upsert_statement(spec: TableSpec, conflict: Sequence[str], updates: Sequence[str]) -> str:
    """
    ``INSERT ... ON CONFLICT DO UPDATE`` for the mapped columns.

    The conflict target and updated columns are supplied by the caller, never inferred: which
    columns are authoritative on conflict is a domain decision (e.g. evidence is immutable while an
    entity may be enriched), and baking it in here would hide that choice.

    ``WHERE`` guards are added by callers that need them (see ``PostgresStore``), because
    last-write-wins is wrong for out-of-order arrivals — e.g. an entity row is only overwritten
    when the incoming ``updated_at`` is newer, so a delayed message cannot regress the record.
    """
    if not conflict:
        raise ValueError("upsert requires an explicit conflict target")
    target = ", ".join(conflict)
    assignments = ", ".join(f"{name} = EXCLUDED.{name}" for name in updates)
    return (
        f"INSERT INTO {spec.table} ({', '.join(spec.column_names)}) "
        f"VALUES ({_placeholders(spec)}) ON CONFLICT ({target}) DO UPDATE SET {assignments}"
    )

