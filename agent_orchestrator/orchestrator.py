"""
LangGraph Multi-Agent Workflow Engine for Domain 6.
Handles State Transitions, Agent Dispatching, Aggregation, and True LangGraph Human-in-the-Loop Interrupts.
"""
from typing import Any, Dict, List, Literal
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from .models import (
    AgentMessage,
    AgentRole,
    DecisionArtifact,
    DecisionReport,
    OrchestratorState,
    TaskStatus,
)
from .agents import (
    SupervisorAgent,
    MarketIntelligenceAgent,
    OpportunityRiskAgent,
    LegalRegulatoryAgent,
)
from .config import load_config, render_template


def supervisor_node(state: OrchestratorState) -> Dict[str, Any]:
    """Plans and decomposes the query into tasks using LLM reasoning if not already planned."""
    if not state.tasks:
        return SupervisorAgent.plan(state)
    return {"active_agent": AgentRole.SUPERVISOR.value}


def execute_market_agent(state: OrchestratorState) -> Dict[str, Any]:
    """Executes any pending tasks assigned to Market Intelligence Agent with retry tolerance."""
    updated_tasks = dict(state.tasks)
    messages = []
    for tid, task in updated_tasks.items():
        if task.target_agent == AgentRole.MARKET_INTELLIGENCE and task.status == TaskStatus.PENDING:
            task.status = TaskStatus.IN_PROGRESS
            try:
                result = MarketIntelligenceAgent.execute(task)
                task.output_data = result["output_data"]
                task.confidence_score = result["confidence_score"]
                task.citations = result["citations"]
                task.status = TaskStatus.COMPLETED
                messages.append(AgentMessage(
                    sender=AgentRole.MARKET_INTELLIGENCE.value,
                    recipient=AgentRole.SUPERVISOR.value,
                    content=f"Market Intelligence completed task {tid}."
                ))
            except Exception as e:
                task.status = TaskStatus.FAILED
                task.output_data = {"error": str(e)}
                messages.append(AgentMessage(
                    sender=AgentRole.MARKET_INTELLIGENCE.value,
                    recipient=AgentRole.SUPERVISOR.value,
                    content=f"Market Intelligence failed task {tid}: {str(e)}"
                ))
    return {"tasks": updated_tasks, "messages": messages, "active_agent": AgentRole.MARKET_INTELLIGENCE.value}


def execute_risk_agent(state: OrchestratorState) -> Dict[str, Any]:
    """Executes any pending tasks assigned to Opportunity & Risk Agent."""
    updated_tasks = dict(state.tasks)
    messages = []
    for tid, task in updated_tasks.items():
        if task.target_agent == AgentRole.OPPORTUNITY_RISK and task.status == TaskStatus.PENDING:
            task.status = TaskStatus.IN_PROGRESS
            try:
                result = OpportunityRiskAgent.execute(task)
                task.output_data = result["output_data"]
                task.confidence_score = result["confidence_score"]
                task.citations = result["citations"]
                task.status = TaskStatus.COMPLETED
                messages.append(AgentMessage(
                    sender=AgentRole.OPPORTUNITY_RISK.value,
                    recipient=AgentRole.SUPERVISOR.value,
                    content=f"Opportunity & Risk completed task {tid}."
                ))
            except Exception as e:
                task.status = TaskStatus.FAILED
                task.output_data = {"error": str(e)}
                messages.append(AgentMessage(
                    sender=AgentRole.OPPORTUNITY_RISK.value,
                    recipient=AgentRole.SUPERVISOR.value,
                    content=f"Opportunity & Risk failed task {tid}: {str(e)}"
                ))
    return {"tasks": updated_tasks, "messages": messages, "active_agent": AgentRole.OPPORTUNITY_RISK.value}


def execute_legal_agent(state: OrchestratorState) -> Dict[str, Any]:
    """Executes any pending tasks assigned to Legal & Regulatory Agent."""
    updated_tasks = dict(state.tasks)
    messages = []
    for tid, task in updated_tasks.items():
        if task.target_agent == AgentRole.LEGAL_REGULATORY and task.status == TaskStatus.PENDING:
            task.status = TaskStatus.IN_PROGRESS
            try:
                result = LegalRegulatoryAgent.execute(task)
                task.output_data = result["output_data"]
                task.confidence_score = result["confidence_score"]
                task.citations = result["citations"]
                task.status = TaskStatus.COMPLETED
                messages.append(AgentMessage(
                    sender=AgentRole.LEGAL_REGULATORY.value,
                    recipient=AgentRole.SUPERVISOR.value,
                    content=f"Legal & Regulatory completed task {tid}."
                ))
            except Exception as e:
                task.status = TaskStatus.FAILED
                task.output_data = {"error": str(e)}
                messages.append(AgentMessage(
                    sender=AgentRole.LEGAL_REGULATORY.value,
                    recipient=AgentRole.SUPERVISOR.value,
                    content=f"Legal & Regulatory failed task {tid}: {str(e)}"
                ))
    return {"tasks": updated_tasks, "messages": messages, "active_agent": AgentRole.LEGAL_REGULATORY.value}



