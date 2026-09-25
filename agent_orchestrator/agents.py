"""
Enterprise Specialized Domain Agent Implementations:
- Supervisor Agent (LLM-driven Chain-of-Thought decomposition & validation)
- Market Intelligence Agent (Domain 7)
- Opportunity & Risk Intelligence Agent (Domain 8)
- Legal & Regulatory Intelligence Agent (Domain 9)

All tunables (enabled flags, confidence scores, prompts, thresholds) are loaded
from orchestrator_config.json via config.py.
"""
from typing import Any, Dict
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
    """Specialized Agent for Domain 7: Business & Market Intelligence."""

    @staticmethod
    def execute(subtask: SubTask) -> Dict[str, Any]:
        settings = agent_settings(AgentRole.MARKET_INTELLIGENCE.value)
        topic = subtask.input_data.get("topic", "Enterprise AI")
        caller = AgentRole.MARKET_INTELLIGENCE.value

        osint_signals = ToolRegistry.query_osint_signals(topic, caller_agent=caller)
        kg_insights = ToolRegistry.query_knowledge_graph(topic, caller_agent=caller)
        document_hits = ToolRegistry.search_documents(topic, caller_agent=caller)
        company_context = ToolRegistry.query_company_context(topic, caller_agent=caller)

        output = {
            "market_signals": osint_signals,
            "graph_context": kg_insights,
            "document_context": document_hits,
            "company_context": company_context,
            "competitive_posture": "High demand in federal and high-assurance sovereign AI sectors.",
            "market_growth_vector": "Accelerating adoption of sovereign AI pipelines with strict provenance.",
            "source_provenance": [s["source"] for s in osint_signals]
        }
        extra_citations = settings.get("extra_citations", ["Person A Knowledge Graph"])
        return {
            "output_data": output,
            "confidence_score": float(settings.get("confidence_score", 0.94)),
            "citations": [s["source"] for s in osint_signals] + list(extra_citations)
        }


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
            "bid_recommendation": fit_result["recommendation"]
        }
        return {
            "output_data": output,
            "confidence_score": float(settings.get("confidence_score", 0.91)),
            "citations": settings.get("citations", ["Enterprise Capability Matrix", "ISO27001 Certification Registry"])
        }


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
            "ip_risk": settings.get(
                "ip_risk_note",
                "Low - No trademark or patent blocking conflicts identified in current claims."
            ),
            "mandatory_governance_clauses": settings.get("mandatory_governance_clauses", [
                "Human supervisor approval required before binding enterprise commitment",
                "Immutable cryptographic audit logging enabled for all agent decisions"
            ])
        }
        return {
            "output_data": output,
            "confidence_score": float(settings.get("confidence_score", 0.96)),
            "citations": compliance_check.get("detected_regulations", [])
        }

