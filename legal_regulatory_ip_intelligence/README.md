# Domain 9: Legal, Regulatory & Intellectual Property Intelligence

Part of the **Vexer Enterprise Intelligence Platform**. Standalone module — connects to **Domain 6 (AI Agents & Agent Orchestration)** through a one-way service adapter with no circular dependencies.

## Architecture

- **`config.json`** — ALL editable inputs: regulation catalog (triggers, obligations, penalties, severity, authority), IP landscape (patents/trademarks with status, owner, expiry, claim topics), clearance rules, legal-risk weights, report tuning, API metadata.
- **`analytics.py`** — Deterministic, explainable legal intelligence:
  - **Word-boundary regulation matching** (`"ai"` matches "AI system", never "risk")
  - Obligation extraction → compliance flags → sign-off policy (HIGH/CRITICAL ⇒ sign-off required)
  - **IP clearance**: topic overlap × status weight × expiry proximity
  - Clearance decision: **CLEAR / REVIEW / BLOCKED**
  - Blended **legal risk** = 0.6 × compliance + 0.4 × IP conflict
- **`service.py`** — `LegalIntelligenceService` facade (compliance, IP clearance, legal risk, report).
- **`api.py`** — FastAPI REST gateway (port **8003**).
- **`models.py`** — Pydantic models incl. `ComplianceAnalysis.to_domain6_legal_check()` contract mapper.

## Domain 6 Integration

`agent_orchestrator/tools.py` lazily imports `LegalIntelligenceService` and delegates `check_legal_compliance()` to Domain 9. Toggle in `agent_orchestrator/orchestrator_config.json`:

```json
"tools": { "integration": { "enable_domain9": true } }
```

- Domain 9 unavailable or disabled → **automatic fallback** to Domain 6's built-in mock.
- RBAC and audit logging always run **before** the integration call.
- Audit records include `"provider": "domain9" | "builtin"`.

## REST Endpoints (port 8003)

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Status + catalog sizes |
| GET | `/regulations` | Regulation catalog |
| GET | `/ip-landscape` | Known patents & trademarks |
| POST | `/compliance/analyze` | Detect regulations, obligations, flags, sign-off |
| POST | `/ip/clearance` | Freedom-to-operate screening `{"topics": [...]}` |
| POST | `/legal/risk` | Blended legal risk score + level |
| POST | `/report` | Full legal + IP report `{"subject": "...", "topics": [...]}` |

## Security & Resilience

- **API-key auth** (`auth.py`): optional `X-API-Key` on every route except `/health`. Enable via `api.auth.enabled = true` + key (or `VEXER_LRI_API_KEY`).
- **Input validation**: scope/subject 2–2000 chars, ≤ 20 topics (200 chars each) — rejected with `422`.
- **Checkpoint safety**: `to_domain6_legal_check()` uses `model_dump(mode="json")`, so enums (e.g. `Severity`) are stored as plain strings and remain serializable under LangGraph strict msgpack mode.

## Running

```bash
# Tests (from repo root)
.venv\Scripts\python -m pytest legal_regulatory_ip_intelligence\tests -v

# API
.venv\Scripts\python -m uvicorn legal_regulatory_ip_intelligence.api:app --reload --port 8003
```

Config override: `VEXER_LRI_CONFIG_PATH` env var → alternate config file.
