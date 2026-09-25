"""
Interactive Test Drive for Vexer Enterprise Intelligence Platform (Domain 6)
Run this script to test the full multi-agent orchestration lifecycle with live terminal output!
"""
import uuid
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agent_orchestrator.models import OrchestratorState, TaskStatus
from agent_orchestrator.orchestrator import build_orchestration_graph, build_checkpoint_serde
from agent_orchestrator.tools import ToolRegistry


def print_banner(text: str):
    print("\n" + "=" * 70)
    print(f"  {text}")
    print("=" * 70)


def main():
    print_banner("VEXER ENTERPRISE INTELLIGENCE BRAIN - INTERACTIVE TEST DRIVE")
    print("Domain 6: AI Agents & Agent Orchestration (Python + LangGraph)\n")

    default_query = "EU Defense AI Tender: Autonomous reconnaissance agent with cross-border data transfer"
    print(f"Default Enterprise Query:\n  '{default_query}'\n")

    user_input = input("Press [ENTER] to use default query or type a custom enterprise inquiry: ").strip()
    query = user_input if user_input else default_query

    print_banner("STEP 1: SUPERVISOR PLANNING & REASONING")
    saver = MemorySaver(serde=build_checkpoint_serde())
    graph = build_orchestration_graph(checkpointer=saver)

    session_id = f"session_{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": session_id}}

    state = OrchestratorState(
        session_id=session_id,
        user_query=query
    )

    print(f"[*] Dispatching inquiry into LangGraph State Machine (Session: {session_id})...")
    result = graph.invoke(state, config=config)

    print("\n[+] Supervisor Decomposition Plan:")
    for step in result.get("plan", []):
        print(f"    - {step}")

    print_banner("STEP 2: SPECIALIZED AGENT EXECUTION")
    for tid, task in result.get("tasks", {}).items():
        status_symbol = "[OK]" if task.status == TaskStatus.COMPLETED else "[FAIL]"
        print(f"{status_symbol} Task ID: {tid} | Agent: {task.target_agent.value}")
        print(f"    Description: {task.description}")
        print(f"    Confidence Score: {task.confidence_score * 100:.1f}%")
        print(f"    Citations: {', '.join(task.citations)}")
        if task.output_data and "risk_assessment" in task.output_data:
            print(f"    Risk Assessment: {task.output_data['risk_assessment']}")
        print()

    print_banner("STEP 3: ENTERPRISE TOOL RBAC AUDIT TRAIL")
    audit_trail = ToolRegistry.get_audit_trail()
    print(f"Total Tools Invoked: {len(audit_trail)}")
    for i, rec in enumerate(audit_trail, 1):
        print(f"  {i}. [{rec.status}] Tool: {rec.tool_name:<25} Caller: {rec.caller_agent:<22} ({rec.execution_time_ms:.2f}ms)")

    print_banner("STEP 4: HUMAN-IN-THE-LOOP (HITL) GOVERNANCE GATE")
    state_snapshot = graph.get_state(config)

    if state_snapshot.tasks and state_snapshot.tasks[0].interrupts:
        interrupt_val = state_snapshot.tasks[0].interrupts[0].value
        print("[!] Execution HALTED at Governance Checkpoint via LangGraph interrupt()!")
        print(f"    Prompt: {interrupt_val['prompt']}")

        artifacts = result.get("artifacts", [])
        if artifacts:
            art = artifacts[0]
            print(f"    Risk Level Detected: {art.risk_level}")
            print(f"    Title: {art.title}")
            print("    Key Recommendations:")
            for rec in art.recommendations:
                print(f"      * {rec}")

        print("\n--- HUMAN GOVERNANCE SIGN-OFF REQUIRED ---")
        try:
            decision = input("Do you approve this enterprise intelligence action? (y/n) [default: y]: ").strip().lower()
        except EOFError:
            decision = "y"
            print("y (auto-selected for non-interactive mode)")
        is_approved = decision != "n"
        try:
            notes = input("Enter governance review notes [default: 'Approved by Enterprise Officer']: ").strip()
        except EOFError:
            notes = "Approved by Enterprise Officer (Auto)"
            print(notes)

        print("\n[*] Resuming LangGraph State Machine with human sign-off token...")
        resume_payload = {"approved": is_approved, "feedback": notes}
        final_result = graph.invoke(Command(resume=resume_payload), config=config)

        print_banner("STEP 5: FINAL DECISION ARTIFACT & RESOLUTION")
        print(f"Status: COMPLETED (Human Decision: {'APPROVED' if is_approved else 'REJECTED'})")
        print(f"System Message: {final_result.get('final_response')}")
    else:
        print("[+] Workflow completed automatically without requiring human sign-off.")
        print(f"Artifacts: {result.get('artifacts')}")

    print_banner("TEST DRIVE COMPLETE - DOMAIN 6 FULLY OPERATIONAL")


if __name__ == "__main__":
    main()
