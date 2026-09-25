"""
Tool-bus runtime guarantees (mandate §19, §26, §29, §34, §45 — increment 3 / P7).

These tests exist because the properties they assert were *false* before the refactor, and nothing
in the old suite would have noticed:

* the breaker predicate mutated state, so a plain read changed behaviour;
* breaker and audit state were module/class globals, so one test could poison another;
* configuration was frozen at import, so a config change was invisible without a restart;
* downstream failures were absorbed by fabricated data that looked identical to real results.

Everything here runs without a database and without any external service.
"""
from __future__ import annotations

import threading

import pytest

from agent_orchestrator.bus import (
    DOMAIN_KEYS,
    CircuitBreaker,
    InMemoryAuditSink,
    InMemoryCircuitBreakers,
    ToolAuditRecord,
    ToolBus,
    canonical_key,
)
from agent_orchestrator.tools import ToolRegistry
from vexer_platform.contracts import DataStatus

# -------------------------------------------------------------------------------------------
# Circuit breaker
# -------------------------------------------------------------------------------------------


def test_breaker_opens_only_after_the_threshold() -> None:
    breaker = CircuitBreaker(failure_threshold=3, recovery_seconds=60)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "closed", "two failures must not open a threshold-of-three breaker"
    breaker.record_failure()
    assert breaker.state == "open"


def test_success_resets_the_failure_run() -> None:
    breaker = CircuitBreaker(failure_threshold=3, recovery_seconds=60)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "closed", "consecutive failures, not total failures, trip the breaker"


def test_state_is_read_only() -> None:
    """
    Regression guard: the old ``is_open()`` transitioned to half-open as a side effect of being
    *read*, so inspecting a breaker changed it. ``state`` must be pure.
    """
    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=0)
    breaker.record_failure()
    for _ in range(10):
        breaker.state
    assert breaker.state == "half_open"


def test_only_one_probe_is_admitted_while_half_open() -> None:
    """A recovering dependency must not be stampeded by every waiting thread."""
    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=0)
    breaker.record_failure()
    admitted: list[bool] = []

    def worker() -> None:
        admitted.append(breaker.allow_request())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert admitted.count(True) == 1, f"exactly one probe must be admitted, got {admitted}"


def test_failed_probe_reopens_immediately(monkeypatch) -> None:
    """
    A half-open probe that fails must start a *fresh* recovery window.

    Uses a controllable clock: with a zero-length window the breaker re-opens and instantly elapses
    again, which would make the assertion meaningless (and hides a real regression).
    """
    clock = {"now": 1000.0}
    monkeypatch.setattr("agent_orchestrator.bus.time.monotonic", lambda: clock["now"])

    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=30)
    breaker.record_failure()
    assert breaker.state == "open"

    clock["now"] += 31            # recovery window elapses -> half-open
    assert breaker.state == "half_open"
    assert breaker.allow_request() is True   # exactly one probe admitted

    breaker.record_failure()       # the probe failed
    assert breaker.state == "open", "a failed probe must re-open and restart the window"
    assert breaker.allow_request() is False, "no new probe until the fresh window elapses"

    clock["now"] += 31
    assert breaker.state == "half_open"


def test_concurrent_failures_do_not_corrupt_the_counter() -> None:
    """A racing counter could undercount and leave a dead dependency tripping forever."""
    breaker = CircuitBreaker(failure_threshold=100, recovery_seconds=60)

    def worker() -> None:
        for _ in range(50):
            breaker.record_failure()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert breaker._failures == 200
    assert breaker.state == "open"


# -------------------------------------------------------------------------------------------
# Bounded audit sink
# -------------------------------------------------------------------------------------------


def test_audit_sink_is_bounded_and_counts_drops() -> None:
    sink = InMemoryAuditSink(maxlen=3)
    for index in range(10):
        sink.append(ToolAuditRecord(tool_name=f"t{index}", caller_agent="A", arguments={}))
    assert len(sink.records()) == 3
    assert sink.dropped == 7, "truncation must be counted, never silent"


def test_audit_sink_with_zero_capacity_records_everything_as_dropped() -> None:
    """Disabling the audit must be visible, not a silent black hole."""
    sink = InMemoryAuditSink(maxlen=0)
    sink.append(ToolAuditRecord(tool_name="t", caller_agent="A", arguments={}))
    assert sink.records() == []
    assert sink.dropped == 1


def test_audit_sink_is_thread_safe() -> None:
    sink = InMemoryAuditSink(maxlen=500)

    def worker() -> None:
        for _ in range(100):
            sink.append(ToolAuditRecord(tool_name="t", caller_agent="A", arguments={}))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(sink.records()) == 400


