"""
FastAPI Gateway exposing Domain 6 AI Agents & Agent Orchestration.
Integrates with SQLite persistent storage and true LangGraph Human-in-the-Loop resume mechanisms.
"""
from collections import OrderedDict
from typing import Any, Dict, Optional
import threading
import time
import uuid
import sqlite3
import os
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from langgraph.types import Command
from langgraph.checkpoint.sqlite import SqliteSaver

from .models import OrchestratorState
from .orchestrator import build_orchestration_graph, build_checkpoint_serde
from .tools import ToolRegistry
from .config import load_config
from .auth import require_api_key

_api_cfg = load_config().get("api", {})

app = FastAPI(
    title=_api_cfg.get("title", "Vexer Enterprise Intelligence Brain - Orchestration Gateway"),
    version=_api_cfg.get("version", "2.0.0"),
    description=_api_cfg.get("description", "Domain 6 AI Agents & Agent Orchestration Engine")
)

# Persistent SQLite database checkpointer for enterprise crash-recovery and auditability
DB_PATH = os.getenv("VEXER_AGENT_DB", _api_cfg.get("database_path", "vexer_agent_checkpoints.db"))
db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
persistent_checkpointer = SqliteSaver(db_conn, serde=build_checkpoint_serde())
compiled_graph = build_orchestration_graph(checkpointer=persistent_checkpointer)


class StartWorkflowRequest(BaseModel):
    query: str = Field(..., min_length=3, max_length=2000,
                       description="Enterprise inquiry (3-2000 characters)")
    session_id: Optional[str] = Field(default=None, max_length=128)
    #: Supplying this makes the call safe to retry: a repeated key replays the original response
    #: instead of starting a second workflow (and paying for a second LLM call).
    idempotency_key: Optional[str] = Field(default=None, max_length=200,
                                           description="Client-generated key for safe retries")


class WorkflowResponse(BaseModel):
    session_id: str
    plan: list[str]
    is_completed: bool
    pending_human_approval: bool
    artifacts: list[Dict[str, Any]]
    decision_report: Optional[Dict[str, Any]] = None
    final_response: Optional[str] = None
    tasks_summary: Dict[str, Any]
    interrupt_details: Optional[Dict[str, Any]] = None
    #: True when this response was replayed from the idempotency registry rather than recomputed.
    replayed: bool = False


class HumanApprovalRequest(BaseModel):
    session_id: str = Field(..., max_length=128)
    approved: bool
    feedback: Optional[str] = Field(default=None, max_length=2000)
    reviewer_id: Optional[str] = Field(default="governance-officer-1", max_length=128)


@app.get("/health")
def health_check():
    return {
        "status": "HEALTHY",
        "domain": "Domain 6: AI Agents & Agent Orchestration",
        "checkpoint_backend": "SQLite-Persistent",
        "active_database": DB_PATH
    }


@app.get("/audit/tools", dependencies=[Depends(require_api_key)])
def get_tool_audit_trail():
    """
    Telemetry and RBAC audit records for tool calls made by agents.

    Each record carries ``provider`` and ``data_status`` so a consumer can tell a live answer from a
    degraded or absent one. The response also states the audit backend's ``distribution``: a
    ``single-process`` audit trail is a per-worker fragment, and saying so is better than letting a
    reader assume it is cluster-wide (§19, §28).
    """
    records = [rec.to_dict() for rec in ToolRegistry.get_audit_trail()]
    return {
        "records": records,
        "count": len(records),
        "bus": ToolRegistry.bus_health(),
    }


#: In-flight request registry for idempotent ``/workflows/start``.
#:
#: Without this, a client that retries after a network timeout gets a *second* workflow: a second
#: thread, a second LLM call, a second bill. With it, a retry of the same ``idempotency_key`` returns
#: the original outcome instead of repeating the work (§21).
#:
#: Bounded and process-local by design: it protects against the realistic failure (one client
#: retrying), not against a distributed race. A multi-process deployment moves this to the same
#: shared store as ``event_dedup`` when the broker lands.
_IDEMPOTENCY_TTL_SECONDS = 900
_IDEMPOTENCY_MAX_ENTRIES = 1024
_idempotency_lock = threading.Lock()
_idempotency_registry: "OrderedDict[str, tuple[float, WorkflowResponse]]" = OrderedDict()


def _idempotency_lookup(key: str) -> Optional[WorkflowResponse]:
    """Return a previous response for ``key``, or ``None``. Expired entries are dropped."""
    now = time.time()
    with _idempotency_lock:
        entry = _idempotency_registry.get(key)
        if entry is None:
            return None
        created, response = entry
        if now - created > _IDEMPOTENCY_TTL_SECONDS:
            _idempotency_registry.pop(key, None)
            return None
        return response


def _idempotency_record(key: str, response: WorkflowResponse) -> None:
    with _idempotency_lock:
        _idempotency_registry[key] = (time.time(), response)
        while len(_idempotency_registry) > _IDEMPOTENCY_MAX_ENTRIES:
            _idempotency_registry.popitem(last=False)


