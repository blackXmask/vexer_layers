"""
Enterprise Tool Registry with RBAC, Tool Execution Audit Logging, and Structured Validation.

Thin, backwards-compatible facade over :mod:`agent_orchestrator.bus`. Every classmethod delegates to
a :class:`~agent_orchestrator.bus.ToolBus` that owns the breakers, the audit sink and the RBAC
policy.

**What changed in P7, and why** (three real defects, not stylistic preferences):

1. **No import-time configuration.** The breaker registry, the RBAC map and the audit cap were
   evaluated when this module was first imported, so ``VEXER_CONFIG_PATH`` and test config swaps had
   no effect without a process restart, and a broken config file crashed at *import*. The default bus
   is now built lazily on first use and owns its config.
2. **State is instance-owned and injectable.** The module-level ``_BREAKERS`` dict and class-level
   ``_audit_log`` were shared by every caller *and* every worker process, so a horizontally scaled
   deployment had one circuit breaker per process and an audit trail that was only a fragment.
   :class:`ToolBus` owns its state, reports the backend's ``distribution`` in :meth:`bus_health`, and
   accepts a Postgres-backed sink/registry once the deployment outgrows one node.
3. **Every result declares its provenance.** Each tool returns a ``data_status`` from the shared
   :class:`~vexer_platform.contracts.DataStatus` vocabulary (§19), so a degraded or absent source
   can never be mistaken for live intelligence.

**Fabricated intelligence was removed.** The fallbacks used to invent records — relationships,
tenders, competitor names — with hardcoded credibility scores. Inventing analysis and presenting it
as intelligence is the exact failure this platform exists to prevent (§41, §52), so the default path
returns an empty result tagged ``UNAVAILABLE``. A clearly-labelled demo payload remains available
behind ``tools.allow_demo_data`` for local demonstrations only.
"""
from typing import Any, Dict, List, Optional
import time

from vexer_platform.contracts import DataStatus

from .bus import ToolAuditRecord, ToolBus

#: Backwards-compatible alias. The record type moved to :mod:`agent_orchestrator.bus` and gained
#: ``provider`` / ``data_status``; the old name is kept so existing imports keep working.
ToolCallAuditRecord = ToolAuditRecord

__all__ = ["ToolCallAuditRecord", "ToolRegistry"]


