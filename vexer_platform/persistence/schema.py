"""
Canonical PostgreSQL schema for the Vexer intelligence store (mandate §11, §15, §17, §21, §23, §24).

**Why this module is the single source of truth.** The Alembic revision executes
:func:`iter_ddl`; the contract-parity test asserts against the same statements. A schema that
drifted from the pydantic contracts would then be a test failure rather than a silent mismatch
between what the code sends and what the database accepts.

**Storage decisions and their reasons (§23/§24 — one engine per workload, no database by default).**

* PostgreSQL 16+ is the system of record. The workload is join- and history-heavy rather than
  write-only-streaming, so a document store would be the wrong engine.
* ``event`` is **time-partitioned** (monthly RANGE on ``occurred_at``). At millions of events per
  day, retention and index maintenance of a single heap become the bottleneck; partitioning turns
  retention into a detach instead of a mass ``DELETE``.
* A partitioned table cannot carry a unique constraint that omits the partition key, so global
  event **idempotency** (§21) cannot be an index on ``event``. It is a separate unpartitioned
  ``event_dedup`` table with a global unique key, written in the same transaction as the event —
  the standard resolution, and the reason dedup stays correct across partitions.
* Reference lists queried by containment (``evidence_ids @> ARRAY[...]``) are native ``text[]`` with
  GIN rather than JSONB: GIN on arrays is much smaller and faster for containment, and these are
  the hot path for evidence resolution and lineage walks.
* Free-form attributes stay ``jsonb`` + GIN. They are the only genuinely schemaless part of the
  model and must not get a column per new vendor field.
* "Currently valid" lookups (§15) are served by partial indexes on ``valid_until IS NULL``; the
  full-history query is served by a btree on the window. Without both, as-of queries scan.
* Vocabulary columns are native PG ``ENUM`` types: the sets are closed and validated by the
  contracts, so a typo in a hand-written query becomes a database error instead of a silent
  no-match. Adding a value is ``ALTER TYPE ... ADD VALUE`` (MINOR contract bump); renaming is
  MAJOR and needs a migration.
* All timestamps are ``timestamptz``. The contracts already reject naïve datetimes, so the DDL
  never has to guess a session timezone.
* ``audit_log`` is append-only and hash-chained (§45):
  ``entry_hash = sha256(prev_hash || canonical_payload)`` so deletion or edits inside the log are
  detectable.

**Deployment requirement (§1, RULE 1).** This schema targets a real PostgreSQL 16+ server. Nothing
here substitutes a fake database: where the schema is exercised, a real server is required (see
``docker-compose.yml`` and ``docs/data-model.md``).

Apache AGE (Apache-2.0) is optional and additive — graph traversal over the *same* tables with no
data duplication, for §16 correlation queries. It is created only when the extension is available;
a recursive-CTE fallback is documented, so the core system never hard-depends on it.
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

__all__ = [
    "DDL",
    "ENUM_VALUES",
    "INDEXES",
    "SCHEMA_VERSION",
    "TABLES",
    "TABLES_DDL",
    "enum_definitions",
    "iter_ddl",
    "partition_ddl",
]

#: Bumped when the physical schema changes. MAJOR = breaking layout change, MINOR = additive
#: column/table, mirroring the intent of ``vexer_platform.contracts.SCHEMA_VERSION``.
SCHEMA_VERSION = "1.0.0"

#: Closed vocabularies sourced from the pydantic contracts. Held as data so the parity test can
#: assert equality with the contract enum members without parsing SQL.
#: fmt: off
ENUM_VALUES: Dict[str, Tuple[str, ...]] = {
    "vx_evidence_type": (
        "DOCUMENT", "NEWS", "TENDER", "PATENT", "REGISTRY",
        "SOCIAL", "ANALYST", "API", "TELEMETRY", "HUMAN",
    ),
    "vx_relationship_type": (
        "CAUSES", "AFFECTS", "FOLLOWS", "PRECEDES", "CONTRADICTS",
        "CORRELATES_WITH", "INCREASES_RISK", "DECREASES_RISK",
        "CREATES_OPPORTUNITY", "MODIFIES", "DEPENDS_ON",
    ),
    "vx_provenance_class": ("OBSERVED", "DERIVED", "HYPOTHESIS", "PREDICTION", "UNKNOWN"),
    "vx_truth_status": ("SUPPORTED", "CONTESTED", "UNSUPPORTED", "UNKNOWN"),
    "vx_severity": ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"),
    "vx_signal_type": (
        "OPPORTUNITY", "RISK", "REGULATORY", "MARKET",
        "THREAT", "ANOMALY", "TREND", "CONTRADICTION",
    ),
}
#: fmt: on

#: Tables owned by the platform kernel. The parity test requires every contract-backed table to
#: appear here, so a newly added contract cannot be silently left unmigrated.
TABLES: Tuple[str, ...] = (
    "source_registry",
    "entity",
    "evidence",
    "event",
    "event_dedup",
    "relationship",
    "causal_link",
    "claim",
    "intelligence_signal",
    "provenance_record",
    "provenance_edge",
    "audit_log",
)


def enum_definitions() -> List[str]:
    """Idempotent ``CREATE TYPE`` statements, guarded by ``DO`` blocks."""
    statements: List[str] = []
    for name, values in ENUM_VALUES.items():
        literal = ", ".join(f"'{value}'" for value in values)
        statements.append(
            "DO $$ BEGIN IF NOT EXISTS ("
            "SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
            f"WHERE t.typname = '{name}') THEN "
            f"CREATE TYPE {name} AS ENUM ({literal}); "
            "END IF; END $$;"
        )
    return statements


#: Every contract-backed table. Split from the second chunk purely for edit size; the
#: concatenation below is what the migration and the parity test consume.
TABLES_DDL_CHUNK1: List[str] = [
    # -- source registry ---------------------------------------------------------------------
    # Evidence.reliability is a prior owned here, not invented per document (§13).
    """
