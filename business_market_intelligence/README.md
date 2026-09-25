# Domain 7: Business & Market Intelligence

Part of the **Vexer Enterprise Intelligence Platform**. Standalone module — connects to **Domain 6 (AI Agents & Agent Orchestration)** through a one-way service adapter with no circular dependencies.

## Architecture

- **`config.json`** — ALL editable inputs: data sources + raw items, sentiment lexicon, scoring weights/thresholds, competitors, market segments, report tuning, API metadata.
- **`sources.py`** — Deterministic source collection (news, tenders, social, patents, analyst). Supports `{{topic}}` templates. Real crawlers (Domain 2) can replace it behind the same interface.
- **`analytics.py`** — Explainable scoring: lexicon sentiment, keyword-overlap relevance, trend momentum/direction/spike detection, competitor threat blending, segment attractiveness.
- **`service.py`** — `MarketIntelligenceService` facade (signals, trends, competitors, segments, full report).
- **`api.py`** — FastAPI REST gateway (port **8001**).
- **`models.py`** — Pydantic models incl. `MarketSignal.to_domain6_osint()` contract mapper.

## Domain 6 Integration

`agent_orchestrator/tools.py` lazily imports `MarketIntelligenceService` and delegates `query_osint_signals()` to Domain 7. Toggle in `agent_orchestrator/orchestrator_config.json`:

```json
"tools": { "integration": { "enable_domain7": true, "signal_limit": 6 } }
```

- Domain 7 unavailable or disabled → **automatic fallback** to Domain 6's built-in mock.
- RBAC and audit logging always run **before** the integration call.
- Audit records include `"provider": "domain7" | "builtin"`.

## REST Endpoints (port 8001)

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Status + enabled sources |
| GET | `/intelligence/signals?topic=&limit=` | Scored signals with sentiment/relevance/provenance |
| GET | `/intelligence/trends?topic=` | Momentum, direction, spike flags |
| GET | `/intelligence/competitors?topic=` | Threat profiles |
| GET | `/intelligence/segments` | TAM/SAM + attractiveness |
| POST | `/intelligence/report` | Full executive report `{"topic": "..."}` |

## Security & Resilience

- **API-key auth** (`auth.py`): optional `X-API-Key` on every route except `/health`. Enable via `api.auth.enabled = true` + key (or `VEXER_MBI_API_KEY`).
- **Input validation**: topic 2–500 chars, `limit` 1–100 — oversized input rejected with `422`.
- **Checkpoint safety**: `to_domain6_osint()` returns JSON-safe values only.

## Running

```bash
# Tests (from repo root)
.venv\Scripts\python -m pytest business_market_intelligence\tests -v

# API
.venv\Scripts\python -m uvicorn business_market_intelligence.api:app --reload --port 8001
```

Config override: `VEXER_MBI_CONFIG_PATH` env var → alternate config file.
