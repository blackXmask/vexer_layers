# Vexer Enterprise Intelligence Platform

Multi-agent enterprise intelligence platform. Python 3.12 · LangGraph · FastAPI · Pydantic.
Every domain is config-driven, deterministic, runs fully offline, and is LLM-ready.

---

## 50/50 Ownership Split — 10 domains

| # | Domain | Owner | Status |
|---|---|---|---|
| 1 | Organizational Intelligence | **Person A** | 🔌 seam ready (`enable_org`, see `INTEGRATION.md`) |
| 2 | External Intelligence & OSINT | **Person A** | 🔌 seam ready (D7 `set_external_provider`) |
| 3 | Document & Knowledge Intelligence | **Person A** | 🔌 seam ready (`enable_documents`, see `INTEGRATION.md`) |
| 4 | Knowledge Graph & Relationship Intelligence | **Person A** | 🔌 seam ready (`enable_knowledge_graph`) |
| 5 | Evidence, Verification & Intelligence Analysis | **Person A** | ⬜ not started — **boundary to settle, see `docs/domain-ownership.md` §4** |
| 6 | AI Agents & Agent Orchestration | **Person B** | ✅ working (P7 complete) |
| 7 | Business & Market Intelligence | **Person B** | ✅ working on static sources |
| 8 | Opportunity, Risk & Requirements Intelligence | **Person B** | ✅ working on rule-based analysis |
| 9 | Legal, Regulatory & Intellectual Property Intelligence | **Person B** | ✅ working on static regulation catalog |
| 10 | Enterprise Platform, Security & Governance | **Person B** | 🟡 partial — contracts, persistence, audit, HITL done; event pipeline, identity, observability missing |

**`vexer_platform/` is domain 10's code and a shared dependency of all ten domains.** It owns the
contracts and storage; Person A's domains own the intelligence that fills them. The full boundary
analysis, the contract/intelligence split, and the one open collision are in
**`docs/domain-ownership.md`**.

| Folder | Domain | Port |
|---|---|---|
| `agent_orchestrator/` | 6 — AI Agents & Agent Orchestration | 8000 |
| `business_market_intelligence/` | 7 — Business & Market Intelligence | 8001 |
| `opportunity_risk_intelligence/` | 8 — Opportunity, Risk & Requirements | 8002 |
| `legal_regulatory_ip_intelligence/` | 9 — Legal, Regulatory & IP | 8003 |
| `vexer_platform/` | 10 — Platform, Security & Governance | library |

---

## Setup (from scratch)

```powershell
cd c:\Users\p7inc3\Desktop\vexer
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## Run everything

```powershell
.venv\Scripts\python -m uvicorn agent_orchestrator.api:app --port 8000
.venv\Scripts\python -m uvicorn business_market_intelligence.api:app --port 8001
.venv\Scripts\python -m uvicorn opportunity_risk_intelligence.api:app --port 8002
.venv\Scripts\python -m uvicorn legal_regulatory_ip_intelligence.api:app --port 8003
```

Interactive docs: `http://127.0.0.1:800{0,1,2,3}/docs`

## Test

```powershell
# all 4 domains — 246 pass, 1 skips unless VEXER_PG_TEST_DSN is set
.venv\Scripts\python -m pytest -v

# the suite skips the live-PostgreSQL tests by default; a green run does NOT mean the
# persistence layer is validated. Bring up Postgres to close that gap:
#   docker compose up -d postgres
#   $env:VEXER_PG_TEST_DSN = "postgresql://vexer:vexer_dev_only@127.0.0.1:5432/vexer"
#   .venv\Scripts\python -m pytest -v

# lint (0 findings is the bar)
.venv\Scripts\python -m pyflakes agent_orchestrator business_market_intelligence opportunity_risk_intelligence legal_regulatory_ip_intelligence vexer_platform test_drive.py

# end-to-end integration smoke test: boots all 4 services and asserts the governance
# machinery actually fires over HTTP (HITL gate, UNAVAILABLE labelling, idempotent replay)
powershell -NoProfile -ExecutionPolicy Bypass -File .\smoke_test.ps1
```

---

## How the modules connect

```
                    ┌──────────────────────────────┐
   HTTP /docs ───▶  │  Orchestration (Domain 6)    │  LangGraph state machine
                    │  agent_orchestrator  :8000   │  supervisor → agents → synthesis
                    └───────┬──────────┬───────┬───┘  native interrupt() HITL gate
        ToolRegistry (RBAC + audit + circuit breaker)   SQLite checkpointing
                            │          │       │
              ┌─────────────┘          │       └──────────────┐
              ▼                        ▼                      ▼
   ┌────────────────────┐  ┌──────────────────────┐  ┌────────────────────────┐
   │ Market Intelligence│  │ Opportunity & Risk   │  │ Legal / Regulatory / IP│
   │ Domain 7  :8001    │  │ Domain 8  :8002      │  │ Domain 9  :8003        │
   └────────────────────┘  └──────────────────────┘  └────────────────────────┘
        ▲ optional: D2 OSINT        ▲ D1 company context     ▲ D4 documents
```

