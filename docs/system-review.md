# Deep System Review — architecture, platform, event flow, confidence, provenance

> Read-only audit. Every claim below is backed by a reproducible check (command or file:line).
> §49: no claim here is asserted without evidence, and where something is unvalidated it says so.

## Headline (round 2 — deeper)

Three findings dominate, and they were not visible in round 1:

1. **51% of the test suite tests unreachable code** (83 of 162 test functions target `vexer_platform`,
   which no running service calls). The suite is green and therefore **actively misleading** — it
   certifies a persistence layer, a confidence model and a provenance DAG that the product does not
   use.
2. **The "AI" layer is a keyword matcher, and one provider silently lies.** `ModelProvider.ANTHROPIC`
   is declared and unreachable: requesting it returns the **MOCK** response with no error. An operator
   who configures Anthropic believes they have Claude and gets `decompose_rules` keyword matching.
3. **The platform runs unauthenticated.** `auth.enabled = false` on all four services, by default,
   with ports exposed. And the P7 import-time-config defect I fixed in `tools.py` is still present in
   all four `api.py` files, so auth cannot be toggled without a restart even if you try.

Underneath those: the platform layer and the working domains remain two disconnected systems.
`vexer_platform` is 3,393 lines; the only symbol any domain imports is the `DataStatus` enum.
`conf-v1`, `ProvenanceRecord`, `Claim`, `ValidityWindow`, `PostgresStore`, `record_event`,
`VexerError`, `new_ulid` — **zero call sites**. Nothing has ever been written to PostgreSQL by a
running service. The real system is a synchronous, in-process chain with hardcoded strings, eight
incomparable confidence formulas, no event pipeline, no provenance, and no observability.


## Evidence

```
vexer_platform              12 files   3,393 lines
agent_orchestrator          12 files   3,136 lines
business_market_intel.       9 files     967 lines
opportunity_risk_intel.      8 files     844 lines
legal_regulatory_ip_intel.   8 files     870 lines

vexer_platform imports found in all domain code:
  bus.py   -> from vexer_platform.contracts import DataStatus
  tools.py -> from vexer_platform.contracts import DataStatus

Call sites in domain code (excluding tests):
  score_confidence 0 · PostgresStore 0 · record_event 0 · new_ulid 0 · stable_id 0
  VexerError 0 · ErrorCode 0 · assert_schema_supported 0 · ProvenanceRecord 0
  ProvenanceClass 0 · ValidityWindow 0 · load_kernel_config 0 · content_hash 0
  idempotency_key 0

append_audit / verify_audit_chain call sites outside tests: 0
logging / structlog / prometheus / opentelemetry imports: 0
broker / queue / publish / consumer / topic references: 0
```

## The eight confidence methodologies (none documented, none using conf-v1)

| # | Location | Formula | Problem |
|---|---|---|---|
| 1 | `agent_orchestrator/agents.py:111,149,178` | `confidence_score` from config, defaults **0.94 / 0.91 / 0.96** | Constant regardless of whether any evidence exists |
| 2 | `agent_orchestrator/orchestrator.py:296` | arithmetic **mean** of the three above | Reports 0.937 for a workflow with zero evidence; a mean of constants is not a confidence |
| 3 | `business_market_intelligence/service.py:98-100` | `min(0.9 + len(signals) * 0.01, 0.99)` | **Confidence rises with signal count, not quality.** Ten low-quality signals beat one strong one |
| 4 | `opportunity_risk_intelligence/service.py:83-85` | `min(0.9 + len(requirements) * 0.01, 0.99)` | Same anti-pattern; a longer requirement list inflates confidence |
| 5 | `business_market_intelligence/models.py:63` | `credibility_score = 0.7 + 0.3 * relevance` | Conflates *relevance to a query* with *credibility of a source* — two different axes |
| 6 | `business_market_intelligence/analytics.py:61-62` | `relevance += 0.3` per matched tag, clamped | Keyword counting presented as a score |
| 7 | `legal_regulatory_ip_intelligence/analytics.py:78,137,210,211` | `compliance_score`, `conflict_score`, `ip_conflict_score`, `legal_risk` | Four more scales, none comparable to the others |
| 8 | `vexer_platform/confidence.py` | `conf-v1` (the only documented model) | **Never called.** Unreachable from the running system |

