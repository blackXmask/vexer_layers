"""
Agent Orchestrator Models and State Definitions.
"""
from enum import Enum
from typing import Annotated, Any, Dict, List, Optional
from pydantic import BaseModel, Field


class AgentRole(str, Enum):
    SUPERVISOR = "SUPERVISOR"
    MARKET_INTELLIGENCE = "MARKET_INTELLIGENCE"
    OPPORTUNITY_RISK = "OPPORTUNITY_RISK"
    LEGAL_REGULATORY = "LEGAL_REGULATORY"
    HUMAN_REVIEWER = "HUMAN_REVIEWER"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class SubTask(BaseModel):
    task_id: str
    target_agent: AgentRole
    description: str
    sequence: int = 0  # planner order; the router executes the lowest pending sequence first
    status: TaskStatus = TaskStatus.PENDING
    input_data: Dict[str, Any] = Field(default_factory=dict)
    output_data: Optional[Dict[str, Any]] = None
    confidence_score: float = 0.0
    citations: List[str] = Field(default_factory=list)


class DecisionArtifact(BaseModel):
    artifact_id: str
    title: str
    summary: str
    recommendations: List[str] = Field(default_factory=list)
    risk_level: str = "LOW"  # LOW, MEDIUM, HIGH, CRITICAL
    requires_human_approval: bool = False
    is_approved: Optional[bool] = None
    approval_notes: Optional[str] = None


class DecisionReport(BaseModel):
    """Flat, machine-readable decision contract produced by synthesis:
    {query, market, risk, legal, decision}."""
    query: str
    market: str = ""
    risk: str = ""
    legal: str = ""
    decision: str = ""
    decision_reason: str = ""
    confidence: float = 0.0
    #: ``measured`` | ``mixed`` | ``none`` — distinguishes a computed confidence from one that rests
    #: on priors or heuristics. A consumer must be able to tell a measurement from a default.
    confidence_basis: str = "none"
    citations: List[str] = Field(default_factory=list)
    requires_human_approval: bool = False
    contributor_agents: List[str] = Field(default_factory=list)
    #: Capability keys that actually answered (``market``, ``legal``, ``knowledge_graph``, ...).
    #: Before this existed the report could not say where its content came from.
    providers: List[str] = Field(default_factory=list)
    #: ``LIVE`` | ``DEGRADED`` | ``UNAVAILABLE`` — the shared DataStatus vocabulary (§19), applied
    #: to the report as a whole.
    data_status: str = "UNAVAILABLE"
    #: Explicit, itemised list of what was missing or degraded. An absent gap is indistinguishable
    #: from a complete analysis; this makes it visible.
    evidence_gaps: List[str] = Field(default_factory=list)


class AgentMessage(BaseModel):
    sender: str
    recipient: str
    content: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


def append_messages(existing: List[AgentMessage], new_items: List[AgentMessage]) -> List[AgentMessage]:
    return existing + new_items


def update_tasks(existing: Dict[str, SubTask], new_items: Dict[str, SubTask]) -> Dict[str, SubTask]:
    updated = dict(existing)
    updated.update(new_items)
    return updated


class OrchestratorState(BaseModel):
    session_id: str
    user_query: str
    plan: List[str] = Field(default_factory=list)
    tasks: Annotated[Dict[str, SubTask], update_tasks] = Field(default_factory=dict)
    active_agent: Optional[str] = None
    next_step: Optional[str] = None
    messages: Annotated[List[AgentMessage], append_messages] = Field(default_factory=list)
    artifacts: List[DecisionArtifact] = Field(default_factory=list)
    decision_report: Optional[DecisionReport] = None
    pending_human_approval: bool = False
    human_approved: Optional[bool] = None
    human_feedback: Optional[str] = None
    final_response: Optional[str] = None
    is_completed: bool = False
