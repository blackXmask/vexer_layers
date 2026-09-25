"""
Runtime state for the Domain 6 tool bus (mandate Â§19, Â§26, Â§29, Â§34, Â§45, P7).

**Why this module exists.** ``tools.py`` previously held its circuit-breaker registry, its audit
trail and its RBAC map as *process-global mutable state* read from config **at import time**. Three
concrete defects followed, all recorded in the audit:

1. **Config was frozen at import.** ``TOOL_PERMISSIONS`` and the audit cap were evaluated when the
   module was first imported, so ``VEXER_CONFIG_PATH`` or a test config swap had no effect unless
   the process restarted. ``reload_config()`` existed but could not fix it.
2. **State was shared across every caller and every worker.** A FastAPI process running threaded
   sync endpoints mutated one module-level breaker dict from several threads, and a horizontally
   scaled deployment got a *separate* breaker per process â€” so the circuit protected one worker and
   nothing else, and ``/audit/tools`` showed only the audit slice held by the worker that answered
   the request.
3. **A missing config file crashed at import**, not at the point of use, so a bad deployment failed
   with an unhelpful ``ImportError`` instead of a structured error.

**What replaces it.** A :class:`ToolBus` instance that *owns* its state and is constructed
explicitly. :class:`ToolRegistry` in ``tools.py`` stays as a thin backwards-compatible facade
delegating to a lazily-built default bus, so every existing caller and test keeps working.

**The state is pluggable, and that is the point.** In-memory backends are correct for one worker
and explicitly *not* correct across several; PostgreSQL-backed ones are shared. Choosing a backend
is configuration, not a code change, which is what turns horizontal scaling into a deployment
decision instead of a rewrite.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from vexer_platform.contracts import DataStatus

__all__ = [
    "DOMAIN_KEYS",
    "LEGACY_KEY_ALIASES",
    "CircuitBreaker",
    "CircuitBreakers",
    "InMemoryAuditSink",
    "InMemoryCircuitBreakers",
    "ToolAuditRecord",
    "ToolBus",
    "canonical_key",
]

#: Downstream dependency keys guarded by a breaker. A new integration seam must be added here
#: deliberately, so it cannot end up uncallable-through-a-breaker by omission.
#:
#: These are **capability names, not domain numbers**, and that is deliberate. They used to be
#: ``domain1``…``domain9`` under the pre-split numbering, which became actively misleading once the
#: project was renumbered into ten domains: old ``domain4`` meant Document & Knowledge while new
#: "4" means Knowledge Graph, and old ``domain5`` meant Knowledge Graph while new "5" means
#: Evidence/Verification. A log line reading ``domain5`` would therefore be misread by anyone who had
#: the current map open. Names do not drift when the org chart does.
#:
#: The name is the capability; the owning person and the canonical domain number are documented
#: alongside it so the boundary stays explicit.
DOMAIN_KEYS: tuple[str, ...] = (
    "org",              # Person A · domain 1  Organizational Intelligence
    "osint",            # Person A · domain 2  External Intelligence & OSINT
    "documents",        # Person A · domain 3  Document & Knowledge Intelligence
    "knowledge_graph",  # Person A · domain 4  Knowledge Graph & Relationship Intelligence
    "market",           # Person B · domain 7  Business & Market Intelligence
    "opportunity",      # Person B · domain 8  Opportunity, Risk & Requirements
    "legal",            # Person B · domain 9  Legal, Regulatory & IP
)

#: Retired seam keys -> current key. Kept so an in-flight config or branch using the old names keeps
#: working during the transition instead of failing closed with a silently disabled integration.
#: Purely a migration aid: every internal call site uses the new names.
LEGACY_KEY_ALIASES: Dict[str, str] = {
    "domain1": "org",
    "domain2": "osint",
    "domain4": "documents",
    "domain5": "knowledge_graph",
    "domain7": "market",
    "domain8": "opportunity",
    "domain9": "legal",
}


def canonical_key(key: str) -> str:
    """Map a possibly-legacy key onto its current name."""
    return LEGACY_KEY_ALIASES.get(key, key)



@dataclass(frozen=True)
class ToolAuditRecord:
    """
    One audited tool call (mandate Â§45).

    ``provider`` and ``data_status`` are the fields that keep a fallback visible: a caller can always
    tell whether an answer came from a real domain or from a degraded path, instead of reading a
    shape-compatible record and assuming it is real.
    """

    tool_name: str
    caller_agent: str
    arguments: Dict[str, Any]
    status: str = "SUCCESS"           # SUCCESS | PERMISSION_DENIED | FAILED
    provider: str = "unknown"
    data_status: str = DataStatus.UNAVAILABLE.value
    error_message: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    execution_time_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "caller_agent": self.caller_agent,
            "arguments": dict(self.arguments),
            "status": self.status,
            "provider": self.provider,
            "data_status": self.data_status,
            "error_message": self.error_message,
            "timestamp": self.timestamp,
            "execution_time_ms": self.execution_time_ms,
        }


class CircuitBreaker:
    """
    Thread-safe circuit breaker for one downstream dependency.

    Deliberate design choices:

    * **No side effects in the predicate.** The previous implementation transitioned to half-open
      *inside* ``is_open()``, so merely inspecting breaker state mutated it, and two threads could
      both observe "half-open" and both send a probe. State now changes only in
      :meth:`allow_request` / :meth:`record_success` / :meth:`record_failure`, all under one lock.
    * **Monotonic clock.** :func:`time.monotonic` instead of :func:`time.time`, so an NTP step
      backwards cannot keep a breaker open forever or re-open a healthy one.
    * **Bounded probe.** Once the recovery window elapses exactly one in-flight probe is admitted;
      further callers are rejected until it resolves. A recovering dependency must not be stampeded
      by every waiting thread.
    """

    __slots__ = (
        "_lock", "_failures", "_opened_at", "_probe_in_flight",
        "failure_threshold", "recovery_seconds",
    )

    def __init__(self, failure_threshold: int = 3, recovery_seconds: float = 30.0) -> None:
        self.failure_threshold = max(int(failure_threshold), 1)
        self.recovery_seconds = max(float(recovery_seconds), 0.0)
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: Optional[float] = None
        self._probe_in_flight = False

    @property
    def state(self) -> str:
        """``closed`` | ``open`` | ``half_open`` â€” read-only, never mutates."""
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> str:
        if self._opened_at is None:
            return "closed"
        if (time.monotonic() - self._opened_at) >= self.recovery_seconds:
            return "half_open"
        return "open"

    def allow_request(self) -> bool:
        """
        Whether a call may proceed right now.

        ``True`` when closed, or when the recovery window has elapsed (a single probe). The probe slot
        is claimed under the same lock, so concurrent callers cannot both take it.
        """
        with self._lock:
            state = self._state_locked()
            if state == "closed":
                return True
            if state == "open":
                return False
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._probe_in_flight = False

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._probe_in_flight = False
            if self._failures >= self.failure_threshold:
                # A half-open probe that failed re-opens immediately and restarts the window.
                self._opened_at = time.monotonic()

    def reset(self) -> None:
        """Force closed. Used by tests and by operational recovery."""
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._probe_in_flight = False


class CircuitBreakers:
    """
    Registry of per-dependency breakers.

    The abstraction exists so the *storage* decision is configuration. An in-memory registry is
    correct for a single process and wrong for several: with N workers each process would hold its
    own breaker and the circuit would never trip globally. Subclasses therefore declare
    :attr:`distribution` honestly, and the bus surfaces it in its health output rather than letting
    a single-process deployment be mistaken for a shared one.
    """

    #: ``"single-process"`` or ``"shared"`` â€” surfaced in health output.
    distribution = "single-process"

    def breaker(self, key: str) -> CircuitBreaker:
        raise NotImplementedError

    def snapshot(self) -> Dict[str, str]:
        """Breaker states for health/diagnostics."""
        return {key: self.breaker(key).state for key in DOMAIN_KEYS}


class InMemoryCircuitBreakers(CircuitBreakers):
    """Single-process, thread-safe breaker registry (the default)."""

    distribution = "single-process"

    def __init__(self, failure_threshold: int = 3, recovery_seconds: float = 30.0) -> None:
        self._lock = threading.Lock()
        self._breakers: Dict[str, CircuitBreaker] = {
            key: CircuitBreaker(failure_threshold, recovery_seconds) for key in DOMAIN_KEYS
        }

    def breaker(self, key: str) -> CircuitBreaker:
        with self._lock:
            if key not in self._breakers:
                # A newly added seam gets a breaker with the same policy rather than none.
                self._breakers[key] = CircuitBreaker()
            return self._breakers[key]

    def reset_all(self) -> None:
        for breaker in list(self._breakers.values()):
            breaker.reset()


class InMemoryAuditSink:
    """
    Bounded in-process audit ring buffer.

    ``maxlen`` bounds memory (Â§26): a long-running server must not grow an unbounded audit list.
    Records beyond the cap are dropped oldest-first and counted in :attr:`dropped`, so a truncated
    audit is never silently indistinguishable from a complete one (Â§19).
    """

    distribution = "single-process"

    def __init__(self, maxlen: int = 1000) -> None:
        self.maxlen = max(0, int(maxlen))
        self._lock = threading.Lock()
        self._records: List[ToolAuditRecord] = []
        self.dropped = 0

    def append(self, record: ToolAuditRecord) -> None:
        with self._lock:
            if self.maxlen == 0:
                self.dropped += 1
                return
            self._records.append(record)
            while len(self._records) > self.maxlen:
                self._records.pop(0)
                self.dropped += 1

    def records(self) -> List[ToolAuditRecord]:
        with self._lock:
            return list(self._records)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self.dropped = 0


class ToolBus:
    """
    Owns the tool-bus runtime: RBAC policy, breakers, audit sink and service resolution.

    Constructed explicitly, so a caller chooses the state backend instead of inheriting whatever a
    module happened to build at import. Nothing here is global; two buses in one process are fully
    independent, which is what makes the domain tests hermetic and what will let a worker own its
    own bus later.

    :meth:`from_config` reads settings **at construction**, which is the fix for the import-time
    freeze: a caller wanting a different config builds a different bus, and the default bus is
    built lazily on first use rather than at import.
    """

    def __init__(
        self,
        *,
        config: Dict[str, Any],
        breakers: Optional[CircuitBreakers] = None,
        audit_sink: Optional[Any] = None,
        service_resolver: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self._config = config if isinstance(config, dict) else {}
        tools_cfg = self._config.get("tools", {}) or {}
        breaker_cfg = tools_cfg.get("circuit_breaker", {}) or {}
        audit_cfg = tools_cfg.get("audit", {}) or {}
        self._breaker_cfg = {
            "failure_threshold": int(breaker_cfg.get("failure_threshold", 3)),
            "recovery_seconds": float(breaker_cfg.get("recovery_seconds", 30)),
        }
        self._audit_maxlen = int(audit_cfg.get("max_records", 1000))
        self._breakers = breakers or InMemoryCircuitBreakers(**self._breaker_cfg)
        self._audit = (
            audit_sink if audit_sink is not None else InMemoryAuditSink(self._audit_maxlen)
        )
        # Resolver maps a domain key to a live service object, or None when unavailable. Injected
        # rather than imported so tests and future transports can substitute a client.
        self._resolve = service_resolver or _default_service_resolver
        self._base_permissions: Dict[str, List[str]] = dict(tools_cfg.get("permissions", {}) or {})

    @classmethod
    def from_config(
        cls,
        config: Optional[Dict[str, Any]] = None,
        *,
        breakers: Optional[CircuitBreakers] = None,
        audit_sink: Optional[Any] = None,
        service_resolver: Optional[Callable[[str], Any]] = None,
    ) -> "ToolBus":
        """Build a bus from an explicit config mapping, or from the loaded config when omitted."""
        if config is None:
            from .config import load_config

            config = load_config()
        return cls(
            config=config,
            breakers=breakers,
            audit_sink=audit_sink,
            service_resolver=service_resolver,
        )


    # -- policy ---------------------------------------------------------------------------------

    def permissions(self, tool_name: str) -> List[str]:
        """
        Roles allowed to call ``tool_name``: the in-code default map, overlaid by config.

        Read per call from the bus's own config, so a config change applies to a newly built bus
        with no process restart — the defect the old import-time read caused.
        """
        configured = self._base_permissions.get(tool_name)
        if configured is not None:
            return [str(role) for role in configured]
        return list(_DEFAULT_TOOL_PERMISSIONS.get(tool_name, []))

    def check_permission(self, tool_name: str, caller_agent: str) -> bool:
        return caller_agent in self.permissions(tool_name)

    def integration_settings(self) -> Dict[str, Any]:
        """The ``tools.integration`` block (feature flags, limits)."""
        return dict((self._config.get("tools", {}) or {}).get("integration", {}) or {})

    # -- guarded calls --------------------------------------------------------------------------

    def guarded(self, key: str, call: Callable[[], Any]) -> "tuple[Any, str, str]":
        """
        Run a downstream call under its breaker.

        Returns ``(result, provider, data_status)`` and **never raises** â€” a downstream failure must
        not take the workflow down. The third element is the shared :class:`DataStatus` vocabulary
        (Â§19):

        * ``LIVE``        the domain answered;
        * ``UNAVAILABLE`` the domain is not installed/disabled â€” nothing was consulted;
        * ``FAILED``      the domain was called and raised;
        * ``DEGRADED``    the circuit is open, so the dependency is being shielded.

        Callers must propagate ``data_status`` into their result. That is the point: a missing or
        substituted answer can no longer masquerade as a real one.
        """
        breaker = self._breakers.breaker(key)
        if not breaker.allow_request():
            return None, f"circuit-open:{key}", DataStatus.DEGRADED.value
        try:
            result = call()
        except Exception:
            breaker.record_failure()
            return None, f"error:{key}", DataStatus.FAILED.value
        breaker.record_success()
        if result is None:
            return None, f"empty:{key}", DataStatus.UNAVAILABLE.value
        return result, key, DataStatus.LIVE.value

    def service(self, key: str) -> Any:
        """Resolve a downstream service, or ``None`` when it is unavailable."""
        try:
            return self._resolve(key)
        except Exception:
            # A broken integration must look unavailable rather than crashing the tool bus; the
            # breaker and the audit record carry the reason.
            return None

    # -- audit ----------------------------------------------------------------------------------

    def record(self, record: ToolAuditRecord) -> None:
        self._audit.append(record)

    def audit_trail(self) -> List[ToolAuditRecord]:
        return list(self._audit.records())

    def clear_audit_trail(self) -> None:
        self._audit.clear()

    def health(self) -> Dict[str, Any]:
        """
        Operational snapshot (Â§28). Reports the *distribution* of the state backend so a healthy
        single-process bus is never mistaken for a cluster-wide one.
        """
        snapshot: Dict[str, Any] = {
            "breakers": self._breakers.snapshot(),
            "breaker_distribution": self._breakers.distribution,
            "audit_distribution": getattr(self._audit, "distribution", "unknown"),
            "breaker_policy": dict(self._breaker_cfg),
        }
        dropped = getattr(self._audit, "dropped", None)
        if dropped is not None:
            snapshot["audit_dropped"] = dropped
            if dropped:
                # A truncated audit must not read as a complete one.
                snapshot["data_status"] = DataStatus.DEGRADED.value
        return snapshot


#: Fallback RBAC map, used only when ``tools.permissions`` omits a tool. Kept in code (rather than
#: config-only) so a stripped config cannot silently grant or deny everything.
_DEFAULT_TOOL_PERMISSIONS: Dict[str, List[str]] = {
    "query_knowledge_graph": ["SUPERVISOR", "MARKET_INTELLIGENCE", "OPPORTUNITY_RISK"],
    "query_osint_signals": ["SUPERVISOR", "MARKET_INTELLIGENCE"],
    "evaluate_rfp_fit": ["SUPERVISOR", "OPPORTUNITY_RISK"],
    "check_legal_compliance": ["SUPERVISOR", "LEGAL_REGULATORY"],
    # Person A seams (see INTEGRATION.md)
    "search_documents": ["SUPERVISOR", "MARKET_INTELLIGENCE", "OPPORTUNITY_RISK"],
    "query_company_context": ["SUPERVISOR", "MARKET_INTELLIGENCE", "OPPORTUNITY_RISK"],
}



#: Capability key -> (feature flag, module, attribute) for Person A's optional seams.
_SEAM_SPECS: Dict[str, "tuple[str, str, str]"] = {
    "org": ("enable_org", "organizational_intelligence.service",
            "OrganizationalIntelligenceService"),
    "documents": ("enable_documents", "document_intelligence.service",
                  "DocumentIntelligenceService"),
    "knowledge_graph": ("enable_knowledge_graph", "knowledge_graph.service",
                        "KnowledgeGraphService"),
}

#: Person B's own services, imported directly. Their flags default to enabled.
_OWNED_FLAGS: Dict[str, str] = {
    "market": "enable_market",
    "opportunity": "enable_opportunity",
    "legal": "enable_legal",
}

#: Old flag name -> new flag name, so a config written against the pre-split keys keeps working.
#: A silently disabled integration is worse than a deprecation: an operator flipping a flag and
#: seeing no effect would have no signal that the flag had been renamed.
_LEGACY_FLAG_ALIASES: Dict[str, str] = {
    "enable_domain1": "enable_org",
    "enable_domain4": "enable_documents",
    "enable_domain5": "enable_knowledge_graph",
    "enable_domain7": "enable_market",
    "enable_domain8": "enable_opportunity",
    "enable_domain9": "enable_legal",
}


def _flag(integration: Dict[str, Any], name: str, default: bool) -> bool:
    """
    Read a feature flag, honouring its legacy name.

    The new name wins when both are present, so migrating is a no-op once the config is updated.
    """
    if name in integration:
        return bool(integration[name])
    for old, new in _LEGACY_FLAG_ALIASES.items():
        if new == name and old in integration:
            return bool(integration[old])
    return default


def _default_service_resolver(key: str) -> Any:
    """
    Resolve a downstream service by capability key.

    Imports happen inside the function so that importing :mod:`bus` does not import every domain:
    that cost, and its failure modes, belong to first use. A missing package or a renamed class
    yields ``None`` — the seams are optional by design, so Person A's domains can land at any time
    without breaking Domain 6.
    """
    import importlib

    from .config import load_config

    key = canonical_key(key)
    integration = (load_config().get("tools", {}) or {}).get("integration", {}) or {}

    spec = _SEAM_SPECS.get(key)
    if spec is not None:
        flag, module_name, attribute = spec
        # Person A's seams are off unless explicitly enabled: an absent package must not turn into
        # an import attempt on the hot path.
        if not _flag(integration, flag, False):
            return None
        try:
            return getattr(importlib.import_module(module_name), attribute)()
        except (ImportError, AttributeError):
            return None

    if key in _OWNED_FLAGS:
        if not _flag(integration, _OWNED_FLAGS[key], True):
            return None
        if key == "market":
            from business_market_intelligence.service import MarketIntelligenceService

            return MarketIntelligenceService()
        if key == "opportunity":
            from opportunity_risk_intelligence.service import OpportunityRiskService

            return OpportunityRiskService()
        if key == "legal":
            from legal_regulatory_ip_intelligence.service import LegalIntelligenceService

            return LegalIntelligenceService()
    return None


