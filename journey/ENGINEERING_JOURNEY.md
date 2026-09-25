# VEXER — ENGINEERING JOURNEY (persistent context)

> Purpose: durable engineering memory for this repository across AI sessions and human
> contributors. If you are an agent: **read this file first**, then `docs/technology-selection.md`,
> then verify every claim against code before acting. This file is a *record*, not a substitute
> for inspection. Update it at the end of every increment.

* Last updated: increment 1 (platform kernel foundations)
* Baseline commit at audit time: `abc5892` (branch `main`, remote `origin` → github.com/blackXmask/vexer_layers)
* Working tree at audit time: clean, **55/55 tests green**, pyflakes clean

---

## 1. Objective

Transform the existing Domain 6–9 implementation into a **production-grade, integrated intelligence
subsystem**: real external systems instead of mocks, real persistence, real event processing,
provenance, security boundaries, observability, failure recovery, and validation by test — while
preserving the working, tested domain logic that already exists.

Non-goals: replacing working domain logic for its own sake; introducing infrastructure without a
documented workload justification; claiming production-readiness that has not been validated.

---

## 2. Verified environment facts (evidence, not assumption)

| Fact | Value | How verified |
|---|---|---|
| Platform | Windows (win32) | environment |
| Python | 3.12.10 (venv `.venv`) | `.venv\Scripts\python --version` |
| Docker | **NOT INSTALLED** (`docker` not on PATH) | `docker --version` → CommandNotFound |
| Network / PyPI | **reachable** (HTTP 200) | `httpx.get('https://pypi.org/pypi/aiokafka/json')` |
| Installed distributions | 54 (incl. `sqlite-vec`, `orjson`, `tenacity`, `uuid_utils`, `PyYAML`, `zstandard`) | `site-packages/*.dist-info/METADATA` scan |
| Deployment artefacts | **NONE** (no Dockerfile, no compose, no YAML, no SQL, no migrations) | recursive file scan |
| Repo size | 49 tracked files, ~4 956 LOC | `git ls-files` |
| Test baseline | 55 passed | `.venv\Scripts\python -m pytest -q` |
| Secrets in repo | none found (`api_key: null` + env-var indirection everywhere) | config inspection + `.gitignore` |

**Consequence (hard constraint).** Infrastructure-dependent work (PostgreSQL, Kafka, Qdrant, AGE,
OpenSearch, Valkey) **cannot be executed or validated in this environment**. Per mandate: implement
real integration boundaries, real client libraries and real deployment definitions, and mark
infra-dependent validation as *pending — not done*. Never substitute fakes.

---

## 3. As-is architecture map (verified against code, not the README)

### 3.1 Components

| Component | Path | Technology | Port | Persistence |
|---|---|---|---|---|
| D6 Orchestration gateway | `agent_orchestrator/` | LangGraph 1.2.12 + FastAPI | 8000 | SQLite checkpoints (`SqliteSaver`) |
| D7 Market intelligence | `business_market_intelligence/` | FastAPI + config-driven analytics | 8001 | none (stateless, config corpus) |
| D8 Opportunity/risk | `opportunity_risk_intelligence/` | FastAPI + risk register/scoring | 8002 | none |
| D9 Legal/IP | `legal_regulatory_ip_intelligence/` | FastAPI + regulation/IP catalogues | 8003 | none |

### 3.2 Internal layering (effective, as implemented)

```
L6 REST/Service gateway   api.py ×4, auth.py ×4 (optional X-API-Key), Pydantic input bounds
L5 Reasoner               llm_engine.py — MOCK | OPENAI | OLLAMA (ANTHROPIC enum declared, unimplemented)
L4 Orchestration          orchestrator.py — supervisor → (market|risk|legal)* → synthesis → human_gate
L3 Agents                 agents.py — Supervisor/planner + Market/OpportunityRisk/LegalRegulatory
L2 Tool bus               tools.py — RBAC → circuit breaker → lazy import → service → bounded audit
L1 Config                 config.json ×4 + lru_cache loaders + {{template}} rendering + env overrides
L0 Contracts/state        models.py ×4 — OrchestratorState, SubTask(sequence), DecisionArtifact, DecisionReport
L7 Domain intelligence    sources → analytics → service (D7/D8/D9), one-way integration into D6 only
```

