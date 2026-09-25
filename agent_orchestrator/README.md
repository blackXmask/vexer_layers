# Domain 6: AI Agents & Agent Orchestration (Enterprise Intelligence Brain)

Part of the **Vexer Enterprise Intelligence Platform**.

## Architecture & Enterprise Capabilities

Domain 6 serves as the core reasoning, planning, and execution engine of Vexer:
- **Enterprise LLM Gateway (`llm_engine.py`)**: Multi-provider reasoning adapter supporting OpenAI, Ollama, Anthropic, and local Enterprise Mock simulation for testing without API keys.
- **LangGraph Multi-Agent Cyclical State Machine (`orchestrator.py`)**: Stateful routing connecting Supervisor, Market Intelligence, Opportunity & Risk, and Legal & Regulatory agents. Pending subtasks execute in the planner's declared order (`SubTask.sequence`).
- **Flat Decision Report (`DecisionReport`)**: Synthesis emits a single machine-readable `{query, market, risk, legal, decision}` contract (verdict from the config-driven `decision_matrix`) alongside the governance `DecisionArtifact`.
- **True Human-in-the-Loop (HITL) Checkpointing**: Uses native LangGraph `interrupt()` and `Command(resume=...)` to freeze execution in storage whenever high risks or regulatory constraints are detected, awaiting signed governance authorization.
- **Persistent Storage (`SqliteSaver`)**: Survives server restarts; state transitions and interrupted runs are recorded on disk or in PostgreSQL.
- **Sandboxed Tool Registry with RBAC & Audit (`tools.py`)**: Enforces Role-Based Access Controls per agent and records cryptographic-grade execution telemetry for compliance.
- **FastAPI Enterprise Gateway (`api.py`)**: Exposes REST contracts (`/workflows/start`, `/workflows/approve`, `/audit/tools`) connecting with Java Platform (Domain 10) and Web Frontend.

## Security & Resilience (v2.1)

| Control | Where | Behaviour |
|---|---|---|
| **Fail-closed governance** | `orchestrator.py` synthesis | If any agent task FAILS, the workflow **never auto-approves** — risk escalates to `risk_level_on_task_failure` (HIGH) and the HITL gate is forced. Config: `orchestrator.fail_closed_on_task_failure` |
| **Failure isolation** | `tools.py` `_fetch_*` | A crashing domain never breaks a workflow — the built-in mock is served and the audit records `provider: builtin-fallback` |
| **Circuit breaker** | `tools.py` `_CircuitBreaker` | Opens after `failure_threshold` consecutive domain failures, then short-circuits (`provider: builtin-circuit-open`) for `recovery_seconds`. Config: `tools.circuit_breaker` |
| **Bounded audit log** | `tools.py` `_BoundedAuditList` | In-memory audit trail capped at `tools.audit.max_records` (default 1000), oldest trimmed first — no unbounded memory growth |
| **API-key auth** | `auth.py` | Optional `X-API-Key` on every route except `/health`. Enable: `api.auth.enabled = true` + key (or env var) |
| **Input validation** | `api.py` | Pydantic bounds on query/session/feedback length; oversized or malformed input rejected with `422` before any work runs |
| **Checkpoint safety** | `models.py` (each domain) | Contract mappers emit JSON-safe values (`model_dump(mode="json")`) so state stays serializable under strict msgpack mode |

## Configuration (orchestrator_config.json)
All inputs are editable from **`agent_orchestrator/orchestrator_config.json`** — no code changes needed. Restart the service after editing.

| Section | What you can edit |
|---|---|
| `llm` | Provider (`MOCK`/`OPENAI`/`OLLAMA`), model name, temperature, API base/key, timeout |
| `llm.mock` | Mock decomposition **keyword rules** (incl. UAE jurisdiction terms), subtask descriptions, `{{query}}` templates, fallback behavior |
| `supervisor` | Supervisor system prompt, planning message, fallback subtasks |
| `agents` | Per-agent `enabled` toggle, `confidence_score`, citations, risk thresholds, default requirements, governance clauses |
| `orchestrator` | HITL gate prompt, which `risk_levels_requiring_approval` trigger the gate, `always_require_human_approval`, legal sign-off honoring, recommendation texts |
| `orchestrator.decision_report` | Flat decision contract: section templates, precedence-ordered `decision_matrix` (first match wins), `not_run_label`, verdict fallback |
| `tools` | RBAC `permissions` map, RFP fit GO threshold + match keywords, compliance detection keywords + regulations |
| `api` | FastAPI title/version/description, SQLite checkpoint `database_path` |

**Environment variable overrides (highest precedence):**
```bash
VEXER_CONFIG_PATH    # alternate config file
VEXER_LLM_PROVIDER   # MOCK | OPENAI | OLLAMA
VEXER_LLM_MODEL      # model name
VEXER_LLM_API_KEY    # API key (read via llm.api_key_env)
VEXER_AGENT_DB       # checkpoint database path
```

**Switching to a real LLM:**
```json
"llm": { "provider": "OPENAI", "model_name": "gpt-4o-mini" }
```
```bash
$env:VEXER_LLM_API_KEY = "sk-..."   # or set "api_key" directly in the JSON
```

## Running Tests
```bash
.venv\Scripts\python -m pytest agent_orchestrator\tests -v
```

## Running the API Service
```bash
.venv\Scripts\uvicorn agent_orchestrator.api:app --reload --port 8000
```