# -------------------------------------------------------------------------------------------
# Bus: configuration, isolation, health
# -------------------------------------------------------------------------------------------


def test_config_is_read_at_construction_not_at_import() -> None:
    """Two buses built from different configs must differ — the import-time freeze made this impossible."""
    strict = ToolBus.from_config({"tools": {"permissions": {"query_osint_signals": ["SUPERVISOR"]}}})
    default = ToolBus.from_config({})
    assert strict.permissions("query_osint_signals") == ["SUPERVISOR"]
    assert default.permissions("query_osint_signals") == ["SUPERVISOR", "MARKET_INTELLIGENCE"]


def test_buses_do_not_share_state() -> None:
    """Two buses in one process are fully independent — the old module globals were not."""
    first = ToolBus.from_config({})
    second = ToolBus.from_config({})
    first._breakers.breaker("market").record_failure()
    first.record(ToolAuditRecord(tool_name="t", caller_agent="A", arguments={}))
    assert second.audit_trail() == []
    assert second._breakers.breaker("market").state == "closed"


def test_breaker_registry_covers_every_declared_capability() -> None:
    """Every declared seam has a breaker, so no dependency can fail unprotected by omission."""
    registry = InMemoryCircuitBreakers()
    snapshot = registry.snapshot()
    for key in DOMAIN_KEYS:
        assert snapshot[key] == "closed", f"{key} has no breaker and could fail unprotected"


def test_health_declares_state_distribution() -> None:
    """§19: a single-process audit trail must not read as a cluster-wide one."""
    health = ToolBus.from_config({}).health()
    assert health["breaker_distribution"] == "single-process"
    assert health["audit_distribution"] == "single-process"
    assert set(health["breakers"]) >= {"market", "opportunity", "legal"}


def test_health_reports_degraded_when_audit_truncated() -> None:
    bus = ToolBus.from_config({"tools": {"audit": {"max_records": 2}}})
    for index in range(5):
        bus.record(ToolAuditRecord(tool_name=f"t{index}", caller_agent="A", arguments={}))
    health = bus.health()
    assert health["audit_dropped"] == 3
    assert health["data_status"] == DataStatus.DEGRADED.value


def test_unresolvable_domain_is_reported_not_raised() -> None:
    """§34: a broken integration must not crash the bus."""
    bus = ToolBus.from_config({}, service_resolver=lambda key: (_ for _ in ()).throw(RuntimeError()))
    assert bus.service("knowledge_graph") is None



# -------------------------------------------------------------------------------------------
# Provenance: no fabricated intelligence (§41, §52)
# -------------------------------------------------------------------------------------------


@pytest.fixture
def no_domains():
    """A bus where no downstream domain resolves, injected into the registry facade."""
    previous = ToolRegistry._bus
    ToolRegistry.set_bus(ToolBus.from_config({}, service_resolver=lambda key: None))
    try:
        yield
    finally:
        ToolRegistry.set_bus(previous)


def test_no_tool_invents_data_when_no_domain_is_available(no_domains) -> None:
    """Every seam must return an empty, explicitly-tagged result — never invented records."""
    kg = ToolRegistry.query_knowledge_graph("Acme", caller_agent="MARKET_INTELLIGENCE")
    docs = ToolRegistry.search_documents("q", caller_agent="MARKET_INTELLIGENCE")
    ctx = ToolRegistry.query_company_context("t", caller_agent="MARKET_INTELLIGENCE")
    signals = ToolRegistry.query_osint_signals("t", caller_agent="MARKET_INTELLIGENCE")
    fit = ToolRegistry.evaluate_rfp_fit("RFP", ["req"], caller_agent="OPPORTUNITY_RISK")
    legal = ToolRegistry.check_legal_compliance("scope", caller_agent="LEGAL_REGULATORY")

    assert kg["relationships"] == [] and kg["verified_facts"] == []
    assert docs["documents"] == []
    assert ctx["context"] == {}
    assert signals == []
    for result in (kg, docs, ctx, fit, legal):
        assert result["data_status"] in (DataStatus.UNAVAILABLE.value, DataStatus.FALLBACK.value)

# -------------------------------------------------------------------------------------------
# Seam naming: capability names, never domain numbers (§53 — cross-team boundary clarity)
# -------------------------------------------------------------------------------------------