### 3.3 Data flow (query path)

`POST /workflows/start` → `graph.invoke` → `SupervisorAgent.plan()` (rule-based keyword decomposition
via `llm.mock.decompose_rules`) → `SubTask(sequence 0..n)` → `router()` executes in planner order →
agents call `ToolRegistry` → per tool: RBAC check → circuit breaker → lazy domain import → service call
→ bounded in-memory audit record → synthesis aggregates into `DecisionArtifact` + flat `DecisionReport`
(verdict from config `decision_matrix`) → `interrupt()` HITL → `POST /workflows/approve` → `Command(resume=…)`.

### 3.4 Event flow

**There is no event flow.** No broker, no topics, no producers/consumers, no outbox, no replay, no
event contracts. Domain intelligence is computed synchronously per request over a static config corpus.
This is the largest architectural gap versus the mandate (§16, §22, §27).

### 3.5 Persistence model

One durable store: **SQLite checkpointing for LangGraph state** (single-node, local file,
`check_versions` not pinned, no migration path, no retention policy, no backup). D7/D8/D9 hold no
state; "evidence" is a config-embedded example list. There is no object store, search index, vector
store, graph store or cache.

### 3.6 Failure model (as-is)

| Mechanism | Where | Behaviour |
|---|---|---|
| Circuit breaker | `tools.py` per domain | threshold 3, recovery 30 s, half-open probe |
| Fallback to builtin mock | `tools.py` `_fetch_*` | served + `provider: builtin-fallback` in audit |
| Fail-closed governance | synthesis | any FAILED task ⇒ risk escalation + forced HITL |
| Bounded audit | `_BoundedAuditList` | cap 1000, oldest trimmed (single-process only) |
| Retry / timeout / backoff | — | **absent** for agent tasks (single attempt) |
| Idempotency | — | **absent** (duplicate `/workflows/start` creates a new thread each time) |

---

## 4. Architecture audit — findings

Severity: **S1** = correctness/security blocker, **S2** = production blocker, **S3** = debt.

### 4.1 Integration gaps
* **G1 (S1)** No event ingestion/streaming layer: nothing ingests external data continuously;
  intelligence is request-time only. Mandate §16/§22/§27 unmet.
* **G2 (S1)** No real external data integration: D7 sources are a static config corpus; the D2 OSINT
  seam is an interface only. Not "hardcoded intelligence", but not real intelligence either (§18/§41).
* **G3 (S2)** Person A domains (1/2/4/5) absent — seams degrade to mocks. `provider: builtin*` is
  visible in the audit trail (good) but is not surfaced as a *state* in outputs (§19).
* **G4 (S2)** No shared domain model: each domain defines its own Pydantic models plus ad-hoc
  `to_domain6_*` mappers. No Entity/Event/Relationship/Evidence/Signal contracts, no schema-versioned
  envelope (§11).
* **G5 (S2)** Provenance stops at citation strings and `provider` tags: no lineage chain, no content
  hash, no model/pipeline attribution (§12/§45).
* **G6 (S2)** No contradiction representation — newer assertions about a regulation/IP record simply
  replace older ones (§14).
* **G7 (S2)** Temporal model is inert: `IPConflict.expires` exists, but there are no
  `valid_from/valid_until` semantics and no "what was true at T" query path (§15/§17).
* **G8 (S2)** Confidence values are **hardcoded constants** (`confidence_score: 0.94/0.91/0.96` in
  config) — precisely the anti-pattern §13 forbids. Needs a documented methodology.
