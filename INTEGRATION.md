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
  "enable_market": true,            // own domain 7 - business & market intelligence
  "enable_opportunity": true,       // own domain 8 - opportunity, risk & requirements
  "enable_legal": true,             // own domain 9 - legal, regulatory & IP
  "enable_org": false,              // Person A domain 1 - organizational intelligence
  "enable_documents": false,        // Person A domain 3 - document & knowledge intelligence
  "enable_knowledge_graph": false,  // Person A domain 4 - knowledge graph & relationships
  "signal_limit": 6
}
```

> **Flags are named for the capability, not a domain number.** They used to be
> `enable_domain1/4/5/7/8/9`, which became misleading once the project was renumbered into ten
> domains: old `domain4` meant Document & Knowledge while new "4" is Knowledge Graph, so a log line
> would be misread by anyone holding the current map. The old names are still honoured by
> `bus._flag()` during migration (the new name wins when both are present), and a test fails if a
> legacy key reappears in the shipped config.

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
Without Domain 5 the tool returns `relationships: []` and `data_status: UNAVAILABLE` — never invented
relationships. A labelled demo payload exists behind `tools.allow_demo_data` (off by default).

---

## Domain 4 — Document & Knowledge Intelligence

**Expected import:** `document_intelligence.service` → `DocumentIntelligenceService`

| Method | Signature | Must return |
|---|---|---|
| `search` | `(query: str, limit: int = 5) -> dict` | `{"documents": [{"title": str, "snippet": str, "source": str, "score": float}]}` |

**Consumer:** `ToolRegistry.search_documents()` (agents: `SUPERVISOR`, `MARKET_INTELLIGENCE`).
Without Domain 4 the tool returns `documents: []` and `data_status: UNAVAILABLE`; the audit records
`provider: unavailable:documents`. Documents are evidence, so they are never invented.

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
      `GET /audit/tools` on port 8000 — each record's `provider` should read `org` / `documents`
      / `knowledge_graph` / `osint` and its `data_status` should be `LIVE`, rather than the
      `unavailable:*` / `FAILED` values seen while the seam is absent

## Compatibility rules (important)

1. **Return JSON-safe values only** — `str`, `int`, `float`, `bool`, `list`, `dict`.
   Enums break strict msgpack checkpointing (already hit and fixed once in Domain 9).
2. **Never raise on empty results** — return an empty list/dict; raising triggers the fallback.
3. **Keep the legacy keys** if extending: Domain 6 reads `capability_gaps`, `fit_score`,
   `recommendation`, `requires_human_signoff`, `detected_regulations`, `source`, `headline`.
   Extra keys are welcome — they flow straight into the agent output and audit records.
