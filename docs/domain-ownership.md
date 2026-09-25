# Domain Ownership & Integration Boundaries

> Mandate §11, §53. This file defines who owns what and — more importantly — where the seams are.
> "We each own 5 domains" is only half the problem: the failures in a two-person split come from the
> *boundaries*, and boundaries are decisions, not accidents.
>
> **Person B owns domains 6–10. `vexer_platform/` is domain 10's code and is a shared dependency of
> domains 1–10, not a private implementation detail of domain 10.**

## 1. Ownership map

| # | Domain | Owner | Package in this repo | State |
|---|---|---|---|---|
| 1 | Organizational Intelligence | Person A | *(not present)* | Awaiting Person A |
| 2 | External Intelligence & OSINT | Person A | *(not present)* | Awaiting Person A; injected into D7 via `sources.set_external_provider` |
| 3 | Document & Knowledge Intelligence | Person A | *(not present)* | Awaiting Person A; D6 seam |
| 4 | Knowledge Graph & Relationship Intelligence | Person A | *(not present)* | Awaiting Person A; D6 seam |
| 5 | Evidence, Verification & Intelligence Analysis | Person A | *(not present)* | **Boundary conflict — see §4** |
| 6 | AI Agents & Agent Orchestration | **Person B** | `agent_orchestrator/` | Working; P7 complete |
| 7 | Business & Market Intelligence | **Person B** | `business_market_intelligence/` | Working on static sources |
| 8 | Opportunity, Risk & Requirements Intelligence | **Person B** | `opportunity_risk_intelligence/` | Working on rule-based analysis |
| 9 | Legal, Regulatory & IP Intelligence | **Person B** | `legal_regulatory_ip_intelligence/` | Working; static regulation catalog |
| 10 | Enterprise Platform, Security & Governance | **Person B** | `vexer_platform/` + `migrations/` + `docker-compose.yml` + `deploy/` | Contracts, persistence, audit, HITL done; **event pipeline, identity, observability missing** |

## 2. The contract/infrastructure split (the important one)

Person A's domains 4 and 5 own *intelligence*: entity resolution, relationship extraction, source
verification, confidence scoring, contradiction detection, event correlation.

Domain 10 owns the *contracts and storage* those algorithms produce. `vexer_platform` defines the
shape and the tables; Person A defines the behaviour that fills them.

| Owned by domain 10 (Person B) | Owned by domains 4/5 (Person A) |
|---|---|
| `Entity`, `Relationship`, `CausalLink` **contracts** + their tables | Entity **resolution**, linking, disambiguation |
| `Evidence`, `ProvenanceRecord` **contracts**, `provenance_edge` lineage DAG | Source **verification**, evidence **collection** |
| `conf-v1` confidence **scoring interface** and shape | Confidence **calibration per domain** |
| `Claim` table + `CONTRADICTS` edges + temporal windows | **Contradiction detection and adjudication** |
| Graph traversal queries (AGE / recursive CTE) | **Relationship extraction** from text |

**Rule:** Person A never writes to these tables directly. They produce contracts through their domain
service; domain 10 owns the write path, schema and migrations. Two people running DDL against one
table is how the platform loses its audit guarantees.

## 3. ✅ Collision 1 — RESOLVED: seam keys are now capability names

The renumbering changed what the numbers mean:

* In this codebase the seam keys `domain1`…`domain9` used the **old** numbering, where 6 = Agents,
  7 = Market, 8 = Opportunity, 9 = Legal, and 1/2/4/5 were Person A's seams.
* Under the **new** 10-domain map, Person A owns 1-5 where **3 = Document & Knowledge**,
  **4 = Knowledge Graph**, **5 = Evidence/Verification**; Person B owns 6-10.

Old `domain4` meant *Document & Knowledge* while new "4" is *Knowledge Graph*, and old `domain5`
meant *Knowledge Graph* while new "5" is *Evidence/Verification*. A log line reading `domain5` would
therefore have been misread by anyone holding the current map.

**Applied fix — keys are named for the capability, not the number:**

| Capability key | Old key | Owner | Canonical domain |
|---|---|---|---|
| `org` | `domain1` | Person A | 1 - Organizational Intelligence |
| `osint` | `domain2` | Person A | 2 - External Intelligence & OSINT |
| `documents` | `domain4` | Person A | 3 - Document & Knowledge Intelligence |
| `knowledge_graph` | `domain5` | Person A | 4 - Knowledge Graph & Relationships |
| `market` | `domain7` | Person B | 7 - Business & Market Intelligence |
| `opportunity` | `domain8` | Person B | 8 - Opportunity, Risk & Requirements |
| `legal` | `domain9` | Person B | 9 - Legal, Regulatory & IP |