CREATE TABLE IF NOT EXISTS source_registry (
    source_id        TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    source_type      vx_evidence_type NOT NULL,
    reliability      REAL NOT NULL DEFAULT 0.5 CHECK (reliability >= 0.0 AND reliability <= 1.0),
    uri              TEXT,
    authority        TEXT,
    licence          TEXT,
    retrieval_mode   TEXT NOT NULL DEFAULT 'api',
    is_active        BOOLEAN NOT NULL DEFAULT TRUE,
    attributes       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    schema_version   TEXT NOT NULL DEFAULT '1.0.0'
);
""",
    # -- entity ------------------------------------------------------------------------------
    """
CREATE TABLE IF NOT EXISTS entity (
    entity_id        TEXT PRIMARY KEY,
    entity_type      TEXT NOT NULL,
    name             TEXT NOT NULL,
    aliases          TEXT[] NOT NULL DEFAULT '{}',
    attributes       JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_ids       TEXT[] NOT NULL DEFAULT '{}',
    confidence       REAL NOT NULL DEFAULT 0.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_from       TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until      TIMESTAMPTZ,
    schema_version   TEXT NOT NULL DEFAULT '1.0.0',
    CONSTRAINT entity_window_ck CHECK (valid_until IS NULL OR valid_until > valid_from)
);
""",
    # -- evidence ---------------------------------------------------------------------------
    """
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id      TEXT PRIMARY KEY,
    source_id        TEXT NOT NULL,
    source_type      vx_evidence_type NOT NULL,
    uri              TEXT,
    content_hash     TEXT,
    evidence_type    vx_evidence_type NOT NULL DEFAULT 'DOCUMENT',
    reliability      REAL NOT NULL DEFAULT 0.5 CHECK (reliability >= 0.0 AND reliability <= 1.0),
    published_at     TIMESTAMPTZ,
    retrieved_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    supports         TEXT[] NOT NULL DEFAULT '{}',
    contradicts      TEXT[] NOT NULL DEFAULT '{}',
    confidence       REAL NOT NULL DEFAULT 0.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    provenance_class vx_provenance_class NOT NULL DEFAULT 'OBSERVED',
    attributes       JSONB NOT NULL DEFAULT '{}'::jsonb,
    schema_version   TEXT NOT NULL DEFAULT '1.0.0',
    CONSTRAINT evidence_hash_ck CHECK (content_hash IS NULL OR position(':' IN content_hash) > 0)
);
""",
    # -- event (time-partitioned) -----------------------------------------------------------
    """
