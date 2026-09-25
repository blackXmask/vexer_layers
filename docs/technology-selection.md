# External Technology Selection — Vexer Intelligence Platform

> Mandate §6 / §48. Every external system is recorded with **evidence**. Licences marked "verified"
> were fetched from an authoritative source (project LICENSE file, PyPI metadata, or the vendor's own
> licensing page) during the audit; the URL is recorded. Nothing here is invented.

* Audit date: increment 1 · re-verify before every dependency upgrade.
* Legend — `runtime` = must be deployed for the system to work; `build` = tooling only;
  `mandatory`/`optional` = whether the platform can operate without it (in degraded mode).

## 1.1 Platform data stores

| # | Project | Version | Licence (verified) | Evidence | Purpose | Integration | R/B | Opt/Mand | Security notes |
|---|---|---|---|---|---|---|---|---|---|
| 1 | **PostgreSQL** | 16/17 | PostgreSQL Licence (permissive) | postgresql.org/about/licence | System of record: entities, events, evidence, claims, relationships, provenance, audit | `asyncpg` pool + Alembic migrations; JSONB attributes | runtime | mandatory | TLS + scram-sha-256; row-level security available; never commit DSNs |
| 2 | **Apache AGE** | 1.5.x | Apache-2.0 | ASF project licence policy | Graph traversal/Cypher over the same PG instance (relationships, causal links, lineage) | PG extension; `cypher()` via SQL | runtime | optional (fallback: recursive CTEs) | Same trust boundary as PG; pin + patch with PG |
| 3 | **Kafka** | 3.9/4.x KRaft | Apache-2.0 | kafka.apache.org (ASF) | Event backbone: ingest topics, replay, ordered partitions, DLQ | `aiokafka`; schema-versioned payloads from `vexer_platform.contracts` | runtime | mandatory for event mode | TLS + SASL/SCRAM or mTLS; per-topic ACLs; no anonymous access |
| 4 | **Qdrant** | 1.12+ | Apache-2.0 (© Qdrant Solutions GmbH) | verified raw LICENSE (GitHub, this session) | Vector/semantic retrieval over evidence + document chunks | `qdrant-client` gRPC; payload carries IDs + validity window | runtime | optional (fallback: pgvector → keyword) | API key + TLS; per-tenant collections; server-side payload filters |
| 5 | **OpenSearch** | 2.x/3.x | Apache-2.0 | opensearch.org | Full-text search over documents/evidence + dashboards | `opensearch-py`; index templates per evidence type | runtime | optional (fallback: PG FTS) | Security plugin + TLS + role mapping; never publicly exposed |
| 6 | **Valkey** | 8.x | **BSD-3-Clause** | verified raw COPYING (GitHub, this session) | Distributed cache, circuit-breaker state, rate limiting, idempotency keys | `valkey` client; TTL on every key | runtime | optional (fallback: in-process, single-node, flagged) | ACL users + TLS; no un-TTL'd PII/secrets |
| 7 | **ClickHouse** | 24.x/25.x | Apache-2.0 (© ClickHouse Inc.) | verified raw LICENSE (GitHub, this session) | Analytical aggregation **only when Postgres is measured insufficient** | HTTP/native client; materialised views | runtime | optional | Dedicated read-only user; TLS |
| 8 | **Object storage** (S3/GCS/Azure) | — | vendor terms | vendor docs | Immutable raw evidence blobs (originals) + large artefacts | `boto3` S3 API | runtime | mandatory at evidence scale | SSE + versioning + object lock; presigned URLs only |

## 1.2 Supporting / observability services

| # | Project | Version | Licence (verified) | Evidence | Purpose | R/B | Opt/Mand | Security notes |
|---|---|---|---|---|---|---|---|---|
| 9 | **OpenTelemetry SDK** | 1.2x | Apache-2.0 | opentelemetry.io (CNCF) | Traces/metrics/log correlation | runtime | optional (fallback: logs only) | No PII in span attributes |
| 10 | **Prometheus** | 2.x/3.x | Apache-2.0 | prometheus.io (CNCF) | Metrics + alerting | runtime | optional | Restrict `/metrics` to cluster network |
| 11 | **Grafana** | 11.x | **AGPL-3.0** ⚠ | verified raw LICENSE (GitHub, this session) | Dashboards | runtime | optional | Deploy **unmodified** as a separate service; no code reuse |
| 12 | **MinIO** | RELEASE.x | **AGPL-3.0** ⚠ | verified raw LICENSE (GitHub, this session) | Local-dev S3 substitute only | build/dev | dev-only | Never shipped in a distributed product image |

