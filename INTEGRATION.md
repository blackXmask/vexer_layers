# Integration Contracts — Person A ↔ Person B

Person B's domains (6–9) are complete and self-contained. They consume Person A's domains
(1, 2, 4, 5) through **lazy, optional adapters**. Nothing breaks if a domain is absent.

**The contract is duck-typed on purpose**: Person A can name packages/classes anything as long
as the service object exposes the method with the right signature and returns plain dicts.

---

## The universal call path

Every cross-domain call in `agent_orchestrator/tools.py` follows the same sequence:

```
RBAC check  →  circuit breaker  →  lazy import  →  service call  →  audit record
                    ↓ (fail / open / missing)
             built-in mock fallback   (provider recorded as builtin-* in the audit trail)
```

Toggles live in `agent_orchestrator/orchestrator_config.json` → `tools.integration`:

```json
"integration": {
  "enable_domain7": true,   // own domain - market intelligence
  "enable_domain8": true,   // own domain - opportunity & risk
  "enable_domain9": true,   // own domain - legal / IP
  "enable_domain1": false,  // Person A - organizational intelligence
  "enable_domain4": false,  // Person A - document & knowledge intelligence
  "enable_domain5": false,  // Person A - knowledge graph
  "signal_limit": 6
}
```

Set a flag to `true` and the adapter activates **only if the package is importable**;
otherwise it silently falls back. No restart-ordering problems.

---

## Domain 5 — Knowledge Graph & Relationship Intelligence

**Expected import:** `knowledge_graph.service` → `KnowledgeGraphService`

| Method | Signature | Must return |
|---|---|---|
| `query_entity` | `(entity_name: str) -> dict` | `{"entity": str, "relationships": [{"relation": str, "target": str}], "verified_facts": [{"fact": str, "confidence": float}]}` |
| *(optional)* `get_neighbors` | `(entity_name: str, depth: int = 1) -> list[dict]` | list of neighbour dicts |

**Consumer:** `ToolRegistry.query_knowledge_graph()` (agent: `MARKET_INTELLIGENCE`).
Fallback today: static relationship mock in `tools.py`.

---

## Domain 4 — Document & Knowledge Intelligence

**Expected import:** `document_intelligence.service` → `DocumentIntelligenceService`

| Method | Signature | Must return |
|---|---|---|
| `search` | `(query: str, limit: int = 5) -> dict` | `{"documents": [{"title": str, "snippet": str, "source": str, "score": float}]}` |

**Consumer:** `ToolRegistry.search_documents()` (agents: `SUPERVISOR`, `MARKET_INTELLIGENCE`).
Fallback today: empty-result stub — agents continue normally, audit records `builtin-fallback`.

---

## Domain 1 — Organizational Intelligence

**Expected import:** `organizational_intelligence.service` → `OrganizationalIntelligenceService`

| Method | Signature | Must return |
|---|---|---|
| `get_company_context` | `(topic: str) -> dict` | `{"capabilities": [str], "certifications": [str], "products": [str], "evidence": [str]}` |

**Consumer:** `ToolRegistry.query_company_context()` (agent: `MARKET_INTELLIGENCE`).
Purpose: grounds opportunity answers in what the company can actually do.
Fallback today: static capability stub.

---

## Domain 2 — External Intelligence & OSINT

Domain 7 already has an OSINT surface (`query_osint_signals`). Domain 2 plugs in **as a source
provider** rather than a direct call.

**Expected import:** `osint_intelligence.service` → `OSINTIntelligenceService`

| Method | Signature | Must return |
|---|---|---|
| `collect` | `(topic: str, limit: int = 10) -> list[dict]` | `[{"title": str, "body": str, "published": str, "url": str, "tags": [str], "source_name": str}]` |

**Two ways to wire it in:**

1. **Config (recommended)** — `business_market_intelligence/config.json`:
   ```json
   "sources": { "domain2": { "enabled": true, "label": "Live OSINT (Domain 2)" } }
   ```
   Domain 7 imports `osint_intelligence.service` lazily and prefers its `collect()` output
   when the module is present. Static sources remain as the fallback.

2. **In-process injection** (advanced):
   ```python
   from business_market_intelligence.sources import set_external_provider
   set_external_provider(my_osint_service)   # any object with .collect(topic, limit)
   ```

---

## Checklist for Person A

- [ ] Package importable from repo root (same venv, no extra install step)
- [ ] Service class exposed as documented, methods return **plain JSON-safe dicts/lists**
      (no Pydantic enums / custom objects — they would break LangGraph checkpointing)
- [ ] Methods are **side-effect free and fast** (< 50 ms typical); the orchestrator has no
      per-call timeout, so long-running crawls should be pre-computed or served from cache
- [ ] Flag enabled in `orchestrator_config.json` → `tools.integration`
- [ ] Verify: `.venv\Scripts\python -m pytest -v` still green, then check
      `GET /audit/tools` on port 8000 — `arguments.provider` should read `domain1` / `domain4`
      / `domain5` / `domain2` instead of `builtin*`

## Compatibility rules (important)

1. **Return JSON-safe values only** — `str`, `int`, `float`, `bool`, `list`, `dict`.
   Enums break strict msgpack checkpointing (already hit and fixed once in Domain 9).
2. **Never raise on empty results** — return an empty list/dict; raising triggers the fallback.
3. **Keep the legacy keys** if extending: Domain 6 reads `capability_gaps`, `fit_score`,
   `recommendation`, `requires_human_signoff`, `detected_regulations`, `source`, `headline`.
   Extra keys are welcome — they flow straight into the agent output and audit records.