* **G9 (S2)** No meaningful AI/LLM integration: planning is rule-based (legitimate, but must be
  labelled as such); `ANTHROPIC` is declared with no implementation; no structured-output enforcement,
  no token accounting, no model attribution (§37–§40).
* **G10 (S2)** No observability: no metrics, tracing, structured logs, correlation IDs, or
  liveness/readiness separation (§20/§28).

### 4.2 Code quality
* Dead enum branch: `ModelProvider.ANTHROPIC` unreachable in `llm_engine.generate` — S3.
* `load_config()` is `lru_cache`d per process and `tools.py` reads config **at import time** for RBAC
  and breakers ⇒ config edits require a restart; `VEXER_CONFIG_PATH` changes race in tests — S3.
* `_BREAKERS`, `_audit_log`, `TOOL_PERMISSIONS` are **class-level mutable global state** ⇒ no
  multi-tenant isolation, no cross-process consistency, blocks horizontal scale — S2.
* Duplication: three near-identical `execute_*_agent` nodes in `orchestrator.py` differing only by role
  ⇒ refactor to registry-driven dispatch — S3.
* No static type checking (mypy/pyright) in the verification gate; pyflakes only — S3.

### 4.3 Security
* Auth: optional shared `X-API-Key`. No rate limiting, rotation, per-tenant identity, mTLS or service
  identity — S2.
* **Unverified (must inspect before claiming):** whether `auth.py` uses a timing-safe comparison.
* Checkpoint DB is unencrypted, unauthenticated and holds full investigation state — S2.
* No outbound-fetch surface yet, therefore no SSRF controls yet — but **any** future crawler or
  LLM-endpoint fetch must add allow-lists, redirect limits, private-IP blocking, size and time caps (§18) — S2 (pre-emptive).
* Audit trail is in-memory only: lost on restart, not tamper-evident, not multi-process (§45) — S2.
* No dependency vulnerability scanning (pip-audit) — S3.

### 4.4 Reliability
* Single-attempt agent execution; no retry/backoff/per-step timeout budget (§37) — S2.
* No idempotency: replay duplicates work and would duplicate state once persistence lands (§21) — S2.
* SQLite checkpointer is single-writer; concurrent workflows serialise on the file lock — S2.
* No DLQ and no poison-message handling (no queue exists) — S2.
* No retention/purge policy for checkpoints or evidence (§44) — S3.

### 4.5 Scalability
* Synchronous per-request fan-out to three domains with no concurrency (§22) — S2.
* Static in-process caches and module-level breakers/audit prevent horizontal consistency — S2.
* No pagination/streaming/batching; D7 `collect_raw_items` materialises the entire corpus in RAM —
  fine at config scale, unacceptable at document scale — S2.
* No backpressure primitives (bounded queues, credit-based flow) — S2.

---

## 5. External technology selection (licences verified this session)

Full matrix with evidence URLs, versions, integration method, runtime/build-time, optional/mandatory
and maintenance status: **`docs/technology-selection.md`**.