Live confirmation: the UAE workflow reported `confidence 0.937` — the exact arithmetic mean of three
hardcoded constants — while `graph_context` and `document_context` were empty because Person A's
domains do not exist.

## Hardcoded intelligence still shipping

`agent_orchestrator/agents.py:104-105` returns constant strings as market analysis for **every**
query:

```python
"competitive_posture": "High demand in federal and high-assurance sovereign AI sectors.",
"market_growth_vector": "Accelerating adoption of sovereign AI pipelines with strict provenance.",
```

Line 108 then appends `"Person A Knowledge Graph"` to the citations — **citing a graph that does not
exist**. A decision report that quotes those strings is a fabricated answer with a 0.94 confidence
attached.

## Failure, resilience and concurrency

| Gap | Evidence | Consequence |
|---|---|---|
| **No retries on agent execution** | `orchestrator.py:35` docstring claims "retry tolerance"; no retry code exists in any `execute_*` node | One transient LLM/network error fails the whole workflow; the HITL gate converts it into a human review of a failed run |
| **No timeouts except the LLM client** | `llm_engine.py:96,121` set `timeout_seconds`; nothing else does | A hung domain call blocks the thread forever. FastAPI runs sync endpoints in a threadpool — 20 hung calls exhaust it |
| **`/workflows/start` is not idempotent** | `api.py:90` `session_id = req.session_id or str(uuid.uuid4())` | A client retry after a network timeout creates a **second** workflow and pays for a second LLM call. No idempotency key on any write endpoint |
| **Error classification bypassed in the tool bus** | `bus.py:guarded` catches bare `Exception` → `FAILED` | An auth error, a validation error and a timeout all collapse to one status. No distinction between "retryable" and "will never succeed" |
| **Breaker state is per-process** | `bus.InMemoryCircuitBreakers.distribution == "single-process"` | N workers = N independent circuits. A dead dependency gets hammered once per worker; `health()` says so, but nothing acts on it |
| **D6 imports D7/D8/D9 in-process** | `bus.py` `from business_market_intelligence.service import MarketIntelligenceService` | Not a boundary. One import error or slow import in D7 breaks D6. D6 and D7 cannot be deployed or scaled independently |
| **State accumulates unbounded per workflow** | LangGraph state holds every task output and the full report; SQLite checkpointer persists it | Memory grows with workflow size × concurrency. At 8 GB this is the first hard ceiling |

## Provenance and traceability — not delivered

The *capability* exists and is well modelled: `ProvenanceRecord` with a stage DAG, `provenance_edge`
for index-assisted lineage walks, a recursive-CTE `lineage()`, hash-chained `audit_log`. **None of it
runs.** `append_audit` has zero non-test call sites, so `audit_log` is permanently empty and the only
real audit is `InMemoryAuditSink` — a bounded ring buffer that evaporates when the process dies.

Concretely, a decision cannot currently be traced: the market leg's provenance is the literal string
`"Person A Knowledge Graph"`, its confidence is a config constant, and no `Evidence` or
`ProvenanceRecord` is ever created. **The system cannot answer "where did this come from?" for any
output it produces.**

## Observability — absent

Zero `logging` imports, zero metrics, zero tracing across all four domains. The only introspection is
a `/health` endpoint returning static strings and an in-memory audit list. You cannot measure
latency, error rate, throughput or queue depth; you cannot debug a production incident. Nothing here
can meet an SLA or support on-call.

## Round 2 — findings that need their own section

### A. The test suite certifies code the product does not use

```
agent_orchestrator/tests                 44 tests   live code
business_market_intelligence/tests       12         live code
opportunity_risk_intelligence/tests      12         live code
legal_regulatory_ip_intelligence/tests   11         live code
root tests/ (all target vexer_platform)  83         UNREACHABLE CODE
                                        ──
                                         162 total  ->  51% dead
```

