"""
Enterprise Specialized Domain Agent Implementations:
- Supervisor Agent (LLM-driven Chain-of-Thought decomposition & validation)
- Market Intelligence Agent (Domain 7)
- Opportunity & Risk Intelligence Agent (Domain 8)
- Legal & Regulatory Intelligence Agent (Domain 9)

All tunables (enabled flags, confidence scores, prompts, thresholds) are loaded
from orchestrator_config.json via config.py.
"""
from typing import Any, Dict, List
import uuid
from .models import AgentMessage, AgentRole, OrchestratorState, SubTask
from .tools import ToolRegistry
from .llm_engine import EnterpriseLLMEngine
from .config import agent_enabled, agent_settings, load_config, render_template


class SupervisorAgent:
    """Supervises planning, breaks down user queries into subtasks using Enterprise LLM reasoning."""

    @staticmethod
    def plan(state: OrchestratorState, llm_engine: EnterpriseLLMEngine = None) -> Dict[str, Any]:
        user_query = state.user_query
        engine = llm_engine or EnterpriseLLMEngine()
        sup_cfg = load_config().get("supervisor", {})
        context = {"query": user_query}

        system_prompt = sup_cfg.get(
            "system_prompt",
            "Decompose the user inquiry into strategic domain-specific subtasks. "
            "Return structured JSON with subtasks."
        )
        llm_resp = engine.generate(system_prompt, user_query)
        parsed = llm_resp.parsed_json or {}

        tasks: Dict[str, SubTask] = {}
        plan: list[str] = []
        planned_index = 0  # contiguous planner order, assigned only after role/enabled filtering

        subtasks_data = parsed.get("subtasks", [])
        if not subtasks_data:
            # Fallback decomposition from config if LLM output had no subtasks
            subtasks_data = render_template(sup_cfg.get("fallback_subtasks", []), context)

        for s in subtasks_data:
            try:
                target_role = AgentRole(s.get("target_agent", "MARKET_INTELLIGENCE"))
            except ValueError:
                continue  # skip unknown agent roles from malformed LLM output
            if not agent_enabled(target_role.value):
                continue  # skip agents disabled in orchestrator_config.json
            task_id = f"task_{uuid.uuid4().hex[:6]}"
            tasks[task_id] = SubTask(
                task_id=task_id,
                target_agent=target_role,
                description=render_template(s.get("description", f"Execute {target_role.value}"), context),
                sequence=planned_index,
                input_data=render_template(s.get("input_data", {}), context)
            )
            planned_index += 1
            plan.append(f"{target_role.value} ({task_id}): {tasks[task_id].description}")

        msg_template = sup_cfg.get(
            "planning_message",
            "Supervisor planned {{count}} enterprise subtask(s). Reasoning: {{thought}}"
        )
        msg = AgentMessage(
            sender=AgentRole.SUPERVISOR.value,
            recipient="ALL",
            content=render_template(msg_template, {
                "count": len(tasks),
                "thought": parsed.get("thought_process", "Standard decomposition")
            })
        )

        return {
            "plan": plan,
            "tasks": tasks,
            "messages": [msg],
            "active_agent": AgentRole.SUPERVISOR.value
        }