| Need | Selected | Licence (verified) | Rejected alternative & why |
|---|---|---|---|
| Relational + JSONB system of record | PostgreSQL 16/17 | PostgreSQL Licence (permissive) | — |
| Graph relationships + temporal validity | **Apache AGE** (Postgres extension) | Apache-2.0 (ASF) | Neo4j CE = **GPL v3** (verified at neo4j.com/licensing) — copyleft risk for a proprietary platform; Enterprise = commercial |
| Event streaming / replay | **Apache Kafka** (KRaft) | Apache-2.0 | Redpanda licence could not be verified from its raw LICENSE (fetch 404) → not selected pending verification |
| Vector / semantic search | **Qdrant** | Apache-2.0 (© Qdrant Solutions GmbH) | pgvector (PostgreSQL Licence) documented as the single-node fallback |
| Full-text search | **OpenSearch** | Apache-2.0 | Elasticsearch is triple-licensed (SSPL / Elastic / AGPL) — avoid |
| Cache / rate limit / dedup | **Valkey** | **BSD-3-Clause** (© Valkey contributors, Redis Ltd.) | Redis 8+ = RSALv2 **or** SSPLv1 **or** AGPLv3 (verified in `LICENSE.txt`) — not OSI-open-source |
| Analytical aggregation (only when Postgres is measured insufficient) | **ClickHouse** | Apache-2.0 (© ClickHouse Inc.) | DuckDB (MIT) for embedded/batch analytics; no measured justification yet for a cluster |
| Immutable raw evidence store | S3-compatible API (cloud S3 in production; MinIO **AGPL-3.0** for local dev only, flagged) | — | MinIO AGPL noted as a distribution constraint |
| Observability | OpenTelemetry SDK + Prometheus (Apache-2.0); Grafana (**AGPL-3.0**, run unmodified as a service) | — | Loki/Tempo AGPL — same constraint, keep unmodified |
| Batch transforms | Polars (MIT) / PyArrow (Apache-2.0) in-process; DuckDB (MIT) embedded | — | Spark/Flink withheld until a measured need exists |
| Document parsing | pypdf (BSD-3), python-docx (MIT), trafilatura (Apache-2.0); Tesseract (Apache-2.0) for OCR | — | PyMuPDF is AGPL — only as an unmodified service if ever needed |
| LLM providers | OpenAI-compatible HTTP + Ollama + vLLM (Apache-2.0) behind one interface | — | Rule-based planner retained but **relabelled** as a deterministic planner, not "AI" |

> **Licensing rule:** any AGPL/GPL/BSL component must remain an **unmodified, separately deployed
> service** with an explicit note, or be replaced. No copyleft code is linked into our process.

---

## 6. Migration strategy (incremental, backwards-compatible)

1. **Additive only.** Existing endpoints, response shapes and `to_domain6_*` contract keys stay valid;
   new fields are optional. There is no destructive data migration to perform — the only durable state
   is SQLite checkpointing, which remains supported.
2. **Contracts before infrastructure.** Define the shared domain model + schema versioning first, then
   derive storage, broker and index payloads from it (one schema, many sinks).
3. **Real clients behind interfaces.** Each external system gets a thin adapter with lazy import,
   startup capability detection, explicit `UNAVAILABLE` propagation (§19) and a documented deployment
   requirement. Adapters are covered by *integration tests that skip with a stated reason* when the
   dependency is absent — never by silent fake behaviour.
4. **Shadow-read before cutover.** Dual-write events/provenance while the current path still serves
   results; compare; then switch the read path.
5. **Preserve domain logic.** The algorithms in `analytics.py` / `service.py` / `tools.py` remain the
   source of intelligence; they are rewired to real inputs rather than replaced.

---

## 7. Implementation plan

| Phase | Scope | Status |
|---|---|---|
| P1 Repository discovery | full inventory, verified facts | ✅ done |
| P2 Architecture audit | findings G1–G10 + code/security/reliability/scalability | ✅ done (this file) |
| P3 Dependency & licence audit | verified licences + selections | ✅ done (`docs/technology-selection.md`) |
| P4 Contract design | shared domain model, envelopes, schema versioning | 🔵 in progress (increment 1) |
| P5 Data model design | Postgres DDL, AGE graph model, Kafka topics, Qdrant payload | ⬜ next |
| P6 Infrastructure selection | compose, migrations, health checks | ⬜ next |
| P7 Layer boundary refactor | registry-driven agent dispatch; remove global mutable state | ⬜ pending |
| P8 Real integrations | Postgres/Kafka/Qdrant/AGE/OpenSearch/Valkey adapters + LLM providers | ⬜ pending |
| P9 Persistence | system of record + object storage for raw evidence | ⬜ pending |
| P10 Event processing | ingest → validate → dedup → correlate → enrich → persist, DLQ + backpressure | ⬜ pending |
| P11 Provenance | end-to-end lineage chain + retrieval API | ⬜ pending |
| P12 Security | identity, RBAC/ABAC, rate limits, secret management, tamper-evident audit | ⬜ pending |
| P13 Observability | OTel traces, Prometheus metrics, structured logs, health/readiness | ⬜ pending |
| P14 Failure recovery | retry/timeout budgets, idempotency, replay, reconciliation | ⬜ pending |
| P15–P19 Validation | integration, failure injection, load/stress, security, E2E, deployment | ⬜ pending — **blocked on Docker** |

