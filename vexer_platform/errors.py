"""
Structured errors for the Vexer platform kernel (mandate §20: no random strings as errors).

Every failure crossing a service boundary is expressed as :class:`VexerError` carrying:

* a stable machine-readable ``code`` (closed enum — consumers can switch on it),
* an explicit ``retryable`` decision (never guessed by the caller),
* an HTTP status for API surfaces,
* service attribution plus request/correlation/trace identifiers (auto-filled from
  :mod:`vexer_platform.context` when a context is bound),
* JSON-safe ``details`` with automatic **secret redaction** for logging.

:func:`from_exception` translates arbitrary third-party exceptions (asyncpg, aiokafka, httpx, …)
into this catalogue so infrastructure failures are never swallowed or leaked raw.
"""
import json
import re
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Optional

from .ids import new_ulid

__all__ = ["ErrorCode", "VexerError", "redact", "from_exception"]

_REDACT_KEYS = re.compile(
    r"(pass(word|phrase)?|secret|token|api[_-]?key|authorization|credential|dsn|private[_-]?key|"
    r"session|bearer|cookie|salt|signature)",
    re.IGNORECASE,
)
_REDACTED = "***REDACTED***"


class ErrorCode(str, Enum):
    """Closed error catalogue. Add codes deliberately; consumers switch on these values."""

    VALIDATION_FAILED = "VALIDATION_FAILED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    RATE_LIMITED = "RATE_LIMITED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    DEPENDENCY_TIMEOUT = "DEPENDENCY_TIMEOUT"
    BACKPRESSURE = "BACKPRESSURE"
    IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"
    SCHEMA_VERSION_UNSUPPORTED = "SCHEMA_VERSION_UNSUPPORTED"
    DATA_INTEGRITY = "DATA_INTEGRITY"
    INTERNAL = "INTERNAL"


_RETRYABLE: Dict[ErrorCode, bool] = {
    ErrorCode.RATE_LIMITED: True,
    ErrorCode.DEPENDENCY_UNAVAILABLE: True,
    ErrorCode.DEPENDENCY_TIMEOUT: True,
    ErrorCode.BACKPRESSURE: True,
    ErrorCode.CONFLICT: True,
}

_HTTP_STATUS: Dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_FAILED: 422,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.DEPENDENCY_UNAVAILABLE: 503,
    ErrorCode.DEPENDENCY_TIMEOUT: 504,
    ErrorCode.BACKPRESSURE: 503,
    ErrorCode.IDEMPOTENT_REPLAY: 200,
    ErrorCode.SCHEMA_VERSION_UNSUPPORTED: 400,
    ErrorCode.DATA_INTEGRITY: 500,
    ErrorCode.INTERNAL: 500,
}


def redact(value: Any, *, depth: int = 0, max_depth: int = 6) -> Any:
    """Return a copy of ``value`` with secret-looking keys masked, for safe logging.

    Bounded recursion: beyond ``max_depth`` the value becomes its type name, so a cyclic or
    pathologically nested payload from an untrusted source cannot exhaust the stack.
    """
    if depth >= max_depth:
        return f"<{type(value).__name__}>"
    if isinstance(value, Mapping):
        return {
            str(key): (_REDACTED if _REDACT_KEYS.search(str(key)) else redact(val, depth=depth + 1))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact(item, depth=depth + 1) for item in value]
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return f"<{type(value).__name__}>"


class VexerError(Exception):
    """Structured, JSON-serialisable platform error."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        service: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
        retryable: Optional[bool] = None,
        http_status: Optional[int] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.code = ErrorCode(code)
        self.message = str(message)
        self.service = service
        self.details: Dict[str, Any] = dict(details or {})
        self.retryable = _RETRYABLE.get(self.code, False) if retryable is None else bool(retryable)
        self.http_status = _HTTP_STATUS.get(self.code, 500) if http_status is None else int(http_status)
        self.cause_type = type(cause).__name__ if cause is not None else None
        self.error_id = new_ulid()
        self.request_id: Optional[str] = None
        self.correlation_id: Optional[str] = None
        self.trace_id: Optional[str] = None
        self._attach_context()

    def _attach_context(self) -> None:
        """Attach ambient request/correlation/trace IDs when a context is bound."""
        try:
            from .context import current_context

            ctx = current_context()
        except Exception:  # pragma: no cover - context lookup must never break error reporting
            ctx = None
        if ctx is not None:
            self.request_id = ctx.request_id
            self.correlation_id = ctx.correlation_id
            self.trace_id = ctx.trace_id
            if self.service is None:
                self.service = ctx.service_name

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe payload for API responses, logs and dead-letter records."""
        return {
            "error_id": self.error_id,
            "code": self.code.value,
            "message": self.message,
            "service": self.service,
            "retryable": self.retryable,
            "http_status": self.http_status,
            "request_id": self.request_id,
            "correlation_id": self.correlation_id,
            "trace_id": self.trace_id,
            "cause_type": self.cause_type,
            "details": redact(self.details),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return f"VexerError(code={self.code.value}, message={self.message!r}, service={self.service!r})"


_TIMEOUT_HINTS: Iterable[str] = ("timeout", "timed out", "deadline exceeded")


def from_exception(
    exc: BaseException, *, service: Optional[str] = None, operation: str = ""
) -> VexerError:
    """Translate a third-party exception into the platform catalogue.

    Classification is deliberately conservative: unknown failures become ``INTERNAL`` and are
    **not** marked retryable, so callers never retry blindly.
    """
    if isinstance(exc, VexerError):
        return exc
    name = type(exc).__name__
    text = str(exc).lower()
    if any(hint in text for hint in _TIMEOUT_HINTS) or "Timeout" in name:
        code = ErrorCode.DEPENDENCY_TIMEOUT
    elif "Connection" in name or "Unavailable" in name or "refused" in text or "unreachable" in text:
        code = ErrorCode.DEPENDENCY_UNAVAILABLE
    elif isinstance(exc, (ValueError, TypeError, KeyError)):
        code = ErrorCode.VALIDATION_FAILED
    else:
        code = ErrorCode.INTERNAL
    return VexerError(
        code,
        f"{operation or 'operation'} failed: {exc}",
        service=service,
        details={"exception_type": name},
        cause=exc,
    )

