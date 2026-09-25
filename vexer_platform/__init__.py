"""
Vexer Platform Kernel — shared foundations for the intelligence subsystem.

This package contains the *integration contracts* that every layer (orchestration, domain
intelligence, ingestion, storage, retrieval) must speak:

- ``contracts``  : versioned domain objects (Entity, Event, Relationship, CausalLink, Evidence,
                   IntelligenceSignal, Claim, ProvenanceRecord).
- ``ids``        : monotonic ULIDs, deterministic stable IDs, content hashing, idempotency keys.
- ``confidence`` : documented, explainable confidence methodology (``conf-v1``). Confidence != truth.
- ``temporal``   : validity windows, bitemporal records, "what was true at T" semantics.
- ``context``    : request/correlation/trace propagation + service-to-service envelopes.
- ``errors``     : structured error catalogue with retryability and secret redaction.
- ``config``     : profile-aware configuration with fail-fast validation.

Design constraints (see ``journey/ENGINEERING_JOURNEY.md``):
* Deterministic, dependency-light (stdlib + pydantic only) so every layer can import it.
* JSON-safe serialisation: values produced here must survive LangGraph checkpoints, Kafka
  payloads and PostgreSQL JSONB without custom encoders.
* No global mutable state, no hidden I/O, no network calls.
"""
from .config import KernelConfig, PlatformProfile, load_kernel_config
from .confidence import CONFIDENCE_METHODOLOGY_VERSION, ConfidenceScore, score_confidence
from .contracts import (
    SCHEMA_VERSION,
    Claim,
    CausalLink,
    Entity,
    Event,
    Evidence,
    IntelligenceSignal,
    ProvenanceClass,
    ProvenanceRecord,
    Relationship,
    RelationshipType,
    Severity,
    SignalType,
    EvidenceType,
)
from .errors import ErrorCode, VexerError
from .ids import content_hash, idempotency_key, new_ulid, stable_id
from .temporal import BitemporalRecord, ValidityWindow, select_valid_as_of

__all__ = [
    "KernelConfig",
    "PlatformProfile",
    "load_kernel_config",
    "CONFIDENCE_METHODOLOGY_VERSION",
    "ConfidenceScore",
    "score_confidence",
    "SCHEMA_VERSION",
    "Claim",
    "CausalLink",
    "Entity",
    "Event",
    "Evidence",
    "IntelligenceSignal",
    "ProvenanceClass",
    "ProvenanceRecord",
    "Relationship",
    "RelationshipType",
    "Severity",
    "SignalType",
    "EvidenceType",
    "ErrorCode",
    "VexerError",
    "content_hash",
    "idempotency_key",
    "new_ulid",
    "stable_id",
    "BitemporalRecord",
    "ValidityWindow",
    "select_valid_as_of",
]
