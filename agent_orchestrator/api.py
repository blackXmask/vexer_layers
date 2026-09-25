"""
FastAPI Gateway exposing Domain 6 AI Agents & Agent Orchestration.
Integrates with SQLite persistent storage and true LangGraph Human-in-the-Loop resume mechanisms.
"""
from typing import Any, Dict, Optional
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
    """Returns telemetry and RBAC audit records of all tool calls made by agents."""
    return [rec.model_dump() for rec in ToolRegistry.get_audit_trail()]


@app.post("/workflows/start", response_model=WorkflowResponse, dependencies=[Depends(require_api_key)])
def start_workflow(req: StartWorkflowRequest):
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
            "confidence": t.confidence_score
        }
        for tid, t in tasks_dict.items()
    }

    decision_report = result.get("decision_report")

    return WorkflowResponse(
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