class MarketIntelligenceAgent:
    """
    Specialized Agent for Domain 7: Business & Market Intelligence.

    **No fabricated analysis.** This agent previously returned two constant strings
    (``competitive_posture`` and ``market_growth_vector``) for *every* query, which then appeared
    verbatim in the decision report at 0.94 confidence. Both are now derived from the signals the
    tool bus actually returned, and when there are no signals the agent says so rather than filling
    the gap with prose.

    Confidence is likewise no longer a configured constant: it is the market domain's own computed
    value, floored by the weakest contributing source, with a ``confidence_basis`` label so a
    consumer can tell a measurement from a prior.
    """

    @staticmethod
    def execute(subtask: SubTask) -> Dict[str, Any]:
        settings = agent_settings(AgentRole.MARKET_INTELLIGENCE.value)
        topic = subtask.input_data.get("topic", "Enterprise AI")
        caller = AgentRole.MARKET_INTELLIGENCE.value

        osint_signals = ToolRegistry.query_osint_signals(topic, caller_agent=caller)
        kg_insights = ToolRegistry.query_knowledge_graph(topic, caller_agent=caller)
        document_hits = ToolRegistry.search_documents(topic, caller_agent=caller)
        company_context = ToolRegistry.query_company_context(topic, caller_agent=caller)

        # Status of each *capability* this leg depends on. Only the status strings are inspected —
        # the signal list is data, not a status, and must never be reported as a degraded source.
        status_by_capability = {
            "market": "LIVE" if osint_signals else "UNAVAILABLE",
            "knowledge_graph": kg_insights.get("data_status", "UNAVAILABLE"),
            "documents": document_hits.get("data_status", "UNAVAILABLE"),
            "org": company_context.get("data_status", "UNAVAILABLE"),
        }
        live_count = sum(1 for status in status_by_capability.values() if status == "LIVE")
        # Report *which* capability was degraded, not its raw status string, so the reason is
        # actionable ("knowledge graph did not respond") rather than a bare "UNAVAILABLE".
        degraded = sorted(cap for cap, status in status_by_capability.items() if status != "LIVE")

        market_report = _market_report(osint_signals)

        prior = float(settings.get("confidence_score", 0.5))
        confidence = _floor_confidence(osint_signals, prior, live_count=live_count)
        basis = "measured" if osint_signals else "prior-no-evidence"

        output = {
            "market_signals": osint_signals,
            "graph_context": kg_insights,
            "document_context": document_hits,
            "company_context": company_context,
            # Derived, not asserted. Present only when there is something to derive them from.
            "competitive_posture": market_report["posture"],
            "market_growth_vector": market_report["growth"],
            "source_provenance": [s["source"] for s in osint_signals],
            "data_status": "LIVE" if osint_signals else "UNAVAILABLE",
            "degraded_sources": degraded,
            "signal_count": len(osint_signals),
            "confidence_basis": basis,
        }
        # Only cite what was actually consulted. The previous default cited "Person A Knowledge
        # Graph" unconditionally - a citation to a graph that does not exist.
        citations = [s["source"] for s in osint_signals]
        for label, result in (("Knowledge Graph", kg_insights), ("Document Corpus", document_hits),
                              ("Company Knowledge", company_context)):
            if result.get("data_status") == "LIVE":
                citations.append(label)
        return {
            "output_data": output,
            "confidence_score": confidence,
            "confidence_basis": basis,
            "citations": citations,
        }


def _market_report(signals: List[Dict[str, Any]]) -> Dict[str, str]:
    """
    Derive the market narrative from real signals.

    Returns empty strings when there is no evidence. Synthesis renders those as an explicit
    "insufficient evidence" clause rather than a fabricated paragraph, so a missing market view can
    never be read as an analysis.
    """
    if not signals:
        return {"posture": "", "growth": ""}
    dated = [s for s in signals if s.get("timestamp")]
    if dated:
        newest = max(dated, key=lambda s: str(s["timestamp"]))
        oldest = min(dated, key=lambda s: str(s["timestamp"]))
        growth = (
            f"{len(signals)} signal(s) between {oldest['timestamp']} and {newest['timestamp']}; "
            f"most recent: {newest.get('headline', '')[:120]}"
        )
    else:
        growth = f"{len(signals)} signal(s) retrieved; none carry a publication timestamp."
    sources = sorted({str(s.get("source", "unknown")) for s in signals})
    mean_relevance = sum(float(s.get("relevance", 0.0)) for s in signals) / len(signals)
    posture = (
        f"{len(sources)} distinct source(s) ({', '.join(sources[:3])}); "
        f"mean relevance {mean_relevance:.2f}."
    )
    return {"posture": posture, "growth": growth}


def _floor_confidence(signals: List[Dict[str, Any]], prior: float, *, live_count: int) -> float:
    """
    Conservative aggregation: the market view cannot be more confident than its weakest source.

    Averaging (the previous behaviour) hides a single failed source behind two good ones. Taking
    the minimum is the defensible choice for a decision input: one unavailable source caps the leg.
    With no signals at all the result is the configured prior, capped low and labelled as a prior by
    the caller.
    """
    if not signals:
        return min(prior, 0.5)
    credibilities = [float(s.get("credibility_score", 0.0)) for s in signals]
    floor = min(credibilities) if credibilities else prior
    # A partially-unavailable evidence base (graph/documents/company) caps confidence further.
    if live_count < 4:
        floor = min(floor, 0.75)
    return round(max(0.0, min(floor, 1.0)), 3)