Every domain call is: **RBAC check → circuit breaker → lazy import → result → audit record**. A
missing, disabled or failing domain yields an **empty result tagged with its `data_status`**
(`LIVE`/`DEGRADED`/`STALE`/`FALLBACK`/`FAILED`/`UNAVAILABLE`) — never invented data.

Person A's domains (1, 2, 3, 4) plug in through the same adapter pattern — contracts are documented
in **`INTEGRATION.md`**, and the capability-key naming is in **`docs/domain-ownership.md`**.

---

## Decision output (flat contract)

Synthesis aggregates every agent output into one flat, machine-readable decision report
(`DecisionReport`) — the platform's deliverable answer for a query:

```json
{
  "query": "Should we expand to UAE?",
  "market": "Accelerating adoption of sovereign AI pipelines with strict provenance. Competitive posture: High demand in federal and high-assurance sovereign AI sectors.",
  "risk": "Risk level LOW - RFP fit 100.0% (0 gap(s)); bid recommendation: GO",
  "legal": "Compliance FLAGGED - General Data Protection Regulation; human sign-off required: True",
  "decision": "Expand with compliance preparation",
  "decision_reason": "Commercial case is viable but regulatory obligations must be resolved first.",
  "confidence": 0.937,
  "requires_human_approval": true
}
```

| Aspect | Detail |
|---|---|
| Verdict policy | `orchestrator.decision_report.decision_matrix` — precedence-ordered rules, first match wins, `{}` = default. Conditions: `task_failures`, `compliance_status`, `risk_level`, `fit_below` |
| Section text | Templates `market_template` / `risk_template` / `legal_template` (missing agents render `not_run_label`) |
| Planner order | `SubTask.sequence` decides execution order, so the rule-based planner (`llm.mock.decompose_rules`) really controls *which* agents run and in *what* order |
| REST exposure | `decision_report` on `POST /workflows/start` and `POST /workflows/approve` |

---

## Configuration

Each domain owns one JSON file (all inputs editable, no code changes required):

| File | Controls |
|---|---|
| `agent_orchestrator/orchestrator_config.json` | LLM provider/model, supervisor prompt + mock rules, agent enables + confidence, HITL + fail-closed policy, circuit breaker, audit cap, RBAC map, integration toggles, API auth |
| `business_market_intelligence/config.json` | Sources + raw items, sentiment lexicon, scoring weights, competitors, segments, report tuning, API auth |
| `opportunity_risk_intelligence/config.json` | Capability catalog, requirement keywords, scoring thresholds + risk bands, risk register (triggers, likelihood × impact, mitigations, owners), API auth |
| `legal_regulatory_ip_intelligence/config.json` | Regulation catalog (triggers, obligations, penalties), IP landscape, clearance rules, legal-risk weights, API auth |

**Enable real LLM later (config-only, no code change):**
```json
"llm": { "provider": "OPENAI", "model_name": "gpt-4o-mini" }
```
```powershell
$env:VEXER_LLM_API_KEY = "sk-..."
```

**Enable API auth:** set `api.auth.enabled = true` + `api_key` (or env var), send `X-API-Key`.

---

## Security & resilience (implemented)

| Control | Behaviour |
|---|---|
| Fail-closed governance | A failed agent task can never auto-approve — risk escalates and the HITL gate is forced |
| Circuit breaker | Domain failures fall back to built-in mocks; breaker opens after N failures |
| Bounded audit log | In-memory trail capped (`tools.audit.max_records`), oldest trimmed — no memory leak |
| API-key auth + validation | Optional `X-API-Key` on every route except `/health`; Pydantic input bounds |
| Checkpoint safety | Contract mappers emit JSON-safe values → survives strict msgpack serialization |
| RBAC | Per-agent tool permissions; denials are audited |

---

## Workspace layout

```
vexer/
├── agent_orchestrator/                 # orchestration (Domain 6)
├── business_market_intelligence/      # Domain 7
├── opportunity_risk_intelligence/     # Domain 8
├── legal_regulatory_ip_intelligence/  # Domain 9
├── test_drive.py                      # interactive end-to-end demo
├── INTEGRATION.md                     # contracts for Person A's domains (1, 2, 4, 5)
├── requirements.txt / pyproject.toml   # reproducible environment
└── README.md
```

## Running the demos

```powershell
# guided end-to-end demo (asks for human approval in the terminal)
.venv\Scripts\python test_drive.py

# automated live smoke test across all four services (no prompts, exit code 0 = healthy)
powershell -NoProfile -ExecutionPolicy Bypass -File .\smoke_test.ps1
```