CREATE TABLE IF NOT EXISTS event (
    event_id         TEXT NOT NULL,
    occurred_at      TIMESTAMPTZ NOT NULL,
    event_type       TEXT NOT NULL,
    entities         TEXT[] NOT NULL DEFAULT '{}',
    source_ids       TEXT[] NOT NULL DEFAULT '{}',
    evidence_ids     TEXT[] NOT NULL DEFAULT '{}',
    attributes       JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence       REAL NOT NULL DEFAULT 0.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    provenance_id    TEXT,
    provenance_class vx_provenance_class NOT NULL DEFAULT 'OBSERVED',
    geo              JSONB,
    idempotency_key  TEXT,
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    schema_version   TEXT NOT NULL DEFAULT '1.0.0',
    CONSTRAINT event_pkey PRIMARY KEY (event_id, occurred_at)
) PARTITION BY RANGE (occurred_at);
""",
    # -- event deduplication (§21) -----------------------------------------------------------
    # Unpartitioned so the uniqueness guarantee is global rather than per month. Written in the
    # same transaction as the event insert: the key row is the serialization point, so two
    # concurrent producers of the same logical event cannot both win.
    """
CREATE TABLE IF NOT EXISTS event_dedup (
    idempotency_key  TEXT PRIMARY KEY,
    event_id         TEXT NOT NULL,
    occurred_at      TIMESTAMPTZ NOT NULL,
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload_hash     TEXT
);
""",
]


#: Remaining contract-backed tables. Split only for edit size; :data:`TABLES_DDL` is the
#: concatenation and is what the migration consumes.
TABLES_DDL_CHUNK2: List[str] = [
    # -- relationship (temporal edge, history preserved) ------------------------------------
    """
CREATE TABLE IF NOT EXISTS relationship (
    relationship_id   TEXT PRIMARY KEY,
    source_entity     TEXT NOT NULL,
    target_entity     TEXT NOT NULL,
    relationship_type vx_relationship_type NOT NULL,
    confidence        REAL NOT NULL DEFAULT 0.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    evidence_ids      TEXT[] NOT NULL DEFAULT '{}',
    valid_from        TIMESTAMPTZ NOT NULL,
    valid_until       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    provenance_id     TEXT,
    provenance_class  vx_provenance_class NOT NULL DEFAULT 'OBSERVED',
    attributes        JSONB NOT NULL DEFAULT '{}'::jsonb,
    supersedes        TEXT,
    schema_version    TEXT NOT NULL DEFAULT '1.0.0',
    CONSTRAINT relationship_window_ck CHECK (valid_until IS NULL OR valid_until > valid_from),
    CONSTRAINT relationship_no_selfloop_ck CHECK (source_entity <> target_entity)
);
""",
    # -- causal link ------------------------------------------------------------------------
    """
CREATE TABLE IF NOT EXISTS causal_link (
    causal_id         TEXT PRIMARY KEY,
    cause             TEXT NOT NULL,
    effect            TEXT NOT NULL,
    relationship_type vx_relationship_type NOT NULL DEFAULT 'CAUSES',
    confidence        REAL NOT NULL DEFAULT 0.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    evidence_ids      TEXT[] NOT NULL DEFAULT '{}',
    occurred_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    mechanism         TEXT,
    provenance_id     TEXT,
    provenance_class  vx_provenance_class NOT NULL DEFAULT 'HYPOTHESIS',
    schema_version    TEXT NOT NULL DEFAULT '1.0.0',
    CONSTRAINT causal_no_selfloop_ck CHECK (cause <> effect)
);
""",
    # -- claim (§14: contradictions preserved, never overwritten) ---------------------------
    """
CREATE TABLE IF NOT EXISTS claim (
    claim_id         TEXT PRIMARY KEY,
    subject          TEXT NOT NULL,
    predicate        TEXT NOT NULL,
    object           TEXT NOT NULL,
    confidence       REAL NOT NULL DEFAULT 0.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    truth_status     vx_truth_status NOT NULL DEFAULT 'UNKNOWN',
    supported_by     TEXT[] NOT NULL DEFAULT '{}',
    contradicted_by  TEXT[] NOT NULL DEFAULT '{}',
    contradicts      TEXT[] NOT NULL DEFAULT '{}',
    valid_from       TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until      TIMESTAMPTZ,
    provenance_id    TEXT,
    provenance_class vx_provenance_class NOT NULL DEFAULT 'DERIVED',
    attributes       JSONB NOT NULL DEFAULT '{}'::jsonb,
    schema_version   TEXT NOT NULL DEFAULT '1.0.0',
    CONSTRAINT claim_window_ck CHECK (valid_until IS NULL OR valid_until > valid_from)
);
""",

    # -- intelligence signal (decision-grade output) ----------------------------------------
    """