## 1.3 Considered and rejected

| Project | Licence finding | Evidence | Decision & reason |
|---|---|---|---|
| **Neo4j** | Community Edition = **GPL v3**; Enterprise = commercial | verified at `neo4j.com/licensing` ("Neo4j Community Edition (GPL v3)") | **Rejected** — GPL v3 copyleft inside a proprietary platform bundle. Apache AGE selected instead |
| **Redis 8+** | **RSALv2 / SSPLv1 / AGPLv3** tri-licence (BSD-3 only for ≤7.2) | verified raw `LICENSE.txt` (GitHub, this session) | **Rejected** as cache/dedup store — not OSI-open-source. Valkey (BSD-3 fork) selected |
| **Redpanda** | licence **NOT VERIFIED** — raw LICENSE fetch returned HTTP 404 | redpanda-data/redpanda | **Rejected pending verification** — per mandate, no component is adopted on an unverified licence. Kafka (Apache-2.0) selected |
| **Elasticsearch** | SSPL / Elastic Licence / AGPL tri-licence | elastic.co licensing page | **Rejected** — non-permissive. OpenSearch selected |
| **PyMuPDF** | AGPL-3.0 | pymupdf.readthedocs.io licence page | **Avoided in-process** — use pypdf (BSD-3) for PDF; PyMuPDF only as an unmodified service if ever required |
| **Spark / Flink** | Apache-2.0 (licence fine) | — | **Deferred** — no measured workload justifies the operational cost yet; Polars/DuckDB cover current batch needs |

## 2. Python dependencies — planned additions (versions verified on PyPI this session)