Config flags renamed to match (`enable_market`, `enable_documents`, `enable_knowledge_graph`, ...).
Names do not drift when the org chart does.

**Migration safety:** `bus.canonical_key()` maps retired key names, and `bus._flag()` honours a
legacy `enable_domain*` flag when the new one is absent (the new name wins when both are present), so
an in-flight branch or config does not silently lose an integration. Two tests lock this in: one
fails if a number-based key reappears, and one fails if a legacy flag returns to the shipped config.

**Deliberately not renamed:** `to_domain6_osint()` / `to_domain6_rfp_fit()` /
`to_domain6_legal_check()` on the domain services. "Domain 6" is still AI Agents & Agent
Orchestration under the new map, so those names remain accurate, and renaming a cross-package
contract mapper would be churn with no clarity gain.

## 4. ⚠️ Collision 2 — evidence/confidence already exist on both sides

`vexer_platform/contracts.py` already defines `Evidence`, `ProvenanceRecord`, `Claim`,
`IntelligenceSignal`, and `confidence.py` implements a `conf-v1` methodology. Person A's domain 5 is
titled "Evidence, Verification & Intelligence Analysis" and lists *Source Verification, Evidence,
Provenance, Confidence, Fact Verification, Contradiction Detection, Event Correlation* — the same
surface area.

This is not a reason to delete either side. It is a reason to agree the split **now**, before two
implementations of "confidence" and two `Evidence` types exist:

* `vexer_platform.confidence` — `conf-v1`: evidence quality × source reliability × agreement ×
  freshness decay × model confidence − contradiction penalty, with a per-component breakdown.
* If Person A also ships a confidence model, the platform ends up with two incomparable numbers and no
  principled way to combine them. §13 requires confidence to have a *defined* methodology; two
  methodologies means neither is authoritative.

**Recommendation:** Person A's domain 5 calls `vexer_platform.confidence.score_confidence()` and
supplies per-domain calibration inputs; it does not define a second scorer. If Person A already has
one, the honest path is a documented reconciliation, not a silent pick.

## 5. What each of your domains still needs (state, not plan)

Stated plainly so no domain is over-claimed:

| Domain | Done | Missing / honest limitation |
|---|---|---|
| 6 Agents | planning, sequential routing, HITL, decision report, tool bus, persistence | No retry/timeout/backoff on agent tasks; `/workflows/start` is not idempotent; `agents.py` still hardcodes `competitive_posture`, `market_growth_vector`, `confidence_score` |
| 7 Market | signals, trends, competitors, ranking, API | Sources are static config records, **not real crawlers** (that is Person A's domain 2 via the seam); scoring is lexicon-based |
| 8 Opportunity | requirement classification, capability matching, risk scoring, API | No real RFP/tender ingestion; matching is keyword/rules; no historical win/loss data |
| 9 Legal/IP | regulation catalog, trigger matching, compliance status, IP clearance | Catalog is a static JSON file, not a monitored legal feed; GDPR triggers are geo-reused for UAE (documented Phase-3 debt) |
| 10 Platform | contracts, temporal model, confidence, errors, context, PG schema + migrations + asyncpg store, hash-chained audit, HITL, Docker | **No event pipeline/broker**, no identity/RBAC-ABAC (per-service API keys only), no observability (OTel/Prometheus), no dashboard/alerts, no Postgres-backed audit sink (audit trail is per-process) |

## 6. Dependency direction

```
        ┌─────────────── Person A (1-5) ───────────────┐
        │ 1 org · 2 osint · 3 docs · 4 graph · 5 evidence │
        └───────┬────────────────────────────┬─────────┘
                │ contracts (Entity/Event/   │ services
                │ Evidence/Claim/Signal)     │ (ToolBus seams)
                ▼                            ▼
        ┌─────────────── Person B (6-10) ───────────────┐
        │ 6 agents ─ 7 market ─ 8 opportunity ─ 9 legal│
        │ 10 platform: contracts · persistence ·       │
        │ governance · identity · observability        │
        └───────────────────────────────────────────────┘
```

Person A depends on domain 10's contracts. Person B's domains 6–9 depend on Person A's services. The
only permitted coupling is through `vexer_platform` contracts (Person A) and the documented `ToolBus`
seam (Person B). The D6↔D7/D8/D9 calls are currently one-way through the bus and should stay that
way; direct package-to-package imports outside those two points are what to watch in review.