CREATE TABLE IF NOT EXISTS intelligence_signal (
    signal_id             TEXT PRIMARY KEY,
    signal_type           vx_signal_type NOT NULL,
    headline              TEXT NOT NULL,
    detail                TEXT,
    entities              TEXT[] NOT NULL DEFAULT '{}',
    events                TEXT[] NOT NULL DEFAULT '{}',
    evidence              TEXT[] NOT NULL DEFAULT '{}',
    claims                TEXT[] NOT NULL DEFAULT '{}',
    confidence            REAL NOT NULL DEFAULT 0.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    confidence_components JSONB NOT NULL DEFAULT '{}'::jsonb,
    truth_status          vx_truth_status NOT NULL DEFAULT 'UNKNOWN',
    severity              vx_severity NOT NULL DEFAULT 'INFO',
    occurred_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    provenance_id         TEXT,
    provenance_class      vx_provenance_class NOT NULL DEFAULT 'DERIVED',
    schema_version        TEXT NOT NULL DEFAULT '1.0.0',
    -- §39: a signal claiming support must cite something. Mirrors the contract validator so the
    -- invariant also holds for writers that bypass pydantic (raw SQL, COPY, another service).
    CONSTRAINT signal_cites_evidence_ck CHECK (
        truth_status = 'UNKNOWN' OR cardinality(evidence) > 0 OR cardinality(claims) > 0
    )
);
""",
    # -- provenance lineage DAG (§12) -------------------------------------------------------
    """
CREATE TABLE IF NOT EXISTS provenance_record (
    provenance_id     TEXT PRIMARY KEY,
    stage             TEXT NOT NULL,
    source_ids        TEXT[] NOT NULL DEFAULT '{}',
    evidence_ids      TEXT[] NOT NULL DEFAULT '{}',
    derived_from      TEXT[] NOT NULL DEFAULT '{}',
    pipeline          TEXT NOT NULL DEFAULT 'vexer',
    pipeline_version  TEXT NOT NULL DEFAULT '0.0.0',
    model             TEXT,
    model_version     TEXT,
    transformation    TEXT NOT NULL DEFAULT 'passthrough',
    recorded_by       TEXT NOT NULL DEFAULT 'system',
    recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    provenance_class  vx_provenance_class NOT NULL DEFAULT 'OBSERVED',
    schema_version    TEXT NOT NULL DEFAULT '1.0.0'
);
""",
    # Edges get their own table so the lineage DAG can be walked with an index-assisted recursive
    # query; as an array on provenance_record it would force a sequential scan per hop.
    """
CREATE TABLE IF NOT EXISTS provenance_edge (
    parent_id       TEXT NOT NULL REFERENCES provenance_record (provenance_id) ON DELETE CASCADE,
    child_id        TEXT NOT NULL REFERENCES provenance_record (provenance_id) ON DELETE CASCADE,
    PRIMARY KEY (parent_id, child_id)
);
""",
    # -- audit log (append-only, hash-chained, §45) ----------------------------------------
    """
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    resource        TEXT,
    decision        TEXT,
    authorization   TEXT,
    result          TEXT,
    request_id      TEXT,
    correlation_id  TEXT,
    trace_id        TEXT,
    detail          JSONB NOT NULL DEFAULT '{}'::jsonb,
    prev_hash       TEXT NOT NULL DEFAULT '',
    entry_hash      TEXT NOT NULL,
    schema_version  TEXT NOT NULL DEFAULT '1.0.0'
);
""",
]

#: All table DDL in creation order.
TABLES_DDL = TABLES_DDL_CHUNK1 + TABLES_DDL_CHUNK2


#: Indexes are part of the schema, not an optimisation to add later: at production cardinality the
#: access paths in §24 are the difference between a lookup and a table scan.
INDEXES: List[str] = [
    # -- entity lookup paths (§24) ----------------------------------------------------------
    # Resolution is by name/alias far more often than by id (crawlers give names, not ids) and a
    # case-insensitive match is the default. Without a lower(name) index every resolution is a
    # sequential scan — the first thing that collapses at a few million entities.
    "CREATE INDEX IF NOT EXISTS entity_name_lower_ix ON entity (lower(name))",
    "CREATE INDEX IF NOT EXISTS entity_type_ix ON entity (entity_type)",
    "CREATE INDEX IF NOT EXISTS entity_aliases_gin ON entity USING GIN (aliases)",
    "CREATE INDEX IF NOT EXISTS entity_attributes_gin ON entity USING GIN (attributes jsonb_path_ops)",
    # "Currently valid" (§15): a partial index keeps the live set small and pre-sorted.
    """
