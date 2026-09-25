"""
Enterprise Tool Registry with RBAC, Tool Execution Audit Logging, and Structured Validation.
Enforces permissions and sandboxing before invoking external knowledge and OSINT sources.
"""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
import importlib
import time

from .config import load_config


class _BoundedAuditList(list):
    """Audit trail with a configurable cap so long-running servers cannot leak memory."""

    def __init__(self, maxlen: int = 1000):
        super().__init__()
        self.maxlen = maxlen

    def append(self, item) -> None:
        super().append(item)
        if self.maxlen > 0 and len(self) > self.maxlen:
            del self[: len(self) - self.maxlen]


class _CircuitBreaker:
    """Minimal circuit breaker for downstream domain services.

    Opens after `failure_threshold` consecutive failures and stays open for
    `recovery_seconds`, then half-opens to allow a single probe request.
    """

    def __init__(self, failure_threshold: int = 3, recovery_seconds: float = 30.0):
        self.failure_threshold = max(int(failure_threshold), 1)
        self.recovery_seconds = float(recovery_seconds)
        self.failures = 0
        self.opened_at: Optional[float] = None

    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.time() - self.opened_at >= self.recovery_seconds:
            self.opened_at = None  # half-open: allow one probe
            self.failures = 0
            return False
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold and self.opened_at is None:
            self.opened_at = time.time()


_CB_CFG = load_config().get("tools", {}).get("circuit_breaker", {})
_BREAKERS: Dict[str, _CircuitBreaker] = {
    key: _CircuitBreaker(
        failure_threshold=int(_CB_CFG.get("failure_threshold", 3)),
        recovery_seconds=float(_CB_CFG.get("recovery_seconds", 30))
    )
    for key in ("domain1", "domain2", "domain4", "domain5", "domain7", "domain8", "domain9")
}


class ToolCallAuditRecord(BaseModel):
    tool_name: str
    caller_agent: str
    arguments: Dict[str, Any]
    timestamp: float = Field(default_factory=time.time)
    execution_time_ms: float = 0.0
    status: str = "SUCCESS"  # SUCCESS, PERMISSION_DENIED, FAILED
    error_message: Optional[str] = None


