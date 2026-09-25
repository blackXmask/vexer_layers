"""
Persistence boundary for the Vexer platform (mandate §21, §23, §26).

This package owns the mapping from the versioned domain contracts to a real PostgreSQL deployment:

* :mod:`~vexer_platform.persistence.schema` — canonical DDL (single source of truth for the
  migration and the parity test), indexes, and ``event`` partitioning.
* :mod:`~vexer_platform.persistence.rows` — declarative contract ↔ column mapping and the
  parameterised statement builders.
* :mod:`~vexer_platform.persistence.postgres` — the real ``asyncpg`` store: bounded pool, batched
  writes, global event idempotency, temporal reads, server-side streaming, tamper-evident audit.

**Nothing here is a fake datastore** (§1 RULE 1, §52). ``asyncpg`` and a reachable PostgreSQL 16+
server are runtime requirements; see ``docker-compose.yml`` and ``docs/data-model.md``. When the
server is unreachable the store reports :class:`~vexer_platform.persistence.postgres.DataHealth`
``UNAVAILABLE`` and raises a structured ``DEPENDENCY_UNAVAILABLE`` error — it never degrades to
in-process storage, because a silent fallback that looks like production data is the failure mode
this platform is built to prevent (§19).
"""
from .postgres import AuditVerification, DataHealth, EventWriteResult, PostgresStore, StoreSettings
from .rows import BY_MODEL, Column, TableSpec, from_row, spec_for, to_params
from .schema import DDL, ENUM_VALUES, SCHEMA_VERSION, TABLES, iter_ddl, partition_ddl

__all__ = [
    "BY_MODEL",
    "DDL",
    "ENUM_VALUES",
    "SCHEMA_VERSION",
    "TABLES",
    "AuditVerification",
    "Column",
    "DataHealth",
    "EventWriteResult",
    "PostgresStore",
    "StoreSettings",
    "TableSpec",
    "from_row",
    "iter_ddl",
    "partition_ddl",
    "spec_for",
    "to_params",
]
