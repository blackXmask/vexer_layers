"""
Request/correlation/trace context propagation (mandate §20).

A single ambient :class:`RequestContext` travels with the work — through FastAPI request handlers,
agent nodes, tool calls, background consumers and async tasks — using :mod:`contextvars`, which is
both coroutine-safe and thread-safe by design (each asyncio task and each thread gets its own copy).

Design rules:
* No hidden globals: contexts are immutable values bound explicitly via :func:`bind_context` /
  :func:`context_scope`.
* Correlation is preserved across hops: :func:`child_context` keeps ``correlation_id`` and
  ``trace_id``, issues a fresh ``request_id``, and swaps ``source``/``target`` services.
* W3C Trace Context interop: :func:`traceparent` / :func:`parse_traceparent` let us join an
  external OpenTelemetry trace instead of inventing a proprietary header format.
"""
import re
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Optional, Tuple

from .errors import ErrorCode, VexerError
from .ids import new_ulid

__all__ = [
    "HEADER_CORRELATION_ID",
    "HEADER_REQUEST_ID",
    "HEADER_TRACEPARENT",
    "RequestContext",
    "bind_context",
    "child_context",
    "context_scope",
    "current_context",
    "new_request_context",
    "parse_traceparent",
    "reset_context",
    "require_context",
    "traceparent",
]

HEADER_REQUEST_ID = "X-Request-Id"
HEADER_CORRELATION_ID = "X-Correlation-Id"
HEADER_TRACEPARENT = "traceparent"

_TRACEPARENT_RE = re.compile(r"^00-(?P<trace>[0-9a-f]{32})-(?P<span>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$")
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")

_context: ContextVar[Optional["RequestContext"]] = ContextVar("vexer_request_context", default=None)


@dataclass(frozen=True)
class RequestContext:
    """Immutable identity of one unit of work, propagated in-process and across services."""

    request_id: str
    correlation_id: str
    trace_id: str
    service_name: str
    tenant_id: Optional[str] = None
    target_service: Optional[str] = None
    schema_version: Optional[str] = None
    timeout_ms: Optional[int] = None
    created_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if not self.request_id or not self.correlation_id or not self.trace_id:
            raise VexerError(ErrorCode.VALIDATION_FAILED, "request/correlation/trace IDs are required")
        if not _TRACE_ID_RE.match(self.trace_id):
            raise VexerError(
                ErrorCode.VALIDATION_FAILED,
                "trace_id must be 32 lowercase hex characters (W3C trace context)",
            )
        if self.timeout_ms is not None and self.timeout_ms <= 0:
            raise VexerError(ErrorCode.VALIDATION_FAILED, "timeout_ms must be positive")
        if self.created_at is None:
            object.__setattr__(self, "created_at", datetime.now(timezone.utc))
        elif self.created_at.tzinfo is None:
            raise VexerError(ErrorCode.VALIDATION_FAILED, "created_at must be timezone-aware (UTC)")

    # -- derivations ---------------------------------------------------------------------------
    def child(self, *, service_name: Optional[str] = None, **overrides: Any) -> "RequestContext":
        """Derive a context for a downstream hop: new ``request_id``, same correlation/trace."""
        data: Dict[str, Any] = {
            "request_id": new_ulid(),
            "correlation_id": self.correlation_id,
            "trace_id": self.trace_id,
            "service_name": service_name or self.service_name,
            "tenant_id": self.tenant_id,
            "target_service": None,
            "schema_version": self.schema_version,
            "timeout_ms": self.timeout_ms,
            "created_at": None,
        }
        data.update(overrides)
        return RequestContext(**data)

    def as_headers(self) -> Dict[str, str]:
        """Outgoing headers for HTTP/gRPC hops (safe to propagate to untrusted peers)."""
        headers = {
            HEADER_REQUEST_ID: self.request_id,
            HEADER_CORRELATION_ID: self.correlation_id,
            HEADER_TRACEPARENT: self.traceparent(),
        }
        if self.tenant_id:
            headers["X-Tenant-Id"] = self.tenant_id
        return headers

    def traceparent(self) -> str:
        """W3C traceparent header for this context (16-hex span id, sampled flag set)."""
        import secrets

        return f"00-{self.trace_id}-{secrets.token_hex(8)}-01"


def new_request_context(
    service_name: str,
    *,
    tenant_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    schema_version: Optional[str] = None,
    timeout_ms: Optional[int] = None,
) -> RequestContext:
    """Create a root context for a new unit of work (request, CLI run, consumer batch)."""
    import secrets

    return RequestContext(
        request_id=new_ulid(),
        correlation_id=correlation_id or new_ulid(),
        trace_id=trace_id or secrets.token_hex(16),
        service_name=service_name,
        tenant_id=tenant_id,
        schema_version=schema_version,
        timeout_ms=timeout_ms,
    )


def current_context() -> Optional[RequestContext]:
    """Ambient context, or ``None`` when nothing is bound (e.g. module import time)."""
    return _context.get()


def require_context() -> RequestContext:
    """Ambient context or a structured error — use in code paths that must be traced."""
    ctx = _context.get()
    if ctx is None:
        raise VexerError(
            ErrorCode.VALIDATION_FAILED,
            "no request context is bound; call new_request_context()/context_scope() first",
        )
    return ctx


def bind_context(ctx: RequestContext) -> Token:
    """Bind ``ctx`` as ambient for the current task/thread; returns a reset token."""
    if not isinstance(ctx, RequestContext):
        raise VexerError(ErrorCode.VALIDATION_FAILED, "bind_context requires a RequestContext")
    return _context.set(ctx)


def reset_context(token: Token) -> None:
    """Restore the previous ambient context (always call in a ``finally`` block)."""
    _context.reset(token)


@contextmanager
def context_scope(ctx: RequestContext) -> Iterator[RequestContext]:
    """Scope an ambient context to a block, restoring the previous one on exit."""
    token = bind_context(ctx)
    try:
        yield ctx
    finally:
        reset_context(token)


def child_context(**overrides: Any) -> RequestContext:
    """Derive a downstream context from the ambient one (new request_id, same correlation)."""
    return require_context().child(**overrides)


def parse_traceparent(value: str) -> Tuple[str, str, bool]:
    """Parse a W3C ``traceparent`` header into ``(trace_id, span_id, sampled)``.

    Malformed input raises a structured ``VALIDATION_FAILED`` rather than being silently ignored:
    untrusted inbound headers must never be trusted implicitly.
    """
    if not isinstance(value, str):
        raise VexerError(ErrorCode.VALIDATION_FAILED, "traceparent must be a string")
    match = _TRACEPARENT_RE.match(value.strip().lower())
    if not match:
        raise VexerError(ErrorCode.VALIDATION_FAILED, f"malformed traceparent: {value!r}")
    trace_id = match.group("trace")
    span_id = match.group("span")
    if trace_id == "0" * 32 or span_id == "0" * 16:
        raise VexerError(ErrorCode.VALIDATION_FAILED, "traceparent carries all-zero IDs")
    return trace_id, span_id, bool(int(match.group("flags"), 16) & 0x01)


def traceparent(ctx: Optional[RequestContext] = None) -> str:
    """Convenience wrapper: traceparent for ``ctx`` or for the ambient context."""
    return (ctx or require_context()).traceparent()