CREATE INDEX IF NOT EXISTS entity_current_ix ON entity (entity_type, valid_from)
    WHERE valid_until IS NULL
""",
    "CREATE INDEX IF NOT EXISTS entity_window_ix ON entity (valid_from, valid_until)",
    # -- evidence ---------------------------------------------------------------------------
    # content_hash is the integrity/dedup key that survives link rot and mirrors, so it is
    # UNIQUE: two rows for the same bytes is a bug and the database is the right place to say so.
    """
CREATE UNIQUE INDEX IF NOT EXISTS evidence_content_hash_ix ON evidence (content_hash)
    WHERE content_hash IS NOT NULL
""",
    "CREATE INDEX IF NOT EXISTS evidence_source_ix ON evidence (source_id, published_at DESC NULLS LAST)",
    "CREATE INDEX IF NOT EXISTS evidence_supports_gin ON evidence USING GIN (supports)",
    "CREATE INDEX IF NOT EXISTS evidence_contradicts_gin ON evidence USING GIN (contradicts)",
    """
CREATE INDEX IF NOT EXISTS evidence_published_ix ON evidence (published_at DESC)
    WHERE published_at IS NOT NULL
""",
    # -- event ------------------------------------------------------------------------------
    # Time-first: the dominant access is "events in window W" and the partition key already prunes
    # by month, so the leading column must be occurred_at to remain useful.
    "CREATE INDEX IF NOT EXISTS event_occurred_ix ON event (occurred_at DESC)",
    "CREATE INDEX IF NOT EXISTS event_type_occurred_ix ON event (event_type, occurred_at DESC)",
    "CREATE INDEX IF NOT EXISTS event_entities_gin ON event USING GIN (entities)",
    "CREATE INDEX IF NOT EXISTS event_evidence_gin ON event USING GIN (evidence_ids)",
    "CREATE INDEX IF NOT EXISTS event_provenance_ix ON event (provenance_id)",
    # -- event_dedup: the global idempotency point (§21). The PK already covers key lookups.
    "CREATE INDEX IF NOT EXISTS event_dedup_event_ix ON event_dedup (event_id)",
    "CREATE INDEX IF NOT EXISTS event_dedup_seen_ix ON event_dedup (first_seen_at)",
    # -- relationship traversal (§16/§17) ----------------------------------------------------
    # Both directions get a live-partitioned index: traversal from either endpoint is common, and
    # a one-sided index would force a sequential scan for half the graph queries.
    """
CREATE INDEX IF NOT EXISTS relationship_src_live_ix ON relationship (source_entity, valid_from DESC)
    WHERE valid_until IS NULL
""",
    """
CREATE INDEX IF NOT EXISTS relationship_tgt_live_ix ON relationship (target_entity, valid_from DESC)
    WHERE valid_until IS NULL
""",
    "CREATE INDEX IF NOT EXISTS relationship_pair_ix ON relationship (source_entity, target_entity, relationship_type)",
    "CREATE INDEX IF NOT EXISTS relationship_supersedes_ix ON relationship (supersedes)",
    "CREATE INDEX IF NOT EXISTS relationship_evidence_gin ON relationship USING GIN (evidence_ids)",
    "CREATE INDEX IF NOT EXISTS relationship_recorded_ix ON relationship (recorded_at DESC)",
    # -- causal ------------------------------------------------------------------------------
    "CREATE INDEX IF NOT EXISTS causal_cause_ix ON causal_link (cause, occurred_at DESC)",
    "CREATE INDEX IF NOT EXISTS causal_effect_ix ON causal_link (effect, occurred_at DESC)",
    "CREATE INDEX IF NOT EXISTS causal_evidence_gin ON causal_link USING GIN (evidence_ids)",
    # -- claim (§14) ---------------------------------------------------------------------------
    "CREATE INDEX IF NOT EXISTS claim_subject_ix ON claim (subject, predicate)",
    "CREATE INDEX IF NOT EXISTS claim_truth_status_ix ON claim (truth_status)",
    "CREATE INDEX IF NOT EXISTS claim_contradicts_gin ON claim USING GIN (contradicts)",
    """
CREATE INDEX IF NOT EXISTS claim_contested_ix ON claim (valid_from DESC)
    WHERE truth_status = 'CONTESTED'
""",

    # -- signals ------------------------------------------------------------------------------
    "CREATE INDEX IF NOT EXISTS signal_type_recent_ix ON intelligence_signal (signal_type, occurred_at DESC)",
    """