---

## 8. Increment 1 — built, tested, and explicitly NOT validated

**Built (real code, unit-tested): `vexer_platform/` kernel**

| Module | Responsibility |
|---|---|
| `ids.py` | Monotonic ULIDs (lexicographically time-sortable, stdlib-only, thread-safe), deterministic UUIDv5 stable IDs for dedup/idempotency, SHA-256 content hashing, idempotency-key derivation |
| `contracts.py` | Shared domain model: `Entity`, `Event`, `Relationship`, `CausalLink`, `Evidence`, `IntelligenceSignal`, `Claim`, `ProvenanceRecord` + the 11 relationship types from §16 and the 5 provenance classes from §39 (observed/derived/hypothesis/prediction/unknown); strict validation (aware UTC timestamps, confidence bounds, non-empty IDs) with lenient parsing for external input |
| `confidence.py` | Documented, explainable confidence methodology `conf-v1`: evidence quality × source reliability × agreement × freshness decay × model confidence − contradiction penalty, returning per-component breakdown + rationale. Confidence ≠ truth (separate `truth_status`) |
| `temporal.py` | Validity windows (`valid_from` inclusive / `valid_until` exclusive), bitemporal records, as-of queries ("what was true at T"), expiry and overlap semantics |
| `errors.py` | Structured error catalogue (`code`, `retryable`, `http_status`, service, request/correlation/trace IDs, `details`), with secret redaction for logs |
| `context.py` | Request/correlation/trace propagation via `contextvars` (async- and thread-safe), W3C `traceparent` interop, RPC envelope with timeout + structured error payload |
| `config.py` | Kernel profiles (dev/test/staging/prod), fail-fast validation, secrets only via environment (never in code) |

**Tests:** root-level `tests/` package (additive to the 4 domain suites).

**Explicitly NOT validated in this environment — do not claim otherwise:**
* Any datastore/broker integration (Docker is absent).
* Any load/stress/soak number (no infrastructure exists to measure; no benchmark is claimed).
* Security validation beyond static review (no penetration test has been performed).

---

## 9. Risks & known limitations (living list)

1. **Person A domains (1/2/4/5) do not exist** — end-to-end validation of the full five-layer platform
   is impossible until those services are implemented or their real equivalents are adopted here.
2. **Licence constraints** — AGPL components (MinIO, Grafana, Loki, PyMuPDF) and GPL components
   (Neo4j CE) must remain unmodified, separately deployed services. Neo4j was therefore not selected.
3. **Single-node assumptions** in the tool bus (`_BREAKERS`, in-memory audit, import-time config) must
   be externalised (Valkey/Postgres) before any horizontal-scaling claim is truthful.
4. **Hardcoded confidences remain** in the D7/D8/D9 configs and are still consumed by the existing
   agents; migrating them to `conf-v1` is an explicit follow-up task (increment 2+).
5. **Event ordering / duplicate-delivery semantics** are designed (`ids.py` + contracts) but not yet
   exercised end-to-end because no broker is running.
6. **`check_versions` for LangGraph checkpointing is unpinned** — a future library upgrade could
   invalidate existing checkpoints; pin it when the persistence phase lands.

---

## 10. Journal