class OpportunityRiskAgent:
    """Specialized Agent for Domain 8: Opportunity, Risk & Requirements Intelligence."""

    @staticmethod
    def execute(subtask: SubTask) -> Dict[str, Any]:
        settings = agent_settings(AgentRole.OPPORTUNITY_RISK.value)
        rfp_name = subtask.input_data.get("rfp_name", "Target Enterprise Opportunity")
        reqs = subtask.input_data.get("requirements") or settings.get(
            "default_requirements", ["Data sovereignty", "Cloud security", "Audit trail"]
        )
        caller = AgentRole.OPPORTUNITY_RISK.value

        fit_result = ToolRegistry.evaluate_rfp_fit(rfp_name, reqs, caller_agent=caller)
        has_gaps = len(fit_result["capability_gaps"]) > 0
        risk_threshold = float(settings.get("high_risk_fit_score_below", 80))
        risk_level = "HIGH" if has_gaps or fit_result["fit_score"] < risk_threshold else "LOW"

        base_risk = settings.get("base_risk_assessment", {
            "cyber_risk": "MEDIUM", "contract_penalty_exposure": "MODERATE"
        })
        risk_assessment = {
            **base_risk,
            "operational_risk": "HIGH" if has_gaps else "LOW",
            "overall_risk_level": risk_level
        }

        output = {
            "evaluation": fit_result,
            "risk_assessment": risk_assessment,
            "bid_recommendation": fit_result["recommendation"],
            # Surface what the assessment is based on. INSUFFICIENT_DATA must never be rendered
            # as a bid recommendation.
            "data_status": fit_result.get("data_status", "LIVE"),
            "confidence_basis": "measured" if fit_result.get("data_status") == "LIVE" else "heuristic",
        }
        return {
            "output_data": output,
            # Derived from the assessment, not a configured constant. A fallback-path assessment
            # (keyword heuristic) is explicitly low confidence.
            "confidence_score": _risk_confidence(fit_result, settings),
            "confidence_basis": "measured" if fit_result.get("data_status") == "LIVE" else "heuristic",
            "citations": settings.get("citations", ["Enterprise Capability Matrix"]),
        }


def _risk_confidence(fit_result: Dict[str, Any], settings: Dict[str, Any]) -> float:
    """
    Confidence for the opportunity leg.

    On the live Domain 8 path the fit score is evidence-derived, so confidence tracks it. On the
    heuristic fallback path the result is a keyword overlap, which is not a defensible basis for a
    bid decision, so confidence is capped low and the caller labels it ``heuristic``.
    """
    if fit_result.get("data_status") != "LIVE":
        return 0.35
    fit = float(fit_result.get("fit_score", 0.0))
    # 100% keyword match is not certainty: cap below 1.0 so an exact match never reads as proof.
    return round(max(0.0, min(fit / 100.0 * 0.9, 0.9)), 3)


class LegalRegulatoryAgent:
    """Specialized Agent for Domain 9: Legal, Regulatory & IP Intelligence."""

    @staticmethod
    def execute(subtask: SubTask) -> Dict[str, Any]:
        settings = agent_settings(AgentRole.LEGAL_REGULATORY.value)
        scope = subtask.input_data.get("scope", "Autonomous agent data processing and intelligence operations")
        caller = AgentRole.LEGAL_REGULATORY.value

        compliance_check = ToolRegistry.check_legal_compliance(scope, caller_agent=caller)

        output = {
            "regulatory_review": compliance_check,
            # Removed: a constant "Low - No trademark or patent blocking conflicts identified"
            # string that asserted an IP clearance nobody performed. Domain 9's own clearance result
            # is used, or the gap is stated explicitly.
            "ip_risk": _ip_risk_note(settings, compliance_check),
            "data_status": compliance_check.get("data_status", "UNAVAILABLE"),
            "confidence_basis": (
                "measured" if compliance_check.get("data_status") == "LIVE" else "insufficient-evidence"
            ),
            "mandatory_governance_clauses": settings.get("mandatory_governance_clauses", [
                "Human supervisor approval required before binding enterprise commitment",
                "Immutable cryptographic audit logging enabled for all agent decisions"
            ])
        }
        return {
            "output_data": output,
            # A compliance check that could not be performed is not 0.96-confident. Confidence
            # tracks the assessment: a completed check is a genuine finding; UNKNOWN is a gap.
            "confidence_score": _legal_confidence(compliance_check),
            "confidence_basis": (
                "measured" if compliance_check.get("data_status") == "LIVE" else "insufficient-evidence"
            ),
            "citations": compliance_check.get("detected_regulations", []),
        }


def _legal_confidence(check: Dict[str, Any]) -> float:
    """Legal confidence: high only when a real assessment ran; low when the position is unknown."""
    if check.get("data_status") != "LIVE":
        return 0.2
    if check.get("compliance_status") in ("FLAGGED", "CLEARED"):
        # A completed assessment is a finding, not a guess.
        return 0.85
    return 0.5


def _ip_risk_note(settings: Dict[str, Any], check: Dict[str, Any]) -> str:
    """
    IP risk statement.

    The previous hardcoded note claimed "No trademark or patent blocking conflicts identified" -
    a clearance assertion produced by no analysis. A configured note is honoured only if an operator
    explicitly supplies one; otherwise the default states the gap rather than asserting safety.
    """
    configured = settings.get("ip_risk_note")
    if configured:
        return str(configured)
    if check.get("data_status") == "LIVE" and check.get("detected_regulations"):
        return "No dedicated IP clearance was run for this scope; the regulations above are the only finding."
    return "IP clearance NOT PERFORMED - treat this as an open gap, not a clearance."