CREATE INDEX IF NOT EXISTS signal_severity_ix ON intelligence_signal (severity, occurred_at DESC)
    WHERE severity IN ('HIGH', 'CRITICAL')
""",
    "CREATE INDEX IF NOT EXISTS signal_entities_gin ON intelligence_signal USING GIN (entities)",
    "CREATE INDEX IF NOT EXISTS signal_evidence_gin ON intelligence_signal USING GIN (evidence)",
    # -- provenance ---------------------------------------------------------------------------
    "CREATE INDEX IF NOT EXISTS provenance_stage_ix ON provenance_record (stage, recorded_at DESC)",
    "CREATE INDEX IF NOT EXISTS provenance_recorded_ix ON provenance_record (recorded_by, recorded_at DESC)",
    "CREATE INDEX IF NOT EXISTS provenance_derived_gin ON provenance_record USING GIN (derived_from)",
    "CREATE INDEX IF NOT EXISTS provenance_edge_child_ix ON provenance_edge (child_id)",
    # -- audit ---------------------------------------------------------------------------------
    "CREATE INDEX IF NOT EXISTS audit_actor_ix ON audit_log (actor, occurred_at DESC)",
    "CREATE INDEX IF NOT EXISTS audit_resource_ix ON audit_log (resource, occurred_at DESC)",
    "CREATE INDEX IF NOT EXISTS audit_correlation_ix ON audit_log (correlation_id)",
    "CREATE INDEX IF NOT EXISTS audit_request_ix ON audit_log (request_id)",
    # BRIN, not btree: the audit log is append-only and physically time-ordered, so a btree would
    # add write amplification and size for a range-scan-only access pattern.
    """
CREATE INDEX IF NOT EXISTS audit_occurred_brin_ix ON audit_log USING BRIN (occurred_at)
    WITH (pages_per_range = 64)
""",
]




def _add_months(moment: datetime, months: int) -> datetime:
    """Month arithmetic without ``dateutil`` (stdlib only, §2)."""
    total = moment.month - 1 + months
    return moment.replace(year=moment.year + total // 12, month=total % 12 + 1)


def partition_ddl(months: int = 24, *, now: Optional[datetime] = None) -> List[str]:
    """
    Monthly ``event`` partitions starting at the current month.

    The window carries future headroom so clock skew or a late-arriving event never hits a missing
    partition — otherwise that surfaces as an opaque runtime error on the ingest path (§34).

    Staying ahead of the horizon is an operational duty, not a one-off: a scheduled maintenance
    job re-runs this with a rolling window, and drops partitions behind the retention horizon
    (§44). Both are documented in ``docs/data-model.md``.
    """
    if months < 1:
        raise ValueError("months must be >= 1")
    base = now or datetime.now(timezone.utc)
    base = base.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    statements: List[str] = []
    for offset in range(months):
        start = _add_months(base, offset)
        end = _add_months(base, offset + 1)
        name = f"event_{start:%Y_%m}"
        statements.append(
            f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF event "
            f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}');"
        )
    return statements


def _terminated(statement: str) -> str:
    """
    Guarantee a statement ends with exactly one ``;``.

    Hand-written DDL drifts: a one-line ``CREATE INDEX`` written without a terminator sits happily
    next to a multi-line one that has it, and the inconsistency only surfaces when someone concatenates
    the statements into a script. Normalising here — at the single point every consumer goes through —
    keeps the invariant enforced for statements added later instead of relying on discipline.
    """
    body = statement.strip()
    return body if body.endswith(";") else body + ";"


def iter_ddl(*, months: int = 24, include_partitions: bool = True) -> List[str]:
    """
    Full ordered DDL: enums → tables → indexes → partitions.

    Order matters — enum types must exist before the tables that reference them, and indexes after
    their tables. The Alembic revision executes exactly this sequence, which is why the parity test
    can treat this list as the schema of record.
    """
    statements: List[str] = []
    statements.extend(enum_definitions())
    statements.extend(TABLES_DDL)
    statements.extend(INDEXES)
    if include_partitions:
        statements.extend(partition_ddl(months))
    return [_terminated(statement) for statement in statements]


#: Import-time DDL without partitions: partitions are time-relative and are generated at deploy
#: time by :func:`iter_ddl`. Tests and the contract-parity check use this constant.
DDL: List[str] = iter_ddl(include_partitions=False)