Worse than useless in one specific way: those 83 tests are the reason "218 passed" was reported as
evidence of a working persistence layer. They pass, and they are true — about code that never runs.
A green suite is currently evidence of nothing for the platform layer.

**Required:** a test that fails if the platform stays disconnected — e.g. assert that a live
`/workflows/start` produces a `ProvenanceRecord` and an `Event` row. That single test converts 83
passing unit tests into a load-bearing contract.

### B. The LLM layer: a keyword matcher wearing an AI label

| Claim | Reality |
|---|---|
| "Multi-provider reasoning adapter supporting OpenAI, Ollama, Anthropic" (`agent_orchestrator/README.md:8`) | 2 of 3 reachable. **`ANTHROPIC` is a dead branch** — `generate()` at `llm_engine.py:54-59` tests `OPENAI`, then `OLLAMA`, else mock. |
| Default provider is a reasoning model | Default is `MOCK`; `_call_enterprise_mock` matches `llm.mock.decompose_rules` keywords and returns a `thought_process` string **from config**. |
| Verified live: `LLMConfig(provider=ANTHROPIC)` → returns `{"thought_process": "Decomposed multi-domain enterprise inquiry..."}` with mock subtasks. | No error, no warning, no fallback signal. A silent lie to the operator. |

`generate()` must raise on an unsupported/unimplemented provider rather than falling through to a
mock. A mock that is indistinguishable from a model is worse than no model: it makes the product
look intelligent in a demo and wrong in production.

### C. Security posture

| Finding | Detail |
|---|---|
| **All four services ship unauthenticated** | `api.auth.enabled = false` in every config, by default, with ports 8000–8003 bound. `/health` is deliberately open; **every other route is too.** |
| **Auth cannot be toggled without a restart** | `api.py:14,19,20`: `_api_cfg = load_config().get("api", {})` and `_AUTH = [Depends(require_api_key)]` execute at **import time** — the exact P7 defect fixed in `tools.py`, still present in all four API modules. The dependency list is frozen at import. |
| **No rate limiting, no audit of access** | The in-memory audit ring dies with the process; there is no record of who read what. |
| **Keyword matching is safe (verified)** | `legal_.../analytics.py:31` uses `re.escape` + word boundaries, and API inputs are length-bounded (`scope` max 2000, `topic` max 500). No regex injection, no unbounded input. |
| **Prompt boundary does not exist yet** | Not a current vulnerability (the LLM is a mock), but it becomes one the moment crawled web text from Person A's domain 2 reaches a real model. Content must be fenced and provenance-tagged *before* that integration lands, not after. |

### D. Configuration fragmentation

Four copy-pasted `config.py` modules — each with its own `load_config`, `@lru_cache(maxsize=4)`,
`DEFAULT_CONFIG_PATH`, `reload_config()` and env-var namespace (`VEXER_CONFIG_PATH`,
`VEXER_MBI_CONFIG_PATH`, `VEXER_ORI_CONFIG_PATH`, `VEXER_LRI_CONFIG_PATH`). Meanwhile the shared
`vexer_platform.config.load_kernel_config` — profiles, fail-fast validation, `PlatformProfile` — sits
with **zero call sites**. The platform already solved configuration; the domains did not use it.

### E. Module-global mutable state still present in the domains

P7 removed the process-global breakers and audit list from `tools.py`. The same pattern survives
elsewhere:

* `business_market_intelligence/sources.py:16` — `_external_provider: Optional[Any] = None`, mutated by
  `set_external_provider()`. Not thread-safe; two concurrent requests can observe each other's
  provider, and the audit trail cannot say which provider produced a given signal.

Every one of these should be constructor-injected, as `ToolBus` now is.


## What is genuinely sound (kept, not rewritten)

* `vexer_platform/persistence/` — the schema, the partitioned `event` table, the global `event_dedup`
  idempotency mechanism, the contract↔column mapping, and the 94 parity tests. Correct and reusable.
