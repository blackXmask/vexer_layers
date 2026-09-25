"""
Profile-aware kernel configuration (mandate §32: no hardcoded operational configuration,
no secrets in source control).

* Profiles: ``DEV`` | ``TEST`` | ``STAGING`` | ``PROD``. The profile is explicit
  (``VEXER_PROFILE``); an unknown value **fails fast** instead of silently defaulting to DEV.
* Secrets come from the environment only. :func:`require_env` proves presence without logging the
  value, so nothing in this module can leak a secret into logs or a health payload.
* ``PROD`` refuses ``DEBUG`` logging so verbose internals cannot leak in production.
* Numeric ranges are validated (weights within [0, 1], half-life > 0, timeouts > 0): a typo in an
  environment variable becomes a startup error instead of a silent behavioural change.

``describe()`` returns a redacted snapshot suitable for ``/health`` and support bundles.
"""
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional

from .errors import ErrorCode, VexerError

__all__ = ["KernelConfig", "PlatformProfile", "load_kernel_config", "require_env"]

ENV_PROFILE = "VEXER_PROFILE"
ENV_SERVICE = "VEXER_SERVICE_NAME"
ENV_LOG_LEVEL = "VEXER_LOG_LEVEL"
ENV_TIMEOUT = "VEXER_DEFAULT_TIMEOUT_MS"

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

#: ``conf-v1`` default weights — see ``vexer_platform.confidence`` for the documented formula.
DEFAULT_CONFIDENCE_WEIGHTS: Dict[str, float] = {
    "evidence_quality": 0.45,
    "source_agreement": 0.25,
    "freshness": 0.20,
    "model_confidence": 0.10,
}
DEFAULT_CONTRADICTION_PENALTY = 0.15
DEFAULT_FRESHNESS_HALF_LIFE_DAYS = 90.0


class PlatformProfile(str, Enum):
    DEV = "DEV"
    TEST = "TEST"
    STAGING = "STAGING"
    PROD = "PROD"


def _env_bool(raw: Optional[str], default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(raw: Optional[str], name: str) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise VexerError(
            ErrorCode.VALIDATION_FAILED,
            f"environment variable {name} must be a number, got {raw!r}",
            details={"variable": name},
            cause=exc,
        ) from exc


@dataclass(frozen=True)
class KernelConfig:
    """Validated, immutable kernel configuration. Build it with :func:`load_kernel_config`."""

    profile: PlatformProfile = PlatformProfile.DEV
    service_name: str = "vexer"
    log_level: str = "INFO"
    default_timeout_ms: int = 30_000
    otel_enabled: bool = False
    strict: bool = False
    confidence_weights: Dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_CONFIDENCE_WEIGHTS)
    )
    contradiction_penalty: float = DEFAULT_CONTRADICTION_PENALTY
    freshness_half_life_days: float = DEFAULT_FRESHNESS_HALF_LIFE_DAYS

    def __post_init__(self) -> None:
        if not isinstance(self.profile, PlatformProfile):
            try:
                object.__setattr__(self, "profile", PlatformProfile(str(self.profile).upper()))
            except ValueError as exc:
                raise VexerError(
                    ErrorCode.VALIDATION_FAILED,
                    f"unknown profile {self.profile!r}; expected one of "
                    f"{[p.value for p in PlatformProfile]}",
                    cause=exc,
                ) from exc
        if not self.service_name or not self.service_name.strip():
            raise VexerError(ErrorCode.VALIDATION_FAILED, "service_name must be non-empty")
        level = str(self.log_level).upper()
        if level not in _VALID_LOG_LEVELS:
            raise VexerError(
                ErrorCode.VALIDATION_FAILED,
                f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}, got {self.log_level!r}",
            )
        object.__setattr__(self, "log_level", level)
        if self.profile is PlatformProfile.PROD and level == "DEBUG":
            raise VexerError(
                ErrorCode.VALIDATION_FAILED,
                "DEBUG logging is refused in PROD: verbose internals must not leak in production",
            )
        if self.default_timeout_ms <= 0:
            raise VexerError(ErrorCode.VALIDATION_FAILED, "default_timeout_ms must be positive")
        if self.freshness_half_life_days <= 0:
            raise VexerError(
                ErrorCode.VALIDATION_FAILED, "freshness_half_life_days must be positive"
            )
        if not 0.0 <= self.contradiction_penalty <= 1.0:
            raise VexerError(
                ErrorCode.VALIDATION_FAILED, "contradiction_penalty must be within [0, 1]"
            )
        weights = dict(self.confidence_weights or {})
        if not weights:
            raise VexerError(ErrorCode.VALIDATION_FAILED, "confidence_weights must not be empty")
        for key, value in weights.items():
            if not 0.0 <= float(value) <= 1.0:
                raise VexerError(
                    ErrorCode.VALIDATION_FAILED,
                    f"confidence weight {key!r} must be within [0, 1], got {value!r}",
                )
        if sum(float(v) for v in weights.values()) <= 0:
            raise VexerError(
                ErrorCode.VALIDATION_FAILED, "confidence_weights must sum to a positive value"
            )
        object.__setattr__(self, "confidence_weights", {k: float(v) for k, v in weights.items()})

    @property
    def is_production(self) -> bool:
        return self.profile is PlatformProfile.PROD

    def describe(self) -> Dict[str, Any]:
        """Redacted configuration snapshot for health endpoints and support bundles."""
        return {
            "profile": self.profile.value,
            "service_name": self.service_name,
            "log_level": self.log_level,
            "default_timeout_ms": self.default_timeout_ms,
            "otel_enabled": self.otel_enabled,
            "strict": self.strict,
            "confidence_weights": dict(self.confidence_weights),
            "contradiction_penalty": self.contradiction_penalty,
            "freshness_half_life_days": self.freshness_half_life_days,
        }


def require_env(name: str) -> str:
    """Return a required environment value without ever logging it."""
    value = os.getenv(name)
    if value is None or value == "":
        raise VexerError(
            ErrorCode.VALIDATION_FAILED,
            f"required environment variable {name} is not set",
            details={"variable": name},
        )
    return value


def load_kernel_config(env: Optional[Mapping[str, str]] = None) -> KernelConfig:
    """Build a validated :class:`KernelConfig` from the environment (or an injected mapping)."""
    source: Mapping[str, str] = env if env is not None else os.environ
    raw_profile = source.get(ENV_PROFILE, PlatformProfile.DEV.value)
    try:
        profile = PlatformProfile(str(raw_profile).strip().upper())
    except ValueError as exc:
        raise VexerError(
            ErrorCode.VALIDATION_FAILED,
            f"{ENV_PROFILE} must be one of {[p.value for p in PlatformProfile]} "
            f"(got {raw_profile!r}) — refusing to guess a profile",
            details={"variable": ENV_PROFILE},
            cause=exc,
        ) from exc
    timeout = _env_float(source.get(ENV_TIMEOUT), ENV_TIMEOUT)
    return KernelConfig(
        profile=profile,
        service_name=source.get(ENV_SERVICE, "vexer"),
        log_level=source.get(ENV_LOG_LEVEL, "INFO"),
        default_timeout_ms=int(timeout) if timeout is not None else 30_000,
        otel_enabled=_env_bool(source.get("VEXER_OTEL_ENABLED"), False),
        strict=_env_bool(source.get("VEXER_KERNEL_STRICT"), profile is PlatformProfile.PROD),
    )