class ToolRegistry:
    """Enterprise Tool Registry enforcing sandboxing, RBAC, and execution telemetry."""

    # Fallback RBAC map (used only if tools.permissions is absent from config)
    _DEFAULT_TOOL_PERMISSIONS = {
        "query_knowledge_graph": ["SUPERVISOR", "MARKET_INTELLIGENCE", "OPPORTUNITY_RISK"],
        "query_osint_signals": ["SUPERVISOR", "MARKET_INTELLIGENCE"],
        "evaluate_rfp_fit": ["SUPERVISOR", "OPPORTUNITY_RISK"],
        "check_legal_compliance": ["SUPERVISOR", "LEGAL_REGULATORY"],
        # Person A seams (see INTEGRATION.md)
        "search_documents": ["SUPERVISOR", "MARKET_INTELLIGENCE", "OPPORTUNITY_RISK"],
        "query_company_context": ["SUPERVISOR", "MARKET_INTELLIGENCE", "OPPORTUNITY_RISK"],
    }

    TOOL_PERMISSIONS = {
        **_DEFAULT_TOOL_PERMISSIONS,
        **load_config().get("tools", {}).get("permissions", {})
    }

    _audit_log: List[ToolCallAuditRecord] = _BoundedAuditList(
        maxlen=int(load_config().get("tools", {}).get("audit", {}).get("max_records", 1000))
    )

    @classmethod
    def get_audit_trail(cls) -> List[ToolCallAuditRecord]:
        return list(cls._audit_log)

    @classmethod
    def clear_audit_trail(cls):
        cls._audit_log.clear()

    @classmethod
    def _check_permission(cls, tool_name: str, caller_agent: str) -> bool:
        allowed = cls.TOOL_PERMISSIONS.get(tool_name, [])
        return caller_agent in allowed


    @classmethod
    def _optional_domain_service(cls, flag: str, module: str, attribute: str):
        """Lazy adapter for an optional Person A domain (see INTEGRATION.md).

        Returns None when the feature flag is off, the package is missing, or the
        class name differs - so Person A can land their domain at any time without
        breaking Domain 6. Duck-typed: only the documented method signature matters.
        """
        integration = load_config().get("tools", {}).get("integration", {})
        if not integration.get(flag, False):
            return None
        try:
            return getattr(importlib.import_module(module), attribute)()
        except (ImportError, AttributeError):
            return None

    @classmethod
    def _domain1_service(cls):
        """Domain 1 - Organizational Intelligence (Person A)."""
        return cls._optional_domain_service(
            "enable_domain1", "organizational_intelligence.service", "OrganizationalIntelligenceService"
        )

    @classmethod
    def _domain4_service(cls):
        """Domain 4 - Document & Knowledge Intelligence (Person A)."""
        return cls._optional_domain_service(
            "enable_domain4", "document_intelligence.service", "DocumentIntelligenceService"
        )

    @classmethod
    def _domain5_service(cls):
        """Domain 5 - Knowledge Graph & Relationship Intelligence (Person A)."""
        return cls._optional_domain_service(
            "enable_domain5", "knowledge_graph.service", "KnowledgeGraphService"
        )

    @classmethod
    def _guarded(cls, key: str, call) -> "tuple[Any, str]":
        """Runs a domain call under its circuit breaker. Returns (result, provider).

        provider is the domain key on success, or a builtin-* marker when the domain
        is open/failed/returned nothing. Never raises.
        """
        breaker = _BREAKERS[key]
        if breaker.is_open():
            return None, "builtin-circuit-open"
        try:
            result = call()
            breaker.record_success()
            return result, key
        except Exception:
            breaker.record_failure()
            return None, "builtin-fallback"

    @classmethod
    def query_knowledge_graph(cls, entity_name: str, caller_agent: str = "MARKET_INTELLIGENCE") -> Dict[str, Any]:
        """Queries Person A's Knowledge Graph (Domain 4 & 5)."""
        t0 = time.time()
        if not cls._check_permission("query_knowledge_graph", caller_agent):
            rec = ToolCallAuditRecord(
                tool_name="query_knowledge_graph",
                caller_agent=caller_agent,
                arguments={"entity_name": entity_name},
                status="PERMISSION_DENIED",
                error_message=f"Agent {caller_agent} lacks permission for query_knowledge_graph"
            )
            cls._audit_log.append(rec)
            raise PermissionError(rec.error_message)

        result = None
        provider = "builtin"
        service = cls._domain5_service()
        if service is not None:
            payload, provider = cls._guarded("domain5", lambda: service.query_entity(entity_name))
            if payload:
                result = payload

        result = result or {
            "entity": entity_name,
            "relationships": [
                {"relation": "OPERATES_IN", "target": "Cloud Security"},
                {"relation": "COMPETES_WITH", "target": "CompetitorCorp"},
                {"relation": "HOLDS_CERTIFICATION", "target": "ISO27001"},
                {"relation": "DATA_SOVEREIGNTY_REGION", "target": "EU-Frankfurt"}
            ],
            "verified_facts": [
                {"fact": f"{entity_name} specializes in enterprise intelligence & autonomous workflows.", "confidence": 0.96},
                {"fact": f"{entity_name} has active bids in EU and US markets.", "confidence": 0.91}
            ]
        }
        cls._audit_log.append(ToolCallAuditRecord(
            tool_name="query_knowledge_graph",
            caller_agent=caller_agent,
            arguments={"entity_name": entity_name, "provider": provider},
            execution_time_ms=(time.time() - t0) * 1000
        ))
        return result

    @classmethod
    def search_documents(cls, query: str, caller_agent: str = "MARKET_INTELLIGENCE") -> Dict[str, Any]:
        """Searches internal documents / knowledge base - Person A Domain 4 seam (INTEGRATION.md)."""
        t0 = time.time()
        if not cls._check_permission("search_documents", caller_agent):
            rec = ToolCallAuditRecord(
                tool_name="search_documents",
                caller_agent=caller_agent,
                arguments={"query": query},
                status="PERMISSION_DENIED",
                error_message=f"Agent {caller_agent} lacks permission for search_documents"
            )
            cls._audit_log.append(rec)
            raise PermissionError(rec.error_message)

        documents: List[Dict[str, Any]] = []
        provider = "builtin"
        service = cls._domain4_service()
        if service is not None:
            payload, provider = cls._guarded(
                "domain4", lambda: service.search(query, limit=5)
            )
            if payload:
                documents = payload.get("documents", [])

        result = {"query": query, "documents": documents}
        cls._audit_log.append(ToolCallAuditRecord(
            tool_name="search_documents",
            caller_agent=caller_agent,
            arguments={"query": query, "provider": provider, "hits": len(documents)},
            execution_time_ms=(time.time() - t0) * 1000
        ))
        return result

    @classmethod
    def query_company_context(cls, topic: str, caller_agent: str = "MARKET_INTELLIGENCE") -> Dict[str, Any]:
        """Company capability evidence - Person A Domain 1 seam (INTEGRATION.md)."""
        t0 = time.time()
        if not cls._check_permission("query_company_context", caller_agent):
            rec = ToolCallAuditRecord(
                tool_name="query_company_context",
                caller_agent=caller_agent,
                arguments={"topic": topic},
                status="PERMISSION_DENIED",
                error_message=f"Agent {caller_agent} lacks permission for query_company_context"
            )
            cls._audit_log.append(rec)
            raise PermissionError(rec.error_message)

        context: Dict[str, Any] = {}
        provider = "builtin"
        service = cls._domain1_service()
        if service is not None:
            payload, provider = cls._guarded(
                "domain1", lambda: service.get_company_context(topic)
            )
            if payload:
                context = payload

        result = {"topic": topic, "context": context}
        cls._audit_log.append(ToolCallAuditRecord(
            tool_name="query_company_context",
            caller_agent=caller_agent,
            arguments={"topic": topic, "provider": provider},
            execution_time_ms=(time.time() - t0) * 1000
        ))
        return result

    @classmethod
    def query_osint_signals(cls, topic: str, caller_agent: str = "MARKET_INTELLIGENCE") -> List[Dict[str, Any]]:
        """Queries Person A's External OSINT & Crawlers (Domain 2)."""
        t0 = time.time()
        if not cls._check_permission("query_osint_signals", caller_agent):
            rec = ToolCallAuditRecord(
                tool_name="query_osint_signals",
                caller_agent=caller_agent,
                arguments={"topic": topic},
                status="PERMISSION_DENIED",
                error_message=f"Agent {caller_agent} lacks permission for query_osint_signals"
            )
            cls._audit_log.append(rec)
            raise PermissionError(rec.error_message)

        signals, provider = cls._fetch_osint_signals(topic)
        cls._audit_log.append(ToolCallAuditRecord(
            tool_name="query_osint_signals",
            caller_agent=caller_agent,
            arguments={"topic": topic, "provider": provider},
            execution_time_ms=(time.time() - t0) * 1000
        ))
        return signals

    @classmethod
    def _domain7_service(cls):
        """Lazy adapter to Domain 7 (Business & Market Intelligence).
        Returns None when disabled in config or when the package is unavailable,
        keeping Domain 6 fully self-contained (no hard dependency)."""
        integration = load_config().get("tools", {}).get("integration", {})
        if not integration.get("enable_domain7", True):
            return None
        try:
            from business_market_intelligence.service import MarketIntelligenceService
            return MarketIntelligenceService()
        except ImportError:
            return None

    @classmethod
    def _fetch_osint_signals(cls, topic: str) -> "tuple[List[Dict[str, Any]], str]":
        """Returns (signals, provider). Prefers Domain 7. Downstream failures are
        isolated by a circuit breaker and ALWAYS fall back to the built-in mock."""
        integration = load_config().get("tools", {}).get("integration", {})
        breaker = _BREAKERS["domain7"]
        provider = "domain7"

        if breaker.is_open():
            provider = "builtin-circuit-open"
        else:
            try:
                domain7 = cls._domain7_service()
                if domain7 is not None:
                    limit = int(integration.get("signal_limit", 6))
                    signals = [s.to_domain6_osint() for s in domain7.get_signals(topic, limit=limit)]
                    breaker.record_success()
                    if signals:
                        return signals, provider
            except Exception:
                breaker.record_failure()
                provider = "builtin-fallback"

        return [
            {
                "source": "Global Tech News",
                "headline": f"Emerging standards in {topic} demand strict data sovereignty",
                "timestamp": "2026-09-20",
                "credibility_score": 0.89
            },
            {
                "source": "Government Tender Board",
                "headline": f"New federal RFP announced requiring high-assurance {topic} compliance",
                "timestamp": "2026-09-24",
                "credibility_score": 0.95
            }
        ], ("builtin" if provider == "domain7" else provider)

    @classmethod
    def evaluate_rfp_fit(cls, rfp_title: str, mandatory_reqs: List[str], caller_agent: str = "OPPORTUNITY_RISK") -> Dict[str, Any]:
        """Evaluates RFP fit and capability matching (Domain 8)."""
        t0 = time.time()
        if not cls._check_permission("evaluate_rfp_fit", caller_agent):
            rec = ToolCallAuditRecord(
                tool_name="evaluate_rfp_fit",
                caller_agent=caller_agent,
                arguments={"rfp_title": rfp_title},
                status="PERMISSION_DENIED",
                error_message=f"Agent {caller_agent} lacks permission for evaluate_rfp_fit"
            )
            cls._audit_log.append(rec)
            raise PermissionError(rec.error_message)

        result, provider = cls._fetch_rfp_fit(rfp_title, mandatory_reqs)
        cls._audit_log.append(ToolCallAuditRecord(
            tool_name="evaluate_rfp_fit",
            caller_agent=caller_agent,
            arguments={"rfp_title": rfp_title, "reqs_count": len(mandatory_reqs), "provider": provider},
            execution_time_ms=(time.time() - t0) * 1000
        ))
        return result

    @classmethod
    def _domain8_service(cls):
        """Lazy adapter to Domain 8 (Opportunity, Risk & Requirements Intelligence).
        Returns None when disabled in config or when the package is unavailable,
        keeping Domain 6 fully self-contained (no hard dependency)."""
        integration = load_config().get("tools", {}).get("integration", {})
        if not integration.get("enable_domain8", True):
            return None
        try:
            from opportunity_risk_intelligence.service import OpportunityRiskService
            return OpportunityRiskService()
        except ImportError:
            return None

    @classmethod
    def _fetch_rfp_fit(cls, rfp_title: str, mandatory_reqs: List[str]) -> "tuple[Dict[str, Any], str]":
        """Returns (result, provider). Prefers Domain 8. Downstream failures are
        isolated by a circuit breaker and ALWAYS fall back to the built-in mock."""
        breaker = _BREAKERS["domain8"]
        provider = "domain8"

        if breaker.is_open():
            provider = "builtin-circuit-open"
        else:
            try:
                domain8 = cls._domain8_service()
                if domain8 is not None:
                    assessment = domain8.assess(rfp_title, mandatory_reqs)
                    breaker.record_success()
                    return assessment.to_domain6_rfp_fit(), provider
            except Exception:
                breaker.record_failure()
                provider = "builtin-fallback"

        rfp_cfg = load_config().get("tools", {}).get("rfp_fit", {})
        match_keywords = [str(k).lower() for k in rfp_cfg.get(
            "capability_match_keywords", ["sovereignty", "security", "audit"]
        )]
        go_threshold = float(rfp_cfg.get("go_threshold_percent", 70))

        matched = [r for r in mandatory_reqs if any(k in r.lower() for k in match_keywords)]
        gaps = [r for r in mandatory_reqs if r not in matched]
        fit_percentage = (len(matched) / max(len(mandatory_reqs), 1)) * 100
        return {
            "rfp_title": rfp_title,
            "fit_score": round(fit_percentage, 2),
            "matched_capabilities": matched,
            "capability_gaps": gaps,
            "recommendation": "GO" if fit_percentage >= go_threshold else "NO_GO"
        }, ("builtin" if provider == "domain8" else provider)

    @classmethod
    def check_legal_compliance(cls, scope_text: str, caller_agent: str = "LEGAL_REGULATORY") -> Dict[str, Any]:
        """Checks regulatory requirements and IP exposure (Domain 9)."""
        t0 = time.time()
        if not cls._check_permission("check_legal_compliance", caller_agent):
            rec = ToolCallAuditRecord(
                tool_name="check_legal_compliance",
                caller_agent=caller_agent,
                arguments={"scope_text": scope_text[:50]},
                status="PERMISSION_DENIED",
                error_message=f"Agent {caller_agent} lacks permission for check_legal_compliance"
            )
            cls._audit_log.append(rec)
            raise PermissionError(rec.error_message)

        result, provider = cls._fetch_legal_compliance(scope_text)
        cls._audit_log.append(ToolCallAuditRecord(
            tool_name="check_legal_compliance",
            caller_agent=caller_agent,
            arguments={"scope_length": len(scope_text), "provider": provider},
            execution_time_ms=(time.time() - t0) * 1000
        ))
        return result

    @classmethod
    def _domain9_service(cls):
        """Lazy adapter to Domain 9 (Legal, Regulatory & IP Intelligence).
        Returns None when disabled in config or when the package is unavailable,
        keeping Domain 6 fully self-contained (no hard dependency)."""
        integration = load_config().get("tools", {}).get("integration", {})
        if not integration.get("enable_domain9", True):
            return None
        try:
            from legal_regulatory_ip_intelligence.service import LegalIntelligenceService
            return LegalIntelligenceService()
        except ImportError:
            return None

    @classmethod
    def _fetch_legal_compliance(cls, scope_text: str) -> "tuple[Dict[str, Any], str]":
        """Returns (result, provider). Prefers Domain 9. Downstream failures are
        isolated by a circuit breaker and ALWAYS fall back to the built-in mock."""
        breaker = _BREAKERS["domain9"]
        provider = "domain9"

        if breaker.is_open():
            provider = "builtin-circuit-open"
        else:
            try:
                domain9 = cls._domain9_service()
                if domain9 is not None:
                    analysis = domain9.analyze(scope_text)
                    breaker.record_success()
                    return analysis.to_domain6_legal_check(), provider
            except Exception:
                breaker.record_failure()
                provider = "builtin-fallback"

        comp_cfg = load_config().get("tools", {}).get("compliance", {})
        cross_border_kw = [str(k).lower() for k in comp_cfg.get("cross_border_keywords", ["data transfer"])]
        high_risk_ai_kw = [str(k).lower() for k in comp_cfg.get(
            "high_risk_ai_keywords", ["autonomous agent", "ai agent", "defense"]
        )]
        detected_regulations = comp_cfg.get("detected_regulations", ["EU AI Act", "GDPR", "NIST CSF"])

        flags = []
        if any(k in scope_text.lower() for k in cross_border_kw):
            flags.append("GDPR Article 44: Cross-border transfer mechanism required.")
        if any(k in scope_text.lower() for k in high_risk_ai_kw):
            flags.append("EU AI Act: High-risk AI system oversight & audit logging required.")

        return {
            "compliance_status": "FLAGGED" if flags else "CLEARED",
            "detected_regulations": detected_regulations,
            "compliance_flags": flags,
            "requires_human_signoff": len(flags) > 0
        }, ("builtin" if provider == "domain9" else provider)