* `vexer_platform/temporal.py`, `ids.py`, `errors.py`, `context.py` — small, tested, well documented.
* The breaker/audit runtime in `bus.py` (post-P7).
* The four domain analytics modules are clean, tested and deterministic.

The problem is not the quality of these pieces. It is that **they are not connected to anything.**



---

# Target architecture

## 1. One canonical model

```
vexer_platform/contracts.py     ← THE model. 8 contracts, 1 source of truth.
        ▲            ▲
   Person A      Person B
   (verifies,    (computes)
    extracts)      │
        │          ▼
        │   D6/D7/D8/D9 emit contracts ──► ingest ──► correlate ──► persist
        │                                     │         │           │
        │                                  broker    graph      Postgres
        │                                     │         │           │
        └──────────── evidence + confidence ────┴─────────┴───────────┘
```

**Rule that makes it hold: a domain may not define a score or an entity that the platform stores.**
D7's `MarketIntelligenceReport` becomes a producer of `IntelligenceSignal` + `ProvenanceRecord`, not a
parallel schema.

## 2. Single canonical confidence model

One model, two layers, one owner each:

| Layer | Owner | Responsibility | API |
|---|---|---|---|
| **Platform definition** | domain 10 (Person B) | The *methodology*: which inputs exist, how they combine, what each term means, the version | `score_confidence(inputs) -> ConfidenceScore` |
| **Evidence-layer computation** | domain 5 (Person A) | Supplies the *per-observation* inputs from verified evidence | `EvidenceObservation` list → `ConfidenceInputs` |

```python
# vexer_platform/confidence.py — platform definition (already implemented, currently unused)
score_confidence(
    *,
    evidence: list[EvidenceObservation],   # domain 5 computes these
    source_reliabilities: dict[str, float],
    corroboration: float,                  # independent sources agreeing
    freshness_days: float,
    model_confidence: float | None,        # only if an LLM contributed
    contradictions: int,
) -> ConfidenceScore                        # value + components + rationale + method_version
```

**Every one of the eight formulas in the table above is deleted.** `relevance` is not confidence and
must not be added to it — relevance answers "is this about my query?", confidence answers "can I trust
this?" Keep them as separate, separately-named fields. Signal *count* is not evidence quality and
must never increase confidence.

A `ConfidenceScore` must carry `method_version="conf-v1"` so a stored number stays interpretable years
later. A score whose method is unknown is not evidence.

## 3. The event pipeline — exact components required

All four are missing. Minimum viable set, in build order:

1. **Event model** — `vexer_platform.contracts.Event` already exists and is enforced
   (`assert_schema_supported`, `schema_version`, aware-UTC timestamps). Needs a `topic` field and a
   `key` (entity id / partition key) for ordering.
2. **Transport** — Kafka (KRaft) or Redpanda via `aiokafka`. Topics: `vexer.raw`, `vexer.normalized`,
   `vexer.evidence`, `vexer.signals`, `vexer.dlq`. Partition by entity id so per-entity ordering holds.
3. **Producer** — an `EventPublisher` **port**. D6/D7/D8/D9 depend on the port, not on Kafka, so
   tests substitute an in-memory double *at the port boundary* while production uses the real client.
   (A seam is legitimate; a fake that replaces the system is not.)
4. **Consumer** — `ingest → validate → dedup → correlate → enrich → persist`, each step its own
   handler with its own error budget. Dedup reuses the `event_dedup` mechanism **that already exists
   and is already correct**.
5. **Backpressure + DLQ** — bounded consumer lag with a pause/resume threshold; a poison message goes
   to `vexer.dlq` after N attempts with the reason attached. Never an infinite retry.
6. **Replay** — rebuild any entity's state by replaying its partition from `vexer.raw`. This is the
   only mechanism that makes correlation reproducible.

## 4. Entity linking — exact structure

There is currently **no entity layer at all**: `Entity`, `Relationship`, `CausalLink` and `Claim` have
zero references in domain code, and there is no resolution, linking or dedup logic anywhere.