def test_seam_keys_are_capability_names_not_domain_numbers() -> None:
    """
    Regression guard for the 10-domain renumbering.

    The seams used to be keyed ``domain1``…``domain9``. After the split that became actively
    misleading: old ``domain4`` meant Document & Knowledge while new "4" is Knowledge Graph, and old
    ``domain5`` meant Knowledge Graph while new "5" is Evidence/Verification. A log line would be
    misread by anyone holding the current map. Keys are now capability names, so they cannot drift
    when the org chart does.
    """
    for key in DOMAIN_KEYS:
        assert not key.startswith("domain"), f"seam key {key!r} is a domain number, not a capability"
        assert not key[0].isdigit(), f"seam key {key!r} must not be a bare number"


def test_legacy_keys_still_resolve() -> None:
    """An in-flight branch or config using the old keys keeps working during migration."""
    assert canonical_key("domain5") == "knowledge_graph"
    assert canonical_key("domain7") == "market"
    assert canonical_key("domain9") == "legal"
    assert canonical_key("market") == "market", "current names must pass through unchanged"


def test_legacy_config_flags_are_honoured_and_new_names_win() -> None:
    """
    A renamed flag that is simply ignored is worse than one that errors: an operator would flip it
    and see no effect, with nothing telling them the name had moved.
    """
    from agent_orchestrator.bus import _flag

    assert _flag({"enable_domain5": True}, "enable_knowledge_graph", False) is True
    assert _flag({}, "enable_knowledge_graph", False) is False
    assert _flag({}, "enable_market", True) is True, "Person B's own domains default to enabled"
    # New name wins when both are present.
    assert _flag(
        {"enable_domain5": True, "enable_knowledge_graph": False},
        "enable_knowledge_graph",
        True,
    ) is False


def test_shipped_config_uses_capability_flags_only() -> None:
    """
    The committed config must not keep legacy keys: a lingering ``enable_domain5`` would be dead
    weight, and worse, would keep the alias map alive after it was no longer needed.
    """
    import json
    from pathlib import Path

    config = json.loads(
        (Path(__file__).resolve().parents[1] / "orchestrator_config.json").read_text("utf-8")
    )
    integration = config["tools"]["integration"]
    for key in integration:
        if key.startswith("enable_"):
            assert not key.startswith("enable_domain"), f"legacy flag {key!r} still in config"
    assert {"enable_market", "enable_opportunity", "enable_legal"} <= set(integration)
    assert {"enable_org", "enable_documents", "enable_knowledge_graph"} <= set(integration)



def test_compliance_is_never_cleared_without_a_real_assessment(no_domains) -> None:
    """
    The single most dangerous fabrication in the old code: ``CLEARED`` was returned whenever two
    keyword lists found no match. A compliance clearance must only ever come from Domain 9.
    """
    result = ToolRegistry.check_legal_compliance(
        "an entirely unrelated scope with no trigger words", caller_agent="LEGAL_REGULATORY"
    )
    assert result["compliance_status"] == "UNKNOWN"
    assert result["compliance_status"] != "CLEARED"
    assert result["requires_human_signoff"] is True
    assert result["detected_regulations"] == [], "regulations must not be asserted without evidence"


def test_rfp_fallback_refuses_to_recommend(no_domains) -> None:
    """A bid/no-bid decision from a keyword heuristic is not a defensible business decision."""
    result = ToolRegistry.evaluate_rfp_fit(
        "Tender", ["data sovereignty controls", "unrelated staffing"],
        caller_agent="OPPORTUNITY_RISK",
    )
    assert result["recommendation"] == "INSUFFICIENT_DATA"
    assert result["recommendation"] not in ("GO", "NO_GO")
    assert result["data_status"] == DataStatus.FALLBACK.value


def test_keyword_flags_can_raise_but_never_clear(no_domains) -> None:
    """Keywords may surface a *possible* obligation; they must never discharge one."""
    flagged = ToolRegistry.check_legal_compliance(
        "we will transfer data and deploy an autonomous agent", caller_agent="LEGAL_REGULATORY"
    )
    assert flagged["compliance_flags"], "a clear trigger must still raise a flag"
    assert flagged["compliance_status"] == "UNKNOWN"
    assert flagged["requires_human_signoff"] is True


def test_demo_payload_is_off_by_default(no_domains) -> None:
    """§41: the labelled demo payload must require an explicit opt-in, never be implicit."""
    assert ToolRegistry._demo_enabled() is False
    result = ToolRegistry.query_knowledge_graph("Acme", caller_agent="MARKET_INTELLIGENCE")
    assert all(not rel.get("is_demo_data") for rel in result["relationships"])