### Increment 1 — platform kernel foundations
* **Changed (additive only):** `journey/ENGINEERING_JOURNEY.md` (this file),
  `docs/technology-selection.md`, `vexer_platform/` (7 modules), root `tests/` (kernel tests),
  `pyproject.toml` (packages + testpaths + dev extras). No existing D6–D9 behaviour was modified.
* **Why:** mandate §11–§15, §19–§21 and §56 require shared versioned contracts, a documented confidence
  methodology and a persistent engineering record *before* storage/broker schemas are chosen. Deciding
  Postgres/Kafka/Qdrant payloads before the contracts exist would guarantee rework.
* **Broke:** nothing — all changes are additive; the 55-test baseline is preserved.
* **Fixed:** nothing yet (audit findings are recorded, not yet remediated).
* **Tests added:** kernel unit tests — contract validation and JSON-safety, ID monotonicity and
  uniqueness (including same-millisecond monotonicity and thread-safety), confidence monotonicity /
  freshness decay / contradiction penalty / clamping, temporal as-of and boundary semantics, structured
  error serialisation and redaction, context isolation across tasks/threads.
* **Risks remaining:** §9 items 1–6.
* **Next increment:** P5 — data model DDL (Postgres + AGE), Kafka topic/schema definitions derived from
  `vexer_platform.contracts`, Qdrant collection/payload design; then P7 — refactor the tool bus to
  remove class-level mutable state and import-time config reads.



### Increment 2 — persistence: data model, migrations, real PostgreSQL access

* **Added (additive only):**
  * `vexer_platform/persistence/schema.py` — canonical DDL: 6 enum types, 12 tables, 44 indexes,
    monthly `event` partitions generated relative to the deploy date, and `iter_ddl()` in strict
    dependency order (enums → tables → indexes → partitions).
  * `vexer_platform/persistence/rows.py` — declarative contract ↔ column mapping (`TableSpec` /
    `Column`), pure `to_params`/`from_row` round-trip, and fully parameterised statement builders.
  * `vexer_platform/persistence/postgres.py` — real `asyncpg` store: bounded pool, batched writes,
    global event idempotency, temporal reads, server-side cursor streaming, bounded recursive-CTE
    lineage walk, hash-chained audit log with verification, and explicit `DataHealth` reporting.
  * `migrations/` + `alembic.ini` — revision `0001_initial` that executes the canonical DDL (no
    copied SQL), `env.py` that refuses to run without an explicit `VEXER_POSTGRES_DSN`, and a
    `script.py.mako` template.
  * `docker-compose.yml` + `deploy/Dockerfile` — pinned PostgreSQL 16.4-alpine with healthcheck,
    non-root build, dropped capabilities, read-only runtime, and a one-shot `migrate` service.
  * `docs/data-model.md`, `requirements-platform.txt`, `.gitignore` entry, pytest markers/asyncio
    config in `pyproject.toml`.
  * `tests/test_kernel_persistence_schema.py` (42 tests), `tests/test_kernel_persistence_rows.py`
    (52 tests), `tests/test_kernel_persistence_integration.py` (20 tests, DSN-gated).
* **Why:** the mandate's phases 5–6 (data model, infrastructure) precede 9–11 (persistence,
  event processing, provenance). Choosing Postgres/Kafka/Qdrant payloads before the contracts
  existed would have guaranteed rework; increment 1 fixed the contracts, so the schema could be
  derived from them rather than guessed.
* **Broke:** nothing. All changes are additive; the 100-test baseline is preserved. The
  `asyncio_mode = "auto"` addition is the only global test-config change and does not affect the
  four pre-existing domain suites (all synchronous).