def _risk_rank(level: str) -> int:
    """Severity ordering helper for fail-closed escalation."""
    order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
    return order.get(level, 0)


def _as_list(value: Any) -> List[Any]:
    """Normalises a config scalar-or-list condition value to a list."""
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _render_section(template: str, view: Dict[str, Any], not_run_label: str) -> str:
    """Renders one decision-report section and collapses whitespace.
    Returns `not_run_label` when the contributing agent produced no output."""
    if not view:
        return not_run_label
    return " ".join(render_template(template, view).split())


def _select_decision(
    facts: Dict[str, Any], matrix: List[Dict[str, Any]], default: Dict[str, str]
) -> Dict[str, str]:
    """First-match-wins selection over `orchestrator.decision_report.decision_matrix`.

    Supported conditions: `risk_level`, `compliance_status`, `task_failures`,
    `fit_below` (strictly less-than). A rule with an empty `when` always matches.
    """
    for rule in matrix:
        when = rule.get("when", {}) or {}
        if "risk_level" in when and facts.get("risk_level") not in _as_list(when["risk_level"]):
            continue
        if "compliance_status" in when and facts.get("compliance_status") not in _as_list(when["compliance_status"]):
            continue
        if "task_failures" in when and bool(facts.get("task_failures")) != bool(when["task_failures"]):
            continue
        if "fit_below" in when:
            fit_score = facts.get("fit_score")
            if fit_score is None or not (float(fit_score) < float(when["fit_below"])):
                continue
        return {
            "decision": rule.get("decision", default["decision"]),
            "reason": rule.get("reason", default["reason"])
        }
    return default


