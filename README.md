# Vexer Enterprise Intelligence Platform

Multi-agent enterprise intelligence platform. Python 3.12 · LangGraph · FastAPI · Pydantic.
Every domain is config-driven, deterministic, runs fully offline, and is LLM-ready.

---

## 50/50 Ownership Split

| # | Domain | Owner | Status |
|---|---|---|---|
| 1 | Organizational Intelligence | **Person A** | ⬜ not started |
| 2 | External Intelligence & OSINT | **Person A** | 🔌 seam ready (see `INTEGRATION.md`) |
| 3 | AI Agents & Agent Orchestration | **Person B** | ✅ complete |
| 4 | Document & Knowledge Intelligence | **Person A** | 🔌 seam ready (see `INTEGRATION.md`) |
| 5 | Knowledge Graph & Relationship Intelligence | **Person A** | 🔌 seam ready (see `INTEGRATION.md`) |
| 6 | Evidence, Verification & Intelligence Analysis | **Person A** | ⬜ not started |
| 7 | Business & Market Intelligence | **Person B** | ✅ complete |
| 8 | Opportunity, Risk & Requirements Intelligence | **Person B** | ✅ complete |
| 9 | Legal, Regulatory & Intellectual Property Intelligence | **Person B** | ✅ complete |
| 10 | Enterprise Platform, Security & Governance | **Person B** | 🟡 partial (inside Domain 6) |

| Folder | Domain | Port |
|---|---|---|
| `agent_orchestrator/` | AI Agents & Agent Orchestration | 8000 |
| `business_market_intelligence/` | Business & Market Intelligence | 8001 |
| `opportunity_risk_intelligence/` | Opportunity, Risk & Requirements Intelligence | 8002 |
| `legal_regulatory_ip_intelligence/` | Legal, Regulatory & IP Intelligence | 8003 |

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
# all 4 domains (55 tests)
.venv\Scripts\python -m pytest -v

# lint
.venv\Scripts\python -m pyflakes agent_orchestrator business_market_intelligence opportunity_risk_intelligence legal_regulatory_ip_intelligence test_drive.py

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

Every domain call is: **RBAC check → circuit breaker → lazy import → result → audit record**,
with a built-in mock fallback if the domain is missing, disabled, or failing.

Person A's domains (1, 2, 4, 5) plug in through the same adapter pattern — contracts are
documented in **`INTEGRATION.md`**.

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

# guided end-to-end demo (asks for human approval in the terminal)
.venv\Scripts\python test_drive.py
```