* **Fixed — four real defects the new tests caught (all in increment-2 code, none in D6–D9):**
  1. **Inconsistent DDL statement terminators** — one-line `CREATE INDEX` statements had no `;`.
     Fixed at the single assembly point (`iter_ddl` → `_terminated`) rather than by hand-editing 30
     statements, so the invariant holds for statements added later.
  2. **Malformed `alembic.ini` config (TOML syntax error)** — a pytest marker description was
     written as Python-style implicit string concatenation across lines, which TOML does not
     support. Caught by a `tomllib` parse check after pytest refused to start.
  3. **Broken async test harness** — `pytest-asyncio` was not installed and the module-scoped
     fixture used `loop_scope` on `pytest.fixture` (only valid on `pytest_asyncio.fixture`), so the
     live-DB suite would have *errored* rather than run once a DSN was supplied. Fixed by adding
     the dependency and using the correct decorator; verified by running a DSN-independent
     integration test to green.
  4. **Fixture bug in the test suite** — a `CausalLink` sample used `occurred_at`; the contract
     field is `timestamp`, and `extra="forbid"` caught it at collection time.
* **Tests added:** 94 unit/contract tests plus 20 DSN-gated integration tests. Two assertions in my
  own parity test were also wrong (they searched only *named* table constraints and compared a
  constraint name case-sensitively); corrected to inspect the whole `CREATE TABLE` statement.
* **Validated here:** 194 passed / 1 skipped; pyflakes exit 0; `alembic upgrade head --sql`
  generates 278 lines of real PostgreSQL DDL offline; `alembic` refuses to run without a DSN; the
  store raises a structured `DEPENDENCY_UNAVAILABLE` against a genuinely unreachable server and
  reports `UNAVAILABLE` rather than falling back.
* **NOT validated (do not overread):** no migration has been applied to a live PostgreSQL instance
  and no integration test has passed against a live database — Docker is absent here.
  `docker-compose.yml` and `deploy/Dockerfile` are reviewed, not built. No performance figure is
  claimed. Four new dependencies have no licence metadata (recorded as UNVERIFIED, not guessed).
* **Risks remaining:** §9 items 1–6, plus:
  7. **Partition maintenance is not automated** — `partition_ddl` creates 24 months from the deploy
     date; a scheduled job must extend it and drop partitions behind the retention horizon.
  8. **Retention policy undefined** — §44 requires a policy; none is set, so nothing is dropped.
  9. **`downgrade()` on revision 0001 is destructive** (drops all tables). Intentional for a
     disposable database, but §54 requires a documented migration strategy before anyone runs it
     against real data.

* **Next increment:** P7 — remove the single-node assumptions in the Domain 6 tool bus
     (`_BREAKERS` class-level mutable state, in-memory audit, import-time config reads) so
     horizontal scaling is possible; then P10 — event processing on a real broker (ingest →
     validate → dedup → correlate → enrich → persist) with DLQ and backpressure.

### Increment 3 — P7: tool-bus runtime; removal of fabricated intelligence

* **Added:** `agent_orchestrator/bus.py` (`CircuitBreaker`, `CircuitBreakers`,
  `InMemoryCircuitBreakers`, `InMemoryAuditSink`, `ToolAuditRecord`, `ToolBus`),
  `agent_orchestrator/tests/test_tool_bus.py` (21 tests).
* **Changed:** `agent_orchestrator/tools.py` is now a thin facade over `ToolBus`;
  `api.py` `/audit/tools` returns a structured envelope; `orchestrator_config.json` gains
  `allow_demo_data: false` and loses the fabricated `compliance.detected_regulations` list;
  `vexer_platform/contracts.py` gains the shared `DataStatus` enum (§19), with
  `persistence.postgres.DataHealth` reduced to an alias of it so the vocabulary cannot drift.
* **Why:** P7 was scoped as "remove single-node assumptions", but auditing the bus first showed the
  harder problem was not where the state lived — it was that the state existed to absorb failures
  with invented data.

**Four real defects fixed in Domain 6 (previously undocumented):**

1. **Configuration was frozen at import.** `TOOL_PERMISSIONS`, the audit cap and the breaker policy
   were evaluated in class/module bodies, so `VEXER_CONFIG_PATH` and any test config swap were
   invisible without a process restart, and `reload_config()` could not fix it. A bad config file
   crashed at *import*. Now: the default bus is built lazily and holds its own config.