| Package | Version | Licence (PyPI metadata) | Purpose | Runtime/Build | Opt/Mand |
|---|---|---|---|---|---|
| `asyncpg` | 0.31.0 | **UNVERIFIED in metadata** — resolve before adoption (project is permissive; confirm LICENSE) | Async PostgreSQL driver (system of record) | runtime | mandatory |
| `psycopg` | 3.3.6 | **UNVERIFIED in metadata** — known LGPL-3.0, confirm before adoption | Sync/`COPY` fallback + migration tooling | build/runtime | optional |
| `sqlalchemy` | 2.1.1 | **UNVERIFIED in metadata** (project is MIT) — confirm | Core SQL toolkit; explicit-SQL-first usage | runtime | optional |
| `alembic` | 1.20.0 | **UNVERIFIED in metadata** (project is MIT) — confirm | Versioned schema migrations | build/runtime | mandatory for schema |
| `aiokafka` | 0.14.0 | **UNVERIFIED in metadata** (project is Apache-2.0) — confirm | Kafka producer/consumer | runtime | mandatory for event mode |
| `qdrant-client` | 1.19.1 | Apache-2.0 ✅ | Vector retrieval client | runtime | optional |
| `opensearch-py` | 3.2.0 | Apache-2.0 ✅ | Full-text search client | runtime | optional |
| `valkey` | 6.1.1 | MIT ✅ | Cache/dedup/rate-limit client | runtime | optional |
| `prometheus-client` | 0.26.0 | **UNVERIFIED in metadata** (project is Apache-2.0) — confirm | Metrics exposition | runtime | optional |
| `opentelemetry-sdk` | 1.45.0 | **UNVERIFIED in metadata** (project is Apache-2.0) — confirm | Tracing | runtime | optional |
| `opentelemetry-instrumentation-fastapi` | 0.66b0 | Apache-2.0 ✅ | Auto-instrumentation | runtime | optional |
| `structlog` | 26.1.0 | Apache-2.0 / MIT ✅ | Structured JSON logging | runtime | optional |
| `boto3` | 1.43.102 | Apache-2.0 ✅ | Object storage (raw evidence) | runtime | mandatory at scale |
| `pypdf` | 6.19.0 | **UNVERIFIED in metadata** (project is BSD-3) — confirm | PDF text extraction (licence-safe alternative to AGPL PyMuPDF) | runtime | optional |
| `python-docx` | 1.2.0 | MIT ✅ | DOCX extraction | runtime | optional |
| `trafilatura` | 2.2.0 | **UNVERIFIED in metadata** (project is Apache-2.0) — confirm | HTML main-content extraction | runtime | optional |
| `polars` | 1.44.2 | MIT ✅ | In-process dataframe transforms | runtime | optional |
| `duckdb` | 1.5.5 | MIT ✅ | Embedded OLAP for batch analytics | runtime | optional |
| `pyarrow` | 25.0.1 | **UNVERIFIED in metadata** (project is Apache-2.0) — confirm | Columnar interchange | runtime | optional |
| `jsonschema` | 4.26.0 | **UNVERIFIED in metadata** (project is MIT) — confirm | External-input schema validation | runtime | optional |
| `testcontainers` | 4.15.0 | Apache-2.0 ✅ | Integration tests against real containers | build | optional |
| `pytest-asyncio` | 1.4.0 | **UNVERIFIED in metadata** (project is Apache-2.0) — confirm | Async test support | build | optional |
| `hypothesis` | 6.168.1 | **UNVERIFIED in metadata** (project is MPL-2.0) — confirm | Property-based tests for contracts/IDs | build | optional |
| `locust` | 2.46.6 | MIT ✅ | Load/stress testing | build | optional |
| `pip-audit` | 2.10.1 | Apache-2.0 ✅ | Dependency vulnerability scanning | build | mandatory in CI |
| `bandit` | 1.9.4 | Apache-2.0 ✅ | Static security analysis | build | optional |
| `mypy` | 2.3.1 | **UNVERIFIED in metadata** (project is MIT) — confirm | Static typing | build | optional |

> **Honesty rule:** entries marked *UNVERIFIED* were **not** adopted on reputation. They must have
> their LICENSE confirmed (or PyPI classifier resolved) before they are pinned into
> `requirements-platform.txt`. This document does not fabricate licence names.

## 3. Existing runtime dependencies (installed baseline, licence from dist metadata)

| Package | Installed | Licence evidence |
|---|---|---|
| `httpx` | 0.28.1 | BSD-3-Clause (declared) |
| `langchain`, `langchain-core`, `langsmith` | 1.4.2 / 1.6.5 / 0.14.0 | MIT (declared) |
| `tenacity` | 9.1.4 | Apache-2.0 (declared) |
| `orjson` | 3.12.0 | Apache-2.0 / MIT / MPL (dual-declared) |
| `certifi` | 2026.7.22 | MPL-2.0 (declared) |
| `fastapi`, `pydantic`, `langgraph*`, `uvicorn`, `starlette`, `anyio`, `pytest`, `pyyaml`, `sqlite-vec` | see lock | **classifier missing in metadata → UNVERIFIED**; resolve via `pip-audit`/LICENSE read before release |
| `pyflakes` | 4.0.0 | MIT (declared) |

Action item (P3 follow-up): resolve the *UNVERIFIED* rows with `pip-audit` + a LICENSE read, and record
the result here. Until then the licence matrix is **incomplete**, and that is stated rather than hidden.

## 4. Engineering rules derived from this matrix

1. No copyleft (GPL/AGPL) or source-available (SSPL/RSAL/BSL) code is **linked into our processes**.
2. AGPL components (Grafana, MinIO, Loki) may only be deployed **unmodified, as separate services**,
   and MinIO is restricted to the local-dev profile.
3. Every new dependency requires: verified licence + purpose + runtime/build classification +
   security review + maintenance check, recorded in this file (§48).
4. `pip-audit` runs in the verification gate; a new high/critical advisory blocks the increment.
5. Versions are pinned once verified; "latest" is never a pin.