def synthesis_and_governance_node(state: OrchestratorState) -> Dict[str, Any]:
    """Synthesizes outputs into a unified Enterprise Decision Artifact.
    Governance policy (which risk levels require sign-off, etc.) comes from orchestrator_config.json."""
    policy = load_config().get("orchestrator", {})
    requires_approval = False
    risk_level = "LOW"
    summaries = []
    failed_tasks: list[str] = []
    contributor_agents: list[str] = []
    confidence_samples: list[float] = []
    report_citations: list[str] = []
    market_view: Dict[str, Any] = {}
    risk_view: Dict[str, Any] = {}
    legal_view: Dict[str, Any] = {}

    for tid, task in state.tasks.items():
        if task.status == TaskStatus.FAILED:
            failed_tasks.append(task.target_agent.value)
        if task.status == TaskStatus.COMPLETED and task.output_data:
            summaries.append(f"[{task.target_agent.value}] {task.description}")
            contributor_agents.append(task.target_agent.value)
            confidence_samples.append(float(task.confidence_score))
            report_citations.extend(c for c in task.citations if c not in report_citations)
            if task.target_agent == AgentRole.MARKET_INTELLIGENCE:
                signals = task.output_data.get("market_signals") or []
                first_signal = signals[0] if signals else {}
                market_view = {
                    "growth": task.output_data.get("market_growth_vector", ""),
                    "posture": task.output_data.get("competitive_posture", ""),
                    "signal": first_signal.get("headline", "") if isinstance(first_signal, dict) else str(first_signal)
                }
            elif task.target_agent == AgentRole.OPPORTUNITY_RISK:
                evaluation = task.output_data.get("evaluation") or {}
                risk_level = task.output_data.get("risk_assessment", {}).get("overall_risk_level", "LOW")
                risk_view = {
                    "level": risk_level,
                    "fit": evaluation.get("fit_score", ""),
                    "gaps": len(evaluation.get("capability_gaps") or []),
                    "bid": task.output_data.get("bid_recommendation", "")
                }
            elif task.target_agent == AgentRole.LEGAL_REGULATORY:
                review = task.output_data.get("regulatory_review") or {}
                legal_view = {
                    "status": review.get("compliance_status", "UNKNOWN"),
                    "regulations": ", ".join(review.get("detected_regulations") or []),
                    "signoff": review.get("requires_human_signoff", False)
                }
                if policy.get("honor_legal_signoff", True) and legal_view["signoff"]:
                    requires_approval = True

    risk_levels_requiring_approval = policy.get("risk_levels_requiring_approval", ["HIGH", "CRITICAL"])
    if risk_level in risk_levels_requiring_approval:
        requires_approval = True
    if policy.get("always_require_human_approval", False):
        requires_approval = True

    # FAIL-CLOSED governance: an incomplete analysis must never auto-approve.
    # A failed agent (e.g. legal module unavailable) escalates risk and forces sign-off.
    recommendations_extra: list[str] = []
    if failed_tasks and policy.get("fail_closed_on_task_failure", True):
        requires_approval = True
        escalation = policy.get("risk_level_on_task_failure", "HIGH")
        if _risk_rank(risk_level) < _risk_rank(escalation):
            risk_level = escalation
        recommendations_extra.append(
            f"Incomplete analysis: {len(failed_tasks)} agent task(s) failed "
            f"({', '.join(sorted(set(failed_tasks)))}). Mandatory re-run before commitment."
        )

    recommendation_proceed = policy.get(
        "recommendation_proceed", "Proceed with formal opportunity qualification"
    )
    recommendation_halt = policy.get("recommendation_halt", "Halt qualification")
    recommendation_compliance = policy.get(
        "recommendation_compliance", "Enforce AI Act compliance controls and audit log streaming"
    )

    artifact = DecisionArtifact(
        artifact_id=f"art_{state.session_id[:8]}",
        title=f"Intelligence Synthesis: {state.user_query}",
        summary=(
            f"Synthesized intelligence across {len(state.tasks)} domains."
            + (f" {len(failed_tasks)} task(s) FAILED - analysis incomplete." if failed_tasks else "")
        ),
        recommendations=[
            recommendation_proceed if risk_level != "CRITICAL" else recommendation_halt,
            recommendation_compliance
        ] + recommendations_extra,
        risk_level=risk_level,
        requires_human_approval=requires_approval
    )

    msg = AgentMessage(
        sender=AgentRole.SUPERVISOR.value,
        recipient="SYSTEM",
        content=f"Synthesis complete. Risk Level: {risk_level}, Human Approval Required: {requires_approval}"
    )

    report_cfg = policy.get("decision_report", {})
    not_run_label = report_cfg.get("not_run_label", "Not evaluated")
    facts = {
        "risk_level": risk_level,
        "compliance_status": legal_view.get("status", "UNKNOWN") if legal_view else "UNKNOWN",
        "fit_score": risk_view.get("fit") if risk_view else None,
        "task_failures": bool(failed_tasks)
    }
    verdict = _select_decision(
        facts,
        report_cfg.get("decision_matrix", []),
        {
            "decision": report_cfg.get("verdict_fallback", "Proceed with caution"),
            "reason": report_cfg.get(
                "verdict_fallback_reason", "No configured rule matched the aggregated facts."
            )
        }
    )

    decision_report = DecisionReport(
        query=state.user_query,
        market=_render_section(
            report_cfg.get("market_template", "{{growth}} {{posture}}"), market_view, not_run_label
        ),
        risk=_render_section(report_cfg.get("risk_template", "{{level}}"), risk_view, not_run_label),
        legal=_render_section(report_cfg.get("legal_template", "{{status}}"), legal_view, not_run_label),
        decision=verdict["decision"],
        decision_reason=verdict["reason"],
        confidence=round(sum(confidence_samples) / len(confidence_samples), 3) if confidence_samples else 0.0,
        citations=report_citations[:10],
        requires_human_approval=requires_approval,
        contributor_agents=sorted(set(contributor_agents))
    )

    return {
        "artifacts": [artifact],
        "decision_report": decision_report,
        "messages": [msg],
        "pending_human_approval": requires_approval,
        "is_completed": not requires_approval
    }