2. **State was process-global.** `_BREAKERS` and `_audit_log` were shared by every caller *and*
   every worker process, so a horizontally scaled deployment had one circuit breaker per process and
   an audit trail that was only a fragment. Now: instance-owned, injectable, with `health()` reporting
   the backend's `distribution` so a single-process bus is never read as cluster-wide.
3. **The breaker predicate mutated state and was not thread-safe.** `is_open()` transitioned to
   half-open *as a side effect of being read*, so inspecting a breaker changed it and two threads
   could both take the probe. It also used `time.time()`, so an NTP step backwards could wedge a
   breaker open. Now: pure `state`, monotonic clock, one admitted probe, all mutation under a lock.
4. **Fabricated intelligence (§41, §52).** Four separate fallbacks invented data:
   * `query_knowledge_graph` returned fixed relationships plus "verified facts" with hardcoded
     confidences 0.96/0.91 for *any* entity name;
   * `query_osint_signals` returned a fabricated "Global Tech News" headline and a fabricated
     "Government Tender Board" RFP, with credibility scores 0.89/0.95 and hardcoded dates;
   * `evaluate_rfp_fit` returned `GO`/`NO_GO` from a three-keyword overlap heuristic — a bid/no-bid
     business decision from a substring match;
   * `check_legal_compliance` returned `compliance_status: CLEARED` whenever two keyword lists found
     no match, and listed a hardcoded `["EU AI Act","GDPR","NIST CSF"]` as *detected* regulations.
     **This was the most dangerous defect in the repository**: a compliance clearance asserted from
     a keyword scan would let a business ship unlawful work, and a false negative in compliance costs
     far more than a false positive.
   All four now return empty results tagged with the shared `DataStatus` vocabulary. Keywords may
   still *raise* a flag; they can never clear one. A labelled demo payload is gated behind
   `tools.allow_demo_data` (default `false`).

* **Behaviour changes requiring sign-off (deliberate, documented in code and tests):**
  * `query_knowledge_graph`/`search_documents`/`query_company_context` return `data_status`.
  * `check_legal_compliance` returns `UNKNOWN` instead of `CLEARED` when Domain 9 is unavailable.
  * `evaluate_rfp_fit` returns `INSUFFICIENT_DATA` instead of `GO`/`NO_GO` on the heuristic path.
  * `GET /audit/tools` returns `{records, count, bus}` instead of a bare list.
  * Four existing tests asserted on the *fabricated* output (`len(relationships) > 0`,
    `provider == "builtin-fallback"`, `_BREAKERS`, `_audit_log.maxlen`). They were rewritten against
    the new seams (`ToolRegistry.set_bus`, injected `service_resolver`, `isolated_bus` fixture).
    These tests validated a mock, not a contract.
* **Validation:** 214 passed / 1 skipped; pyflakes exit 0; `test_drive.py` completes the full
  HITL lifecycle; no fabricated literal remains in any source file.
* **NOT validated:** still no live database and no multi-process test. The Postgres-backed breaker
  registry and audit sink are **designed for** in `bus.py` but **not implemented** — an in-memory
  sink cannot be made cluster-wide, so honest horizontal scaling of the audit trail remains future
  work. No latency or throughput figure is claimed.
* **Still open (not in this increment):** `agents.py` hardcodes
  `competitive_posture`, `market_growth_vector` and `confidence_score` as constant strings, and the
  Phase 1/2 decision-report tests assert on that text. Removing it changes decision output and needs
  its own increment and explicit sign-off — flagged, not silently changed.
* **Next increment:** P10 — event processing on a real broker (ingest → validate → dedup → correlate
  → enrich → persist) with DLQ and backpressure, plus the Postgres-backed audit sink so the audit
  trail survives more than one process.

