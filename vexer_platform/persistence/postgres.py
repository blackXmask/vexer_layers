"""
Real PostgreSQL persistence for the Vexer platform (mandate §21, §22, §23, §24, §26, §45).

**This is not a mock.** It talks to a real PostgreSQL server through ``asyncpg``. Where no server is
reachable, :meth:`PostgresStore.health` reports ``UNAVAILABLE`` and every method raises a structured
``DEPENDENCY_UNAVAILABLE`` error — it never falls back to an in-process fake (§1 RULE 1, §19, §52).

Design points that matter at production scale:

* **Bounded pool, explicit timeouts** (§26, §34, §52). ``asyncpg`` is configured with a finite
  ``min_size``/``max_size`` and a ``command_timeout``; unbounded connection growth is the classic
  way a service takes down its own database.
* **Batched writes** (§26). Rows are written with ``executemany`` inside one transaction rather than
  a round trip per object; a crawler resolving ten thousand entities must not make ten thousand
  commits.
* **Global event idempotency** (§21). :meth:`record_event` claims the ``event_dedup`` key first
  inside the same transaction as the event insert, so a redelivered message cannot create duplicate
  state and two concurrent producers cannot both win. A replay returns the original ``event_id`` and
  reports ``is_new=False`` rather than raising, so at-least-once delivery is safe.
* **Temporal reads** (§15). ``valid_until IS NULL`` and as-of queries are index-supported, not
  filtered in Python; the difference is a scan versus an index lookup at a few million rows.
* **Streaming** (§26). ``iter_events`` is an ``async`` generator using a server-side cursor, so a
  full-corpus export never materialises in memory.
* **Tamper-evident audit** (§45). :meth:`append_audit` extends a hash chain; :meth:`verify_audit_chain`
  detects removal or edits.
"""
import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

import asyncpg

from .. import contracts as C
from ..config import require_env
from ..errors import ErrorCode, VexerError
from . import rows as R
from . import schema as S

__all__ = ["DataHealth", "PostgresStore", "StoreSettings"]

ENV_DSN = "VEXER_POSTGRES_DSN"

#: Retryable asyncpg SQLSTATEs (§34). A unique-violation on our own dedup keys is *not* an error —
#: it is the idempotency mechanism working — so it is handled explicitly in :meth:`record_event`
#: rather than being retried here.
_UNIQUE_VIOLATION = "23505"
_SERIALIZATION_FAILURE = "40001"
_DEADLOCK_DETECTED = "40P01"


class DataHealth(str, Enum):
    """
    Explicit data-freshness/health states (§19). A degraded read path must never be
    indistinguishable from a live one.

    ``LIVE``       real datastore, current data
    ``DEGRADED``   reachable but impaired (slow, partial, replication lag)
    ``STALE``      serving data older than the freshness budget
    ``FALLBACK``   a documented alternate path produced the result (e.g. recursive CTE, not AGE)
    ``FAILED``     last operation failed; state is unknown
    ``UNAVAILABLE`` cannot be reached at all
    """

    LIVE = "LIVE"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    FALLBACK = "FALLBACK"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class StoreSettings:
    """
    Connection settings, sourced from the environment only (§29, §32).

    ``dsn`` is read via ``require_env`` so a missing value is a loud, structured failure instead of
    a connection attempt to localhost with default credentials.
    """

    dsn: str
    min_size: int = 1
    max_size: int = 10
    command_timeout_s: float = 30.0
    statement_cache_size: int = 100

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "StoreSettings":
        source = env if env is not None else {}

        def _int(name: str, default: int) -> int:
            raw = source.get(name)
            if raw is None or raw == "":
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise VexerError(
                    ErrorCode.VALIDATION_FAILED,
                    f"{name} must be an integer",
                    details={"variable": name},
                    cause=exc,
                ) from exc

        dsn = source.get(ENV_DSN) or require_env(ENV_DSN)
        settings = cls(
            dsn=dsn,
            min_size=_int("VEXER_PG_MIN_SIZE", 1),
            max_size=_int("VEXER_PG_MAX_SIZE", 10),
        )
        if settings.min_size < 1:
            raise VexerError(ErrorCode.VALIDATION_FAILED, "VEXER_PG_MIN_SIZE must be >= 1")
        if settings.max_size < settings.min_size:
            raise VexerError(
                ErrorCode.VALIDATION_FAILED, "VEXER_PG_MAX_SIZE must be >= VEXER_PG_MIN_SIZE"
            )
        return settings

    def describe(self) -> Dict[str, Any]:
        """Redacted description for health output — the DSN must never be echoed (§29)."""
        return {
            "min_size": self.min_size,
            "max_size": self.max_size,
            "command_timeout_s": self.command_timeout_s,
            "dsn_configured": bool(self.dsn),
        }