class ToolRegistry:
    """
    Enterprise Tool Registry enforcing sandboxing, RBAC, and execution telemetry.

    The classmethods remain for historical API compatibility. They hold no state themselves: state
    lives in the bus returned by :meth:`get_bus`, created on first use and replaceable via
    :meth:`set_bus` — the seam a worker, a test, or a multi-tenant deployment uses to obtain an
    isolated bus.
    """

    _bus: Optional[ToolBus] = None

    # -- bus lifecycle ----------------------------------------------------------------------------

    @classmethod
    def get_bus(cls) -> ToolBus:
        """
        The default bus, built lazily.

        Deliberately lazy: building it at import time is precisely the defect this refactor removed,
        because it would freeze configuration into module import.
        """
        if cls._bus is None:
            cls._bus = ToolBus.from_config()
        return cls._bus

    @classmethod
    def set_bus(cls, bus: Optional[ToolBus]) -> None:
        """
        Replace the default bus (dependency injection).

        ``None`` resets to lazy construction. Tests use this for isolation; a worker that owns a
        shared (Postgres-backed) bus injects it at startup.
        """
        cls._bus = bus

    @classmethod
    def bus_health(cls) -> Dict[str, Any]:
        """Runtime health, including whether breaker/audit state is per-process or shared (§28)."""
        return cls.get_bus().health()



    # -- audit ------------------------------------------------------------------------------------

    @classmethod
    def get_audit_trail(cls) -> List[ToolCallAuditRecord]:
        return cls.get_bus().audit_trail()

    @classmethod
    def clear_audit_trail(cls) -> None:
        cls.get_bus().clear_audit_trail()

    @classmethod
    def _check_permission(cls, tool_name: str, caller_agent: str) -> bool:
        return cls.get_bus().check_permission(tool_name, caller_agent)

    @classmethod
    def _audit(
        cls,
        *,
        tool_name: str,
        caller_agent: str,
        arguments: Dict[str, Any],
        provider: str = "unknown",
        data_status: str = DataStatus.UNAVAILABLE.value,
        status: str = "SUCCESS",
        error_message: Optional[str] = None,
        t0: Optional[float] = None,
    ) -> None:
        """Record one audited call through the bus (never raises)."""
        cls.get_bus().record(
            ToolCallAuditRecord(
                tool_name=tool_name,
                caller_agent=caller_agent,
                arguments=arguments,
                status=status,
                provider=provider,
                data_status=data_status,
                error_message=error_message,
                execution_time_ms=((time.time() - t0) * 1000) if t0 is not None else 0.0,
            )
        )

    @classmethod
    def _denied(cls, tool_name: str, caller_agent: str) -> "PermissionError":
        """
        Build an RBAC rejection: audit it, then raise.

        Auditing *before* raising matters — a denied call is exactly the event a reviewer needs to
        see, and raising first would lose it.
        """
        message = f"Agent {caller_agent} lacks permission for {tool_name}"
        cls._audit(
            tool_name=tool_name,
            caller_agent=caller_agent,
            arguments={"caller_agent": caller_agent},
            status="PERMISSION_DENIED",
            error_message=message,
        )
        return PermissionError(message)

    @classmethod
    def _demo_enabled(cls) -> bool:
        """
        Whether the labelled demo payload is permitted (§41).

        Off by default. It exists so a local demonstration has something to render, and it must
        never be reachable implicitly: an operator can switch it on, nothing turns it on for them.
        """
        from .config import load_config

        return bool((load_config().get("tools", {}) or {}).get("allow_demo_data", False))


    # -- downstream services ----------------------------------------------------------------------

    @classmethod
    def _service(cls, key: str) -> Any:
        """
        Resolve a downstream domain service through the bus.

        All domain lookup (feature flags, lazy import, optional-package tolerance) now lives in one
        place, :func:`agent_orchestrator.bus._default_service_resolver`, so four near-identical
        adapters in this class collapsed into a single lookup and the seams cannot drift apart.
        """
        return cls.get_bus().service(key)

    @classmethod
    def _guarded(cls, key: str, call) -> "tuple[Any, str, str]":
        """Run a downstream call under its breaker via the bus. Never raises."""
        return cls.get_bus().guarded(key, call)

    @classmethod
    def query_knowledge_graph(
        cls, entity_name: str, caller_agent: str = "MARKET_INTELLIGENCE"
    ) -> Dict[str, Any]:
        """
        Knowledge-graph relationships for an entity (Person A Domain 5).

        When Domain 5 is absent the result is **empty and tagged** ``UNAVAILABLE`` rather than
        populated with invented relationships. The previous implementation returned a fixed set of
        relationships and "verified facts" with hardcoded confidence scores (0.96, 0.91) for any
        entity name at all — fabricated intelligence that looked identical to a real answer, which
        is precisely what §41 and §52 forbid. Callers can see the difference in ``data_status``.
        """
        t0 = time.time()
        if not cls._check_permission("query_knowledge_graph", caller_agent):
            raise cls._denied("query_knowledge_graph", caller_agent)

        provider = "unavailable:domain5"
        data_status = DataStatus.UNAVAILABLE.value
        relationships: List[Dict[str, Any]] = []
        verified: List[Dict[str, Any]] = []

        service = cls._service("domain5")
        if service is not None:
            payload, provider, data_status = cls._guarded(
                "domain5", lambda: service.query_entity(entity_name)
            )
            if isinstance(payload, dict):
                relationships = list(payload.get("relationships", []) or [])
                verified = list(payload.get("verified_facts", []) or [])
            elif isinstance(payload, list):
                relationships = list(payload)
        elif cls._demo_enabled():
            # Clearly-labelled local demo only; never reachable without an explicit opt-in.
            provider = "demo:domain5"
            data_status = DataStatus.FALLBACK.value
            relationships = [
                {"relation": "DEMO_RELATION", "target": "DEMO_ENTITY", "is_demo_data": True}
            ]

        result = {
            "entity": entity_name,
            "relationships": relationships,
            "verified_facts": verified,
            "provider": provider,
            "data_status": data_status,
        }
        cls._audit(
            tool_name="query_knowledge_graph",
            caller_agent=caller_agent,
            arguments={"entity_name": entity_name, "provider": provider,
                       "relationships": len(relationships)},
            provider=provider,
            data_status=data_status,
            t0=t0,
        )
        return result


    @classmethod
    def search_documents(cls, query: str, caller_agent: str = "MARKET_INTELLIGENCE") -> Dict[str, Any]:
        """
        Search internal documents — Person A Domain 4 seam (INTEGRATION.md).

        Absent Domain 4 this returns an empty, ``UNAVAILABLE``-tagged result. Documents are evidence,
        so inventing them would corrupt provenance rather than merely look wrong.
        """
        t0 = time.time()
        if not cls._check_permission("search_documents", caller_agent):
            raise cls._denied("search_documents", caller_agent)

        documents: List[Dict[str, Any]] = []
        provider = "unavailable:domain4"
        data_status = DataStatus.UNAVAILABLE.value
        service = cls._service("domain4")
        if service is not None:
            payload, provider, data_status = cls._guarded(
                "domain4", lambda: service.search(query, limit=5)
            )
            if isinstance(payload, dict):
                documents = list(payload.get("documents", []) or [])
            elif isinstance(payload, list):
                documents = list(payload)

        result = {
            "query": query,
            "documents": documents,
            "provider": provider,
            "data_status": data_status,
        }
        cls._audit(
            tool_name="search_documents",
            caller_agent=caller_agent,
            arguments={"query": query, "provider": provider, "hits": len(documents)},
            provider=provider,
            data_status=data_status,
            t0=t0,
        )
        return result

    @classmethod
    def query_company_context(cls, topic: str, caller_agent: str = "MARKET_INTELLIGENCE") -> Dict[str, Any]:
        """
        Company capability evidence — Person A Domain 1 seam (INTEGRATION.md).

        Absent Domain 1 this returns an empty, ``UNAVAILABLE``-tagged result. Company capability
        claims are exactly the kind of assertion that must be evidence-backed, so a placeholder would
        be worse than an explicit gap.
        """
        t0 = time.time()
        if not cls._check_permission("query_company_context", caller_agent):
            raise cls._denied("query_company_context", caller_agent)

        context: Dict[str, Any] = {}
        provider = "unavailable:domain1"
        data_status = DataStatus.UNAVAILABLE.value
        service = cls._service("domain1")
        if service is not None:
            payload, provider, data_status = cls._guarded(
                "domain1", lambda: service.get_company_context(topic)
            )
            if isinstance(payload, dict):
                context = dict(payload)

        result = {
            "topic": topic,
            "context": context,
            "provider": provider,
            "data_status": data_status,
        }
        cls._audit(
            tool_name="query_company_context",
            caller_agent=caller_agent,
            arguments={"topic": topic, "provider": provider},
            provider=provider,
            data_status=data_status,
            t0=t0,
        )
        return result

    @classmethod
    def query_osint_signals(cls, topic: str, caller_agent: str = "MARKET_INTELLIGENCE") -> List[Dict[str, Any]]:
        """
        External intelligence signals — Person A Domain 2, with Domain 7 as the implemented source.

        Returns a list because that is the established Domain 6 contract. When no source is available
        the list is **empty**; the audit record carries the ``data_status`` explaining why, and an
        empty signal set is a visible absence rather than an invented one.
        """
        t0 = time.time()
        if not cls._check_permission("query_osint_signals", caller_agent):
            raise cls._denied("query_osint_signals", caller_agent)

        signals, provider, data_status = cls._fetch_osint_signals(topic)
        cls._audit(
            tool_name="query_osint_signals",
            caller_agent=caller_agent,
            arguments={"topic": topic, "provider": provider, "count": len(signals)},
            provider=provider,
            data_status=data_status,
            t0=t0,
        )
        return signals

    @classmethod
    def _fetch_osint_signals(cls, topic: str) -> "tuple[List[Dict[str, Any]], str, str]":
        """
        Returns ``(signals, provider, data_status)``, preferring Domain 7.

        When Domain 7 is unavailable or failing this returns an **empty** list tagged
        ``UNAVAILABLE``/``FAILED``/``DEGRADED``. It previously returned two invented signals — a
        fabricated "Global Tech News" headline and a fabricated "Government Tender Board" RFP, each
        with a hardcoded credibility score of 0.89/0.95 and a hardcoded date. Those numbers are the
        worst kind of fabrication: they *look* like a calibrated confidence, so a downstream consumer
        would treat them as measured. Removing them is a deliberate behaviour change (§41, §52).
        """
        integration = cls.get_bus().integration_settings()
        service = cls._service("domain7")
        if service is None:
            return [], "unavailable:domain7", DataStatus.UNAVAILABLE.value

        limit = int(integration.get("signal_limit", 6))
        payload, provider, data_status = cls._guarded(
            "domain7", lambda: [s.to_domain6_osint() for s in service.get_signals(topic, limit=limit)]
        )
        if data_status == DataStatus.LIVE.value and not payload:
            # The domain answered and found nothing. That is a real result, not a failure.
            return [], f"{provider}:empty", DataStatus.LIVE.value
        return list(payload or []), provider, data_status

    @classmethod
    def evaluate_rfp_fit(cls, rfp_title: str, mandatory_reqs: List[str], caller_agent: str = "OPPORTUNITY_RISK") -> Dict[str, Any]:
        """Evaluates RFP fit and capability matching (Domain 8)."""
        t0 = time.time()
        if not cls._check_permission("evaluate_rfp_fit", caller_agent):
            raise cls._denied("evaluate_rfp_fit", caller_agent)

        result, provider, data_status = cls._fetch_rfp_fit(rfp_title, mandatory_reqs)
        result.setdefault("provider", provider)
        result.setdefault("data_status", data_status)
        cls._audit(
            tool_name="evaluate_rfp_fit",
            caller_agent=caller_agent,
            arguments={"rfp_title": rfp_title, "reqs_count": len(mandatory_reqs),
                       "provider": provider},
            provider=provider,
            data_status=data_status,
            t0=t0,
        )
        return result

    @classmethod
    def _fetch_rfp_fit(
        cls, rfp_title: str, mandatory_reqs: List[str]
    ) -> "tuple[Dict[str, Any], str, str]":
        """
        Returns ``(result, provider, data_status)``, preferring Domain 8.

        When Domain 8 is unavailable, a keyword-overlap heuristic still reports which requirements
        matched — that part is genuinely informative. It does **not** issue a ``GO``/``NO_GO``: a
        bid/no-bid recommendation derived from three keywords is not a defensible business decision,
        and the previous implementation returned one. The recommendation is ``INSUFFICIENT_DATA``
        and the result is tagged ``FALLBACK`` so a consumer cannot mistake it for an assessment.
        """
        service = cls._service("domain8")
        if service is not None:
            payload, provider, data_status = cls._guarded(
                "domain8", lambda: service.assess(rfp_title, mandatory_reqs).to_domain6_rfp_fit()
            )
            if data_status == DataStatus.LIVE.value and isinstance(payload, dict):
                return dict(payload), provider, data_status

        from .config import load_config

        rfp_cfg = (load_config().get("tools", {}) or {}).get("rfp_fit", {}) or {}
        match_keywords = [
            str(k).lower()
            for k in rfp_cfg.get("capability_match_keywords", ["sovereignty", "security", "audit"])
        ]
        matched = [r for r in mandatory_reqs if any(k in r.lower() for k in match_keywords)]
        gaps = [r for r in mandatory_reqs if r not in matched]
        fit_percentage = (len(matched) / max(len(mandatory_reqs), 1)) * 100
        return {
            "rfp_title": rfp_title,
            "fit_score": round(fit_percentage, 2),
            "matched_capabilities": matched,
            "capability_gaps": gaps,
            "recommendation": "INSUFFICIENT_DATA",
            "recommendation_basis": "keyword-overlap heuristic; no Domain 8 assessment available",
        }, "heuristic:domain8-unavailable", DataStatus.FALLBACK.value

    @classmethod
    def check_legal_compliance(cls, scope_text: str, caller_agent: str = "LEGAL_REGULATORY") -> Dict[str, Any]:
        """Checks regulatory requirements and IP exposure (Domain 9)."""
        t0 = time.time()
        if not cls._check_permission("check_legal_compliance", caller_agent):
            raise cls._denied("check_legal_compliance", caller_agent)

        result, provider, data_status = cls._fetch_legal_compliance(scope_text)
        result.setdefault("provider", provider)
        result.setdefault("data_status", data_status)
        cls._audit(
            tool_name="check_legal_compliance",
            caller_agent=caller_agent,
            arguments={"scope_length": len(scope_text), "provider": provider},
            provider=provider,
            data_status=data_status,
            t0=t0,
        )
        return result

    @classmethod
    def _fetch_legal_compliance(cls, scope_text: str) -> "tuple[Dict[str, Any], str, str]":
        """
        Returns ``(result, provider, data_status)``, preferring Domain 9.

        When Domain 9 is unavailable this returns ``compliance_status = "UNKNOWN"`` — never
        ``"CLEARED"``. The previous implementation returned ``CLEARED`` whenever two keyword lists
        found no match, which is the most dangerous failure in this codebase: a compliance clearance
        asserted from a substring scan would let a business ship something unlawful, and a false
        negative in compliance costs far more than a false positive. It also reported a hardcoded
        ``["EU AI Act", "GDPR", "NIST CSF"]`` as *detected* regulations — asserting facts nobody
        checked.
        """
        service = cls._service("domain9")
        if service is not None:
            payload, provider, data_status = cls._guarded(
                "domain9", lambda: service.analyze(scope_text).to_domain6_legal_check()
            )
            if data_status == DataStatus.LIVE.value and isinstance(payload, dict):
                return dict(payload), provider, data_status

        # Keywords can still *raise* a flag — a possible obligation is worth surfacing. They can
        # never clear one.
        from .config import load_config

        comp_cfg = (load_config().get("tools", {}) or {}).get("compliance", {}) or {}
        cross_border_kw = [
            str(k).lower() for k in comp_cfg.get("cross_border_keywords", ["data transfer"])
        ]
        high_risk_kw = [
            str(k).lower()
            for k in comp_cfg.get(
                "high_risk_ai_keywords", ["autonomous agent", "ai agent", "defense"]
            )
        ]
        lowered = scope_text.lower()
        flags: List[str] = []
        if any(keyword in lowered for keyword in cross_border_kw):
            flags.append("GDPR Article 44: Cross-border transfer mechanism may be required.")
        if any(keyword in lowered for keyword in high_risk_kw):
            flags.append("EU AI Act: system may fall under high-risk AI obligations.")

        return {
            "compliance_status": "UNKNOWN",
            "detected_regulations": [],
            "compliance_flags": flags,
            "requires_human_signoff": True,
            "status_reason": "no Domain 9 assessment available; clearance cannot be asserted",
        }, "heuristic:domain9-unavailable", DataStatus.FALLBACK.value