@app.post("/workflows/start", response_model=WorkflowResponse, dependencies=[Depends(require_api_key)])
def start_workflow(req: StartWorkflowRequest):
    """
    Start (or resume) a decision workflow.

    **Idempotency.** Supplying ``idempotency_key`` makes this endpoint safe to retry: a repeated key
    returns the original response (with ``replayed: true``) instead of starting a second workflow.
    Callers that omit it get a fresh ``session_id`` every time — the previous behaviour, stated here
    so it is a documented choice rather than a surprise.
    """
    if req.idempotency_key:
        cached = _idempotency_lookup(req.idempotency_key)
        if cached is not None:
            return cached.model_copy(update={"replayed": True})

    session_id = req.session_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": session_id}}

    initial_state = OrchestratorState(
        session_id=session_id,
        user_query=req.query
    )

    result = compiled_graph.invoke(initial_state, config=config)

    # Check if graph halted at a human-in-the-loop gate
    state_snapshot = compiled_graph.get_state(config)
    interrupt_details = None
    if state_snapshot.tasks and state_snapshot.tasks[0].interrupts:
        interrupt_details = state_snapshot.tasks[0].interrupts[0].value

    tasks_dict = result.get("tasks", {})
    tasks_summary = {
        tid: {
            "agent": t.target_agent.value,
            "status": t.status.value,
            "confidence": t.confidence_score,
            # Why the leg is confident, and what it could not reach.
            "confidence_basis": (t.output_data or {}).get("confidence_basis", "unknown"),
            "data_status": (t.output_data or {}).get("data_status", "UNKNOWN"),
        }
        for tid, t in tasks_dict.items()
    }

    decision_report = result.get("decision_report")

    response = WorkflowResponse(
        session_id=session_id,
        decision_report=decision_report.model_dump() if hasattr(decision_report, "model_dump") else decision_report,
        plan=result.get("plan", []),
        is_completed=result.get("is_completed", False),
        pending_human_approval=result.get("pending_human_approval", False),
        artifacts=[a.model_dump() for a in result.get("artifacts", [])],
        final_response=result.get("final_response"),
        tasks_summary=tasks_summary,
        interrupt_details=interrupt_details
    )
    if req.idempotency_key:
        _idempotency_record(req.idempotency_key, response)
    return response


@app.get("/workflows/{session_id}/trace", dependencies=[Depends(require_api_key)])
def workflow_trace(session_id: str):
    """
    Per-workflow execution trace: which agents ran, what each produced, and what was missing.

    The workflow test suite and the platform README both described a "Trace" endpoint that did not
    exist — this service had only four routes. Without it, a reviewer handed a ``session_id`` had no
    way to see how a decision was reached, which is the minimum for the auditability the platform
    claims.
    """
    config = {"configurable": {"thread_id": session_id}}
    snapshot = compiled_graph.get_state(config)
    values = snapshot.values or {}
    if not values:
        raise HTTPException(status_code=404, detail=f"no workflow found for session {session_id}")

    tasks = values.get("tasks", {}) or {}
    report = values.get("decision_report")
    return {
        "session_id": session_id,
        "query": values.get("user_query"),
        "plan": values.get("plan", []),
        "agents": [
            {
                "task_id": tid,
                "agent": task.target_agent.value,
                "status": task.status.value,
                "confidence": task.confidence_score,
                "confidence_basis": (task.output_data or {}).get("confidence_basis", "unknown"),
                "data_status": (task.output_data or {}).get("data_status", "UNKNOWN"),
                "degraded_sources": (task.output_data or {}).get("degraded_sources", []),
                "citations": task.citations,
            }
            for tid, task in tasks.items()
        ],
        "artifacts": [a.model_dump() for a in (values.get("artifacts") or [])],
        "decision_report": report.model_dump() if hasattr(report, "model_dump") else report,
        "pending_human_approval": values.get("pending_human_approval", False),
        "tool_audit": [rec.to_dict() for rec in ToolRegistry.get_audit_trail()],
    }


@app.post("/workflows/approve", response_model=WorkflowResponse, dependencies=[Depends(require_api_key)])
def approve_workflow(req: HumanApprovalRequest):
    config = {"configurable": {"thread_id": req.session_id}}
    state_snapshot = compiled_graph.get_state(config)

    if not state_snapshot or not state_snapshot.values:
        raise HTTPException(status_code=404, detail="Workflow session not found")

    if not (state_snapshot.tasks and state_snapshot.tasks[0].interrupts):
        raise HTTPException(status_code=400, detail="Workflow is not currently awaiting human approval")

    # Resume the interrupted execution with the human sign-off payload
    resume_payload = {
        "approved": req.approved,
        "feedback": f"Reviewer {req.reviewer_id}: {req.feedback or 'Approved'}"
    }

    result = compiled_graph.invoke(Command(resume=resume_payload), config=config)

    tasks_dict = result.get("tasks", {})
    tasks_summary = {
        tid: {
            "agent": t.target_agent.value,
            "status": t.status.value,
            "confidence": t.confidence_score
        }
        for tid, t in tasks_dict.items()
    }

    decision_report = result.get("decision_report")

    return WorkflowResponse(
        session_id=req.session_id,
        decision_report=decision_report.model_dump() if hasattr(decision_report, "model_dump") else decision_report,
        plan=result.get("plan", []),
        is_completed=result.get("is_completed", True),
        pending_human_approval=result.get("pending_human_approval", False),
        artifacts=[a.model_dump() for a in result.get("artifacts", [])],
        final_response=result.get("final_response"),
        tasks_summary=tasks_summary,
        interrupt_details=None
    )

