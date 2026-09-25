# Domain 8: Opportunity, Risk & Requirements Intelligence

Part of the **Vexer Enterprise Intelligence Platform**. Standalone module — connects to **Domain 6 (AI Agents & Agent Orchestration)** through a one-way service adapter with no circular dependencies.

## Architecture

- **`config.json`** — ALL editable inputs: capability catalog (name/category/weight/keywords), requirement classification keywords, scoring thresholds & risk bands, **risk register** (triggers, likelihood × impact, mitigations, owners), report tuning, API metadata.
- **`analytics.py`** — Deterministic, explainable scoring:
  - **Word-boundary matching** (keyword `"ai"` matches "AI tender" but never "risk")
  - Requirement classification → category (TECHNICAL/LEGAL/FINANCIAL/OPERATIONAL/GOVERNANCE) + priority (MANDATORY/OPTIONAL)
  - Capability fitting → fit score (unweighted + capability-weighted), GO / CONDITIONAL / NO-GO
  - Risk register triggering → severity = likelihood × impact → LOW/MEDIUM/HIGH/CRITICAL bands
  - Dynamic capability-gap risk (likelihood scales with gap count)
  - Opportunity score = 0.7 × fit + 0.3 × (1 − risk)
- **`service.py`** — `OpportunityRiskService` facade (assess, analyze_risks, parse, report).
- **`api.py`** — FastAPI REST gateway (port **8002**).
- **`models.py`** — Pydantic models incl. `OpportunityAssessment.to_domain6_rfp_fit()` contract mapper.

## Domain 6 Integration

`agent_orchestrator/tools.py` lazily imports `OpportunityRiskService` and delegates `evaluate_rfp_fit()` to Domain 8. Toggle in `agent_orchestrator/orchestrator_config.json`:

```json
"tools": { "integration": { "enable_opportunity": true } }
```

- Domain 8 unavailable or disabled → **automatic fallback** to Domain 6's built-in mock.
- RBAC and audit logging always run **before** the integration call.
- Audit records include `"provider": "opportunity" | "unavailable:opportunity"`.

## REST Endpoints (port 8002)

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Status + catalog/register sizes |
| GET | `/capabilities` | Capability catalog |
| POST | `/requirements/parse` | Classify requirements + match capabilities |
| POST | `/opportunity/assess` | Fit score + GO/NO-GO + breakdown |
| POST | `/risks/analyze` | Triggered risks with bands + mitigations |
| POST | `/report` | Full opportunity+risk report `{"rfp_title": "...", "requirements": [...]}` |

## Security & Resilience

- **API-key auth** (`auth.py`): optional `X-API-Key` on every route except `/health`. Enable via `api.auth.enabled = true` + key (or `VEXER_ORI_API_KEY`).
- **Input validation**: max 100 requirements (500 chars each), title ≤ 500 chars — rejected with `422`.
- **Checkpoint safety**: `to_domain6_rfp_fit()` returns JSON-safe values only.

## Running

```bash
# Tests (from repo root)
.venv\Scripts\python -m pytest opportunity_risk_intelligence\tests -v

# API
.venv\Scripts\python -m uvicorn opportunity_risk_intelligence.api:app --reload --port 8002
```

Config override: `VEXER_ORI_CONFIG_PATH` env var → alternate config file.