```
source mention ──► mention extraction (domain 5)
                 └─► candidate generation   (alias table + trigram + embedding)
                     └─► blocking            (deterministic key; O(n) not O(n²))
                         └─► scoring         (conf-v1, not a private float)
                             └─► merge / link / new
                                 └─► Entity + ProvenanceRecord(derived_from=mention_ids)
```

Mandatory properties: a **deterministic** `stable_id` (already in `ids.py`, UUIDv5 over a canonical
key) so the same entity always lands on the same row; every link decision records the score **and the
candidates it beat**; `valid_from/valid_until` so a wrong merge is superseded, not deleted. Relations
go in `relationship` with `supersedes` — never an in-place update.

## 5. Service boundary

Replace the in-process imports in `bus.py` with clients:

```python
# today — not a boundary
from business_market_intelligence.service import MarketIntelligenceService

# target — the bus holds a typed client per capability
bus.service("market")   # -> MarketClient(base_url=..., timeout=..., breaker=...)
```

Same `ToolBus` API and same `data_status` semantics, but the transport becomes HTTP/gRPC. That is what
makes D6 and D7 independently deployable, independently scalable and independently failable — and
what makes "distributed" true rather than a folder count.

## 6. Minimum viable observability

No logging, metrics or tracing exists. Smallest stack that makes the system operable:

| Signal | Implementation | Why not optional |
|---|---|---|
| Structured logs | `structlog` JSON to stdout, one event per request, `request_id`/`correlation_id`/`trace_id` from `vexer_platform.context` (already written, unused) | Without it a single request cannot be reconstructed |
| Health depth | `/health/live` (process up) vs `/health/ready` (DB + broker reachable) | Conflating them causes restart storms |
| Metrics | `prometheus-client`: request count/latency by route, tool calls by `provider` + `data_status`, breaker state, DB pool saturation, consumer lag | The `data_status` distribution measures **how much output is real** — the key business signal here |
| Traces | OpenTelemetry, W3C `traceparent` — plumbing in `context.py` already exists | Cross-service correlation the moment D6→D7 is a network call |
| Audit | Route `InMemoryAuditSink` → `PostgresAuditSink` (hash-chained `audit_log`) | Closes §45; the sink is an interface that already exists |

## 7. Performance and the 8 GB ceiling

| Bottleneck | Detail | Mitigation |
|---|---|---|
| **Per-workflow state growth** | LangGraph state holds every task output and the full report; SQLite persists all of it | Cap stored outputs; stream large payloads to object storage, keep references in state |
| **Threadpool exhaustion** | Sync FastAPI endpoints; a hung domain call blocks a thread forever | Mandatory per-call timeouts + async endpoints |
| **N+1 domain calls** | One synchronous in-process call per tool, per task, per workflow | Batch; the persistence layer already uses `executemany`, the domain layer does not |
| **Service churn** | `MarketIntelligenceService()` is constructed on every call | Make services process-level singletons (they are documented stateless) |
| **Unbounded checkpoint state** | Audit ring is bounded (good); LangGraph checkpointer is not | Retention policy for checkpoints; partition drop already specified for `event` |


---

# Priority roadmap

Seven steps, ordered by what **unlocks full system integration** — not by what is most fun to build.
Steps 1–3 are the credibility repair: until the platform layer is load-bearing, every other investment
is decoration. Steps 4–7 are the capabilities that only make sense once it is.

### 1. Make the kernel load-bearing (highest leverage, smallest change)
Replace the hardcoded `agents.py:104-105` strings and the three config confidence constants with real
computation: D7/D8/D9 results become `vexer_platform.contracts.IntelligenceSignal` +
`ProvenanceRecord` + `Evidence`, and D6 **persists them** via `PostgresStore.record_event`.
**Ship one integration test with it** — a live `/workflows/start` must produce a `ProvenanceRecord`
and an `Event` row. That single test is what stops 51% of the suite testing dead code.
*Unlocks: the single correctness problem (steps 2–3 need it), provenance, persistence, replay, and a
suite that means something.*