class PostgresStore:
    """
    Async data-access layer over a real PostgreSQL server.

    Usage::

        store = PostgresStore(StoreSettings.from_env())
        await store.connect()
        await store.record_event(event)      # idempotent
        await store.close()

    The store is a thin, explicit repository: it owns transactions, batching, idempotency and
    temporal query shapes, and it returns contracts — never raw dicts — so the rest of the platform
    never depends on the storage engine's row shape (§53: one coherent system, real infrastructure).
    """

    def __init__(self, settings: StoreSettings) -> None:
        self._settings = settings
        self._pool: Optional[asyncpg.Pool] = None
        self._health = DataHealth.UNAVAILABLE
        self._last_error: Optional[str] = None

    # -- lifecycle ---------------------------------------------------------------------------

    @property
    def health(self) -> DataHealth:
        """Last observed health. ``UNAVAILABLE`` until a successful :meth:`connect`."""
        return self._health

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    async def connect(self) -> None:
        """Open the pool. Raises ``DEPENDENCY_UNAVAILABLE`` if the server is unreachable."""
        if self._pool is not None:
            return
        try:
            self._pool = await asyncpg.create_pool(
                dsn=self._settings.dsn,
                min_size=self._settings.min_size,
                max_size=self._settings.max_size,
                command_timeout=self._settings.command_timeout_s,
                statement_cache_size=self._settings.statement_cache_size,
            )
        except (OSError, asyncpg.PostgresError) as exc:
            self._health = DataHealth.UNAVAILABLE
            self._last_error = type(exc).__name__
            raise VexerError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "cannot connect to PostgreSQL",
                details={"operation": "connect", "driver_error": type(exc).__name__},
                cause=exc,
            ) from exc
        self._health = DataHealth.LIVE
        self._last_error = None

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        self._health = DataHealth.UNAVAILABLE

    async def __aenter__(self) -> "PostgresStore":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise VexerError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "PostgresStore.connect() has not been awaited",
                details={"operation": "pool"},
            )
        return self._pool

    async def ping(self) -> DataHealth:
        """Liveness probe for the datastore (§28, §31). Never raises; reports via ``health``."""
        try:
            pool = self._require_pool()
        except VexerError:
            return self._health
        try:
            await pool.fetchval("SELECT 1")
            self._health = DataHealth.LIVE
        except (OSError, asyncpg.PostgresError, asyncio.TimeoutError) as exc:
            self._health = DataHealth.FAILED
            self._last_error = type(exc).__name__
        return self._health

    async def apply_schema(self, *, months: int = 24) -> None:
        """
        Create enums, tables, indexes and event partitions in dependency order.

        Normally invoked through Alembic (``alembic upgrade head``) so schema changes are versioned
        and auditable. This direct path exists for ephemeral integration environments where running
        a migration process per test is wasteful, and it executes the *same* statements — there is no
        second definition of the schema.
        """
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                for statement in S.iter_ddl(months=months):
                    await conn.execute(statement)
        self._health = DataHealth.LIVE

    # -- error translation -------------------------------------------------------------------

    def _wrap(self, exc: BaseException, operation: str) -> VexerError:
        """Translate a driver error into the platform catalogue; never leak a raw driver error."""
        sqlstate = getattr(exc, "sqlstate", None) or ""
        if sqlstate in (_SERIALIZATION_FAILURE, _DEADLOCK_DETECTED):
            code = ErrorCode.CONFLICT  # retryable per the catalogue
        elif sqlstate == _UNIQUE_VIOLATION:
            code = ErrorCode.DATA_INTEGRITY
        elif isinstance(exc, (OSError, asyncpg.PostgresConnectionError)):
            code = ErrorCode.DEPENDENCY_UNAVAILABLE
            self._health = DataHealth.FAILED
        elif isinstance(exc, asyncpg.QueryCanceledError):
            code = ErrorCode.DEPENDENCY_TIMEOUT
        else:
            code = ErrorCode.INTERNAL
        self._last_error = type(exc).__name__
        return VexerError(
            code,
            f"postgres operation failed: {operation}",
            details={"operation": operation, "sqlstate": sqlstate or None},
            cause=exc,
        )


    # -- writes -------------------------------------------------------------------------------

    async def upsert_entities(self, entities: Sequence[C.Entity], *, batch_size: int = 500) -> int:
        """
        Upsert entities, guarding against out-of-order arrival.

        ``WHERE entity.updated_at <= EXCLUDED.updated_at`` means a delayed or replayed message cannot
        regress a record that newer knowledge already improved. Batched (§26): one transaction per
        batch rather than one per entity, because a crawler resolving thousands of entities must not
        make thousands of commits.
        """
        if not entities:
            return 0
        if batch_size < 1:
            raise VexerError(ErrorCode.VALIDATION_FAILED, "batch_size must be >= 1")
        spec = R.SPECS["Entity"]
        statement = (
            f"INSERT INTO {spec.table} ({', '.join(spec.column_names)}) "
            f"VALUES ({R._placeholders(spec)}) "
            "ON CONFLICT (entity_id) DO UPDATE SET "
            "name = EXCLUDED.name, aliases = EXCLUDED.aliases, attributes = EXCLUDED.attributes, "
            "source_ids = EXCLUDED.source_ids, confidence = EXCLUDED.confidence, "
            "updated_at = EXCLUDED.updated_at, valid_until = EXCLUDED.valid_until, "
            "schema_version = EXCLUDED.schema_version "
            "WHERE entity.updated_at <= EXCLUDED.updated_at"
        )
        pool = self._require_pool()
        written = 0
        for start in range(0, len(entities), batch_size):
            chunk = entities[start : start + batch_size]
            rows = [R.to_params(entity, spec) for entity in chunk]
            try:
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.executemany(statement, rows)
            except (OSError, asyncpg.PostgresError) as exc:
                raise self._wrap(exc, "upsert_entities") from exc
            written += len(chunk)
        return written

    async def upsert_evidence(self, evidence: Sequence[C.Evidence]) -> int:
        """
        Upsert evidence by ``evidence_id``.

        Evidence is treated as immutable knowledge: ``reliability`` and the ``supports`` /
        ``contradicts`` sets are **not** overwritten on conflict, because a later observation must not
        silently rewrite what an earlier document attested. Corrections arrive as new evidence that
        contradicts the earlier claim (§14) — preserving contradictions is the design, not an
        oversight.
        """
        if not evidence:
            return 0
        spec = R.SPECS["Evidence"]
        statement = R.upsert_statement(
            spec,
            conflict=("evidence_id",),
            updates=("attributes", "confidence", "provenance_class", "schema_version"),
        )
        pool = self._require_pool()
        rows = [R.to_params(item, spec) for item in evidence]
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.executemany(statement, rows)
        except (OSError, asyncpg.PostgresError) as exc:
            raise self._wrap(exc, "upsert_evidence") from exc
        return len(rows)


    async def record_event(self, event: C.Event) -> "EventWriteResult":
        """
        Persist one event idempotently (§21).

        Order of operations, all inside one transaction:

        1. ``INSERT INTO event_dedup ... ON CONFLICT DO NOTHING`` — the key row is the serialization
           point. A conflict means this logical event is already stored, so the original
           ``event_id`` is returned and nothing else happens. At-least-once delivery is therefore
           safe and a replay is not an error.
        2. Otherwise insert into ``event`` and report ``is_new=True``.

        Claim and insert share a transaction, so a crash between them rolls back both: a partially
        recorded event is impossible and the retry is clean. A missing partition surfaces as a
        structured ``DATA_INTEGRITY``/``INTERNAL`` error naming the operation, not as a silent drop.
        """
        C.assert_schema_supported(event.schema_version, as_of="event_ingest")
        spec = R.SPECS["Event"]
        pool = self._require_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    if event.idempotency_key:
                        claimed = await conn.fetchrow(
                            """
                            INSERT INTO event_dedup (idempotency_key, event_id, occurred_at,
                                                      payload_hash)
                            VALUES ($1, $2, $3, $4)
                            ON CONFLICT (idempotency_key) DO NOTHING
                            RETURNING event_id
                            """,
                            event.idempotency_key,
                            event.event_id,
                            event.timestamp,
                            _payload_hash(event),
                        )
                        if claimed is None:
                            existing = await conn.fetchval(
                                "SELECT event_id FROM event_dedup WHERE idempotency_key = $1",
                                event.idempotency_key,
                            )
                            return EventWriteResult(
                                event_id=str(existing), is_new=False, deduplicated=True
                            )
                    await conn.execute(
                        f"INSERT INTO {spec.table} ({', '.join(spec.column_names)}) "
                        f"VALUES ({R._placeholders(spec)})",
                        *R.to_params(event, spec),
                    )
            return EventWriteResult(event_id=event.event_id, is_new=True, deduplicated=False)
        except (OSError, asyncpg.PostgresError) as exc:
            raise self._wrap(exc, "record_event") from exc

    async def add_relationship(self, relationship: C.Relationship) -> str:
        """
        Insert a relationship edge and close the superseded open edge for the same pair (§17).

        History is preserved rather than overwritten: the previous row keeps its content, its
        ``valid_until`` is set to the new edge's ``valid_from``, and the ``supersedes`` pointer
        records the lineage. Closing is scoped to the same ``(source, target, type)`` triple — a
        different relationship type between the same entities is a different fact and stays open.
        Returns the superseded relationship id, or ``""`` when there was none.
        """
        spec = R.SPECS["Relationship"]
        pool = self._require_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    if relationship.supersedes is None:
                        prior = await conn.fetchval(
                            """
                            UPDATE relationship SET valid_until = $4
                            WHERE source_entity = $1 AND target_entity = $2
                              AND relationship_type = $3
                              AND valid_until IS NULL AND valid_from < $4
                            RETURNING relationship_id
                            """,
                            relationship.source_entity,
                            relationship.target_entity,
                            relationship.relationship_type.value,
                            relationship.valid_from,
                        )
                    else:
                        prior = relationship.supersedes
                    await conn.execute(
                        f"INSERT INTO {spec.table} ({', '.join(spec.column_names)}) "
                        f"VALUES ({R._placeholders(spec)})",
                        *R.to_params(relationship, spec),
                    )
            return str(prior) if prior else ""
        except (OSError, asyncpg.PostgresError) as exc:
            raise self._wrap(exc, "add_relationship") from exc


    # -- reads --------------------------------------------------------------------------------

    async def get_entity(self, entity_id: str) -> Optional[C.Entity]:
        spec = R.SPECS["Entity"]
        pool = self._require_pool()
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT {R.select_columns(spec)} FROM {spec.table} WHERE entity_id = $1",
                    entity_id,
                )
        except (OSError, asyncpg.PostgresError) as exc:
            raise self._wrap(exc, "get_entity") from exc
        return R.from_row(C.Entity, dict(row), spec) if row else None

    async def find_entities_by_name(
        self, name: str, *, as_of: Optional[datetime] = None
    ) -> List[C.Entity]:
        """
        Resolve entities by case-insensitive name or alias (§24 entity lookup path).

        With ``as_of`` this becomes a temporal lookup — "which of these existed and were valid at T"
        — served by the window index instead of filtered in Python.
        """
        spec = R.SPECS["Entity"]
        pool = self._require_pool()
        pattern = f"%{name.lower()}%"
        sql = (
            f"SELECT {R.select_columns(spec)} FROM {spec.table} "
            "WHERE (lower(name) = $1 OR lower(name) LIKE $2 "
            "       OR EXISTS (SELECT 1 FROM unnest(aliases) AS a WHERE lower(a) LIKE $2))"
        )
        args: List[Any] = [name.lower(), pattern]
        if as_of is None:
            sql += " AND valid_until IS NULL"
        else:
            # Half-open window: valid_from <= T < valid_until (§15 boundary semantics).
            sql += " AND valid_from <= $3 AND (valid_until IS NULL OR valid_until > $3)"
            args.append(as_of)
        sql += " ORDER BY confidence DESC, updated_at DESC LIMIT 200"
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(sql, *args)
        except (OSError, asyncpg.PostgresError) as exc:
            raise self._wrap(exc, "find_entities_by_name") from exc
        return [R.from_row(C.Entity, dict(row), spec) for row in rows]

    async def active_relationships(
        self, entity_id: str, *, direction: str = "out"
    ) -> List[C.Relationship]:
        """
        Currently-valid edges touching ``entity_id`` (§15/§16), served by the live partial indexes.
        ``direction`` is ``"out"``, ``"in"`` or ``"both"``.
        """
        if direction not in ("out", "in", "both"):
            raise VexerError(
                ErrorCode.VALIDATION_FAILED, "direction must be one of 'out', 'in', 'both'"
            )
        spec = R.SPECS["Relationship"]
        if direction == "out":
            predicate, args = "source_entity = $1", [entity_id]
        elif direction == "in":
            predicate, args = "target_entity = $1", [entity_id]
        else:
            predicate, args = "(source_entity = $1 OR target_entity = $1)", [entity_id]
        pool = self._require_pool()
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {R.select_columns(spec)} FROM {spec.table} "
                    f"WHERE {predicate} AND valid_until IS NULL "
                    "ORDER BY valid_from DESC LIMIT 1000",
                    *args,
                )
        except (OSError, asyncpg.PostgresError) as exc:
            raise self._wrap(exc, "active_relationships") from exc
        return [R.from_row(C.Relationship, dict(row), spec) for row in rows]


    async def iter_events(
        self,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        event_type: Optional[str] = None,
        batch_size: int = 1000,
    ) -> AsyncIterator[C.Event]:
        """
        Stream events in time order using a **server-side cursor** (§26).

        A full-corpus export or a re-correlation pass must not materialise the result set; a
        client-side cursor would transfer every row into the driver first, which is precisely the
        unbounded-growth failure this avoids.
        """
        spec = R.SPECS["Event"]
        if batch_size < 1:
            raise VexerError(ErrorCode.VALIDATION_FAILED, "batch_size must be >= 1")
        clauses: List[str] = []
        args: List[Any] = []
        if since is not None:
            args.append(since)
            clauses.append(f"occurred_at >= ${len(args)}")
        if until is not None:
            args.append(until)
            clauses.append(f"occurred_at < ${len(args)}")
        if event_type is not None:
            args.append(event_type)
            clauses.append(f"event_type = ${len(args)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT {R.select_columns(spec)} FROM {spec.table} {where} "
            "ORDER BY occurred_at, event_id"
        )
        pool = self._require_pool()
        try:
            async with pool.acquire() as conn:
                # read-only transaction: the snapshot is consistent for the whole stream, and the
                # cursor is released deterministically when the generator closes.
                async with conn.transaction(readonly=True):
                    async for row in conn.cursor(sql, *args, prefetch=batch_size):
                        yield R.from_row(C.Event, dict(row), spec)
        except (OSError, asyncpg.PostgresError) as exc:
            raise self._wrap(exc, "iter_events") from exc

    async def lineage(
        self, provenance_id: str, *, direction: str = "upstream", max_depth: int = 20
    ) -> List[str]:
        """
        Walk the provenance DAG (§12) with an index-assisted recursive CTE.

        This is the query that answers *where did this conclusion come from?* Depth is bounded and
        the traversal materialises each ``(node, depth)`` once, so a cyclic or heavily fan-out
        lineage graph cannot produce an infinite walk or an unbounded result.

        This CTE is also the documented fallback when Apache AGE is absent;
        :meth:`graph_backend` makes that substitution visible (§19) rather than silent.
        """
        if max_depth < 1:
            raise VexerError(ErrorCode.VALIDATION_FAILED, "max_depth must be >= 1")
        if direction not in ("upstream", "downstream"):
            raise VexerError(
                ErrorCode.VALIDATION_FAILED, "direction must be 'upstream' or 'downstream'"
            )
        edge_from, edge_to = (
            ("child_id", "parent_id") if direction == "upstream" else ("parent_id", "child_id")
        )
        sql = f"""
            WITH RECURSIVE walk (node, depth) AS (
                SELECT $1::text, 0
                UNION
                SELECT e.{edge_to}, w.depth + 1
                  FROM walk w
                  JOIN provenance_edge e ON e.{edge_from} = w.node
                 WHERE w.depth < $2
            )
            SELECT node FROM walk WHERE depth > 0 ORDER BY depth, node
        """
        pool = self._require_pool()
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(sql, provenance_id, max_depth)
        except (OSError, asyncpg.PostgresError) as exc:
            raise self._wrap(exc, "lineage") from exc
        return [row["node"] for row in rows]

    async def graph_backend(self) -> DataHealth:
        """
        Report the graph engine actually in use (§19 — no silent fallbacks).

        ``LIVE`` when Apache AGE is installed, otherwise ``FALLBACK`` to signal the recursive-CTE
        path. Behaviour is equivalent; the difference is visible instead of implied.
        """
        pool = self._require_pool()
        try:
            async with pool.acquire() as conn:
                installed = await conn.fetchval("SELECT 1 FROM pg_extension WHERE extname = 'age'")
        except (OSError, asyncpg.PostgresError) as exc:
            raise self._wrap(exc, "graph_backend") from exc
        return DataHealth.LIVE if installed else DataHealth.FALLBACK


    # -- audit (§45) ---------------------------------------------------------------------------

    async def append_audit(
        self,
        *,
        actor: str,
        action: str,
        resource: Optional[str] = None,
        decision: Optional[str] = None,
        authorization: Optional[str] = None,
        result: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> str:
        """
        Append one entry to the tamper-evident audit log and return its hash.

        The chain is ``entry_hash = sha256(prev_hash || canonical_json(payload))``. ``SELECT ... FOR
        UPDATE`` on the tail serialises concurrent appends, so two writers cannot read the same
        ``prev_hash`` and fork the chain. This class exposes no update or delete path for the table
        at all — the log is insert-only by construction (§45).
        """
        if not actor or not actor.strip():
            raise VexerError(ErrorCode.VALIDATION_FAILED, "audit actor must be non-empty")
        if not action or not action.strip():
            raise VexerError(ErrorCode.VALIDATION_FAILED, "audit action must be non-empty")
        payload = {
            "action": action,
            "actor": actor,
            "authorization": authorization,
            "correlation_id": correlation_id,
            "decision": decision,
            "detail": detail or {},
            "request_id": request_id,
            "resource": resource,
            "result": result,
            "trace_id": trace_id,
        }
        return await self._insert_audit_row(payload)

    async def _insert_audit_row(self, payload: Dict[str, Any]) -> str:
        occurred_at = datetime.now(timezone.utc)
        hashed = dict(payload)
        hashed["occurred_at"] = occurred_at.isoformat()
        body = _canonical(hashed)
        pool = self._require_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    prev = await conn.fetchval(
                        "SELECT entry_hash FROM audit_log ORDER BY audit_id DESC LIMIT 1 FOR UPDATE"
                    )
                    prev_hash = str(prev) if prev else ""
                    entry_hash = hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()
                    await conn.execute(
                        """
                        INSERT INTO audit_log (occurred_at, actor, action, resource, decision,
                                               authorization, result, request_id, correlation_id,
                                               trace_id, detail, prev_hash, entry_hash)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, $13)
                        """,
                        occurred_at,
                        payload["actor"],
                        payload["action"],
                        payload["resource"],
                        payload["decision"],
                        payload["authorization"],
                        payload["result"],
                        payload["request_id"],
                        payload["correlation_id"],
                        payload["trace_id"],
                        json.dumps(payload["detail"], sort_keys=True, default=str),
                        prev_hash,
                        entry_hash,
                    )
            return entry_hash
        except (OSError, asyncpg.PostgresError) as exc:
            raise self._wrap(exc, "append_audit") from exc


    async def verify_audit_chain(self, *, limit: Optional[int] = None) -> "AuditVerification":
        """
        Recompute the hash chain and report the first break (§45).

        Returns the verified prefix length and the offending ``audit_id``, if any. A gap in
        ``audit_id`` is reported as ``sequence_gap``: deleting a row wholesale still leaves the
        remaining rows hashing correctly, so id continuity is a necessary second check.
        """
        pool = self._require_pool()
        sql = (
            "SELECT audit_id, occurred_at, actor, action, resource, decision, authorization, "
            "result, request_id, correlation_id, trace_id, detail, prev_hash, entry_hash "
            "FROM audit_log ORDER BY audit_id ASC"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(sql)
        except (OSError, asyncpg.PostgresError) as exc:
            raise self._wrap(exc, "verify_audit_chain") from exc
        prev_hash = ""
        prev_id: Optional[int] = None
        for index, row in enumerate(rows):
            detail = row["detail"]
            if isinstance(detail, (str, bytes, bytearray)):
                detail = json.loads(detail)
            body = _canonical(
                {
                    "action": row["action"],
                    "actor": row["actor"],
                    "authorization": row["authorization"],
                    "correlation_id": row["correlation_id"],
                    "decision": row["decision"],
                    "detail": detail,
                    "occurred_at": row["occurred_at"].isoformat(),
                    "request_id": row["request_id"],
                    "resource": row["resource"],
                    "result": row["result"],
                    "trace_id": row["trace_id"],
                }
            )
            expected = hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()
            if str(row["entry_hash"]) != expected:
                return AuditVerification(
                    intact=False, checked=index, broken_at=int(row["audit_id"]),
                    reason="hash_mismatch",
                )
            if str(row["prev_hash"]) != prev_hash:
                return AuditVerification(
                    intact=False, checked=index, broken_at=int(row["audit_id"]), reason="chain_break"
                )
            if prev_id is not None and int(row["audit_id"]) != prev_id + 1:
                return AuditVerification(
                    intact=False, checked=index, broken_at=int(row["audit_id"]),
                    reason="sequence_gap",
                )
            prev_hash = str(row["entry_hash"])
            prev_id = int(row["audit_id"])
        return AuditVerification(intact=True, checked=len(rows), broken_at=None, reason=None)


# -- module-level helpers and result types -------------------------------------------------


def _canonical(payload: Dict[str, Any]) -> str:
    """
    Deterministic JSON for hashing: sorted keys, no insignificant whitespace.

    Hashing must not depend on dict ordering or on a serialiser's default spacing, otherwise a
    rewrite of the same logical entry would look like tampering and vice versa.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _payload_hash(instance: Any) -> str:
    """SHA-256 of an event's canonical JSON, stored alongside the dedup key for forensics."""
    return hashlib.sha256(_canonical(instance.to_json_dict()).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EventWriteResult:
    """Outcome of :meth:`PostgresStore.record_event` — a replay is reported, not raised."""

    event_id: str
    is_new: bool
    deduplicated: bool


@dataclass(frozen=True)
class AuditVerification:
    """Outcome of :meth:`PostgresStore.verify_audit_chain`."""

    intact: bool
    checked: int
    broken_at: Optional[int]
    reason: Optional[str]