def human_approval_node(state: OrchestratorState) -> Dict[str, Any]:
    """Human-in-the-Loop node. Freezes execution via interrupt() until human sign-off.
    The review prompt text is configurable via orchestrator_config.json -> orchestrator.human_gate_prompt."""
    policy = load_config().get("orchestrator", {})
    gate_prompt = policy.get(
        "human_gate_prompt",
        "Governance Officer review required. Please approve or reject with comments."
    )
    human_payload = interrupt({
        "session_id": state.session_id,
        "query": state.user_query,
        "artifacts": [a.model_dump() for a in state.artifacts],
        "prompt": gate_prompt
    })

    approved = False
    feedback = "Reviewed by governance officer"
    if isinstance(human_payload, dict):
        approved = human_payload.get("approved", False)
        feedback = human_payload.get("feedback") or feedback

    msg = AgentMessage(
        sender=AgentRole.HUMAN_REVIEWER.value,
        recipient=AgentRole.SUPERVISOR.value,
        content=f"Human Review Decision: {'APPROVED' if approved else 'REJECTED'}. Comments: {feedback}"
    )

    final_text = (
        f"Execution finalized with human sign-off ({'APPROVED' if approved else 'REJECTED'}). "
        f"Governance Notes: {feedback}"
    )

    return {
        "human_approved": approved,
        "human_feedback": feedback,
        "pending_human_approval": False,
        "final_response": final_text,
        "is_completed": True,
        "messages": [msg]
    }



# Execution order for the orchestration graph nodes, keyed by agent role.
_ROLE_TO_NODE: Dict[AgentRole, str] = {
    AgentRole.MARKET_INTELLIGENCE: "market",
    AgentRole.OPPORTUNITY_RISK: "risk",
    AgentRole.LEGAL_REGULATORY: "legal",
}


def router(state: OrchestratorState) -> Literal["market", "risk", "legal", "synthesis"]:
    """Routes execution to the planner's next pending subtask.

    SubTasks are executed in the order the planner declared them (`SubTask.sequence`);
    Python's stable sort keeps insertion order for ties. Falls through to synthesis
    when no executable task remains."""
    pending = [
        task for task in state.tasks.values()
        if task.status == TaskStatus.PENDING and task.target_agent in _ROLE_TO_NODE
    ]
    if not pending:
        return "synthesis"
    pending.sort(key=lambda task: task.sequence)
    return _ROLE_TO_NODE[pending[0].target_agent]


def should_require_human_gate(state: OrchestratorState) -> Literal["human_gate", "__end__"]:
    """Routes to human gate if human approval is pending and hasn't been decided yet."""
    if state.pending_human_approval and state.human_approved is None:
        return "human_gate"
    return END


# Model classes stored in checkpoints must be allowlisted so future LangGraph
# versions (strict msgpack mode) can safely deserialize them.
CHECKPOINT_ALLOWED_MODULES = [
    ("agent_orchestrator.models", "AgentRole"),
    ("agent_orchestrator.models", "TaskStatus"),
    ("agent_orchestrator.models", "SubTask"),
    ("agent_orchestrator.models", "AgentMessage"),
    ("agent_orchestrator.models", "DecisionArtifact"),
    ("agent_orchestrator.models", "DecisionReport"),
]


def build_checkpoint_serde() -> JsonPlusSerializer:
    """Creates a serializer that allows this module's state classes in checkpoints.
    Pass it to MemorySaver(serde=...) / SqliteSaver(conn, serde=...)."""
    return JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_ALLOWED_MODULES)


def build_orchestration_graph(checkpointer: Any = None):
    """Assembles and compiles the enterprise LangGraph StateGraph."""
    workflow = StateGraph(OrchestratorState)

    workflow.add_node("supervisor", supervisor_node)
    workflow.add_node("market", execute_market_agent)
    workflow.add_node("risk", execute_risk_agent)
    workflow.add_node("legal", execute_legal_agent)
    workflow.add_node("synthesis", synthesis_and_governance_node)
    workflow.add_node("human_gate", human_approval_node)

    workflow.add_edge(START, "supervisor")

    workflow.add_conditional_edges("supervisor", router, {
        "market": "market", "risk": "risk", "legal": "legal", "synthesis": "synthesis"
    })
    workflow.add_conditional_edges("market", router, {
        "market": "market", "risk": "risk", "legal": "legal", "synthesis": "synthesis"
    })
    workflow.add_conditional_edges("risk", router, {
        "market": "market", "risk": "risk", "legal": "legal", "synthesis": "synthesis"
    })
    workflow.add_conditional_edges("legal", router, {
        "market": "market", "risk": "risk", "legal": "legal", "synthesis": "synthesis"
    })

    workflow.add_conditional_edges("synthesis", should_require_human_gate, {
        "human_gate": "human_gate",
        END: END
    })

    workflow.add_edge("human_gate", END)

    return workflow.compile(checkpointer=checkpointer)