### 2. Kill the eight confidence formulas; adopt conf-v1 as the only model
Delete `base_conf + len(signals) * 0.01` and its five siblings. Route every score through
`score_confidence()` with `method_version="conf-v1"`. `relevance` stays a separate field. Remove the
arithmetic mean in `orchestrator.py:296` — it is not a confidence. Success test: a query with zero
evidence reports low confidence; adding corroborating sources raises it; a contradiction lowers it.
*Unlocks: trust in every number the system emits. Do this immediately after step 1.*

### 3. Close the audit gap (one interface, one wiring change)
Implement `PostgresAuditSink` against the existing `append_audit` and route `ToolBus` records to it.
Success test: `audit_log` is non-empty in a live run and `verify_audit_chain()` passes.
*Unlocks: §45, the governance story, and the tamper-evidence already built but unreachable.*

### 3a. Fix the two "lies" before anything else (hours, not days)
`generate()` must **raise** on `ANTHROPIC` (unimplemented) rather than silently returning the mock;
and every "supports Anthropic" claim in the docs must be corrected. Separately: stop shipping
`auth.enabled = false` as a default for anything reachable, and move `_api_cfg` / `_AUTH` out of
import time in all four `api.py` files so auth can be turned on without a code change.
*Unlocks: operator trust. A system that silently substitutes a mock for a model, or serves
unauthenticated by default, will be trusted in exactly the situations where it must not be.*

### 4. Idempotency + timeouts + bounded retries on the execution path
Require an idempotency key on `POST /workflows/start` (reject a replay or return the original
workflow); mandatory per-call timeouts on every tool and domain call; bounded retry with backoff and
jitter on *retryable* error codes only, honouring `ErrorCode.retryable` instead of catching bare
`Exception`. Success test: a client retry never creates a second workflow; a hung domain call cannot
exhaust the threadpool.
*Unlocks: safe retries, and the failure tests the mandate requires.*

### 5. Structured logging + health depth + core metrics
`structlog` with `request_id`/`correlation_id`/`trace_id` from the already-written `context.py`; split
`/health/live` from `/health/ready`; expose the `data_status` distribution, breaker states, DB pool
saturation and request latency. Success test: one request is reconstructable end to end from logs
alone, and you can answer "how much of today's output is real?".
*Unlocks: debuggability, SLAs, and the failure/load testing in step 7.*

### 6. Real event pipeline on a real broker
Build the four missing components: publisher port → Kafka/Redpanda → consumer
(`ingest → validate → dedup → correlate → enrich → persist`) → DLQ + bounded lag. Dedup reuses the
existing `event_dedup`. Success test: a redelivered message produces exactly one row; a poison message
lands in `vexer.dlq` with a reason; a partition replays to identical state.
*Unlocks: async ingestion, backpressure, replay, and Person A's domains feeding real data.*

### 7. Replace in-process imports with real service clients
Convert `bus.py`'s direct `MarketIntelligenceService()` / `OpportunityRiskService()` /
`LegalIntelligenceService()` imports into HTTP/gRPC clients behind the same `ToolBus` API, then add
the entity-linking layer (mention → candidates → blocking → conf-v1 scoring → merge/link) against the
`Entity`/`Relationship` tables. Success test: D6 and D7 deploy and fail independently.
*Unlocks: genuine distribution, independent scaling, and the shared knowledge graph.*

## What is deliberately NOT on this list

* **A broker before step 1.** Events with no producer that emits real contracts is infrastructure for
  its own sake — the exact failure mode this review is about.
* **Qdrant / OpenSearch / ClickHouse.** Selected but unimplemented; §23 forbids adding a store before
  its workload exists, and the workloads do not exist yet.
* **A dashboard.** Useless until step 5 produces the metrics to render.
* **More domain logic in D7/D8/D9.** Their analytics are adequate. The problem is not what they
  compute; it is that nothing they compute is trusted, stored or traceable.

