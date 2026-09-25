# Data Model — Vexer Intelligence Store

> Mandate §11, §15, §17, §21, §23, §24, §44, §45. The schema of record is code, not this
> document: `vexer_platform/persistence/schema.py`. This file explains *why* it looks like this and
> what operations must maintain. If the two ever disagree, the code and its tests are right and
> this file is a bug.

## 1. What this store is

PostgreSQL 16+ is the system of record for entities, events, evidence, claims, relationships,
causal links, signals, provenance and the audit log. It is the only database implemented so far.
Qdrant, Kafka, Valkey, OpenSearch and ClickHouse appear in `docs/technology-selection.md` as
*selected but not yet integrated* — §23 forbids adding a database before its workload exists, and
each one lands in the increment that implements its real integration.

| Object | Table | Contract | Notes |
|---|---|---|---|
| Entity | `entity` | `contracts.Entity` | Bitemporal: `valid_from/valid_until` + `created_at/updated_at` |
| Event | `event` (partitioned) | `contracts.Event` | `timestamp` is stored as `occurred_at` |
| Evidence | `evidence` | `contracts.Evidence` | `content_hash` is UNIQUE — dedup survives link rot |
| Claim | `claim` | `contracts.Claim` | Contradictions preserved, never overwritten (§14) |
| Relationship | `relationship` | `contracts.Relationship` | Temporal edge; superseded rows keep history |
| Causal link | `causal_link` | `contracts.CausalLink` | Directional assertion with a stated mechanism |
| Signal | `intelligence_signal` | `contracts.IntelligenceSignal` | Decision-grade output; must cite evidence |
| Provenance | `provenance_record` + `provenance_edge` | `contracts.ProvenanceRecord` | Lineage DAG, index-walkable |
| Source registry | `source_registry` | — | Owns the `reliability` prior (§13) |
| Event dedup | `event_dedup` | — | Global idempotency point (§21) |
| Audit | `audit_log` | — | Append-only, hash-chained (§45) |

## 2. Five decisions that look odd without explanation

**1. `event` is time-partitioned, and idempotency lives in a separate table.**
A partitioned table cannot have a unique constraint that omits the partition key. Putting
`UNIQUE (idempotency_key)` on `event` would fail at deploy time, or — worse — silently only
deduplicate within a month. Dedup is therefore a small unpartitioned table whose key row is claimed
in the *same transaction* as the event insert. That key row is the serialization point: two
concurrent producers of the same logical event cannot both win, and a replay resolves to the
original `event_id` instead of raising.

**2. Reference lists are `text[]` + GIN, not `jsonb`.**
`evidence_ids @> ARRAY[...]` is the hot path for evidence resolution and lineage walks. GIN on
arrays is materially smaller and faster for containment than GIN on JSONB. `attributes` stays
`jsonb` because it is genuinely schemaless.

**3. Every vocabulary is a native PG `ENUM`.**
The value sets are closed and validated by the contracts, so a typo becomes a database error rather
than a silent no-match. Adding a value is `ALTER TYPE ... ADD VALUE` (a MINOR contract bump);
renaming is MAJOR and needs a migration.

**4. Contract invariants are duplicated as database constraints.**
No self-loops, half-open validity windows, bounded confidence, and "a non-`UNKNOWN` signal must
cite evidence" are all enforced in DDL, not only in pydantic. A writer that bypasses the service
layer — raw SQL, `COPY`, another team's service — cannot violate them.
`tests/test_kernel_persistence_integration.py` proves each one by inserting hand-built rows that
skip contract validation.

**5. `audit_log` uses BRIN, and is hash-chained.**
The log is append-only and physically time-ordered, so a btree would add write amplification for a
range-scan-only access pattern. Each entry stores `sha256(prev_hash || canonical_json(payload))`,
and `verify_audit_chain()` reports the first break as `hash_mismatch`, `chain_break` or
`sequence_gap` — the last catches wholesale row deletion, which leaves the remaining rows hashing
correctly.

## 3. Contract ↔ column mapping

`vexer_platform/persistence/rows.py` is the only place that knows how a contract becomes a row.
Exactly one rename exists:

| Contract field | Column | Why |
|---|---|---|
| `timestamp` | `occurred_at` | `timestamp` is a type keyword in PostgreSQL, and this column is the partition key on `event` |

This is enforced: `tests/test_kernel_persistence_schema.py` fails if a contract field loses its
column, if a column has no contract field, or if the enum vocabularies diverge.

## 4. Running it

```powershell
# 1. Start a real PostgreSQL (a runtime requirement, not optional)
docker compose up -d postgres

# 2. Apply the schema (versioned + auditable)
$env:VEXER_POSTGRES_DSN = "postgresql://vexer:vexer_dev_only@localhost:5432/vexer"
alembic upgrade head

# 3. Review the SQL before applying it in a change pipeline
alembic upgrade head --sql

# 4. Run the live-DB tests
$env:VEXER_PG_TEST_DSN = "postgresql://vexer:vexer_dev_only@localhost:5432/vexer_test"
pytest tests/test_kernel_persistence_integration.py -v
```

`alembic.ini` contains **no** database URL. `migrations/env.py` reads `VEXER_POSTGRES_DSN` and exits
with an explicit message when it is unset, so no credential can be committed and no migration can
quietly target a default `localhost`.

## 5. Operational duties the code assumes

These are not optional extras — the platform misbehaves without them.

| Duty | Why | State |
|---|---|---|
| **Add `event` partitions ahead of the horizon** | An insert into a missing partition fails on the ingest path. `partition_ddl(months=24)` creates 24 months from the deploy date. | **Not automated.** Needs a scheduled job before production traffic. |
| **Drop partitions behind the retention horizon** | §44: history is retained by policy, then detached — a `DROP TABLE`, not a mass `DELETE`. | **Not implemented.** Blocked on a retention decision. |
| **Run `alembic upgrade head` as a deploy step** | Schema changes must be versioned, not applied ad hoc. | `docker-compose.yml` defines a one-shot `migrate` service. |
| **Point `VEXER_PG_TEST_DSN` at a disposable database in CI** | The integration suite writes rows and tampers with the audit chain. | Gated; skips loudly when absent. |

## 6. Explicitly NOT validated yet

Stated plainly so no claim is overread (§49):

* **No live-server execution.** Docker is unavailable in the authoring environment. The DDL was
  generated and executed through Alembic in *offline* mode (278 lines of real SQL), and the store's
  error paths were exercised against a genuinely unreachable server — but **no migration has been
  applied to a real PostgreSQL instance and no integration test has passed against a live
  database.** `docker-compose.yml` and `deploy/Dockerfile` are authored and statically reviewed,
  not built or run.
* **No performance measurement.** No latency, throughput or query-plan figure is claimed. The
  indexing and partitioning decisions are reasoned, not benchmarked (§36).
* **Licence matrix incomplete.** Four new dependencies have no licence metadata; see
  `requirements-platform.txt`.
* **Apache AGE not exercised.** The recursive-CTE lineage walk is the implemented path; the graph
  engine is optional and its presence is reported via `graph_backend()`, never assumed.

