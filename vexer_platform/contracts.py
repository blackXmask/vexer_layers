"""
Shared, versioned domain contracts (mandate §11–§15, §17, §21, §39).

**Versioning policy.** ``SCHEMA_VERSION`` follows ``MAJOR.MINOR.PATCH`` semantics for payloads that
cross a boundary (Kafka, HTTP, JSONB columns, checkpoints):

* additive, optional field → **MINOR** bump; older consumers keep working (all new fields optional),
* removal, rename, type change or semantic change → **MAJOR** bump; ``assert_schema_supported``
  rejects unsupported majors instead of misinterpreting data.

**Design rules.**
* Every object carries a stable identifier plus ``schema_version`` and explicit temporal fields —
  never "latest wins".
* Datetimes must be **timezone-aware**; naïve timestamps are rejected at construction time because
  they are the root cause of ordering bugs in distributed ingestion.
* Confidence is bounded to ``[0, 1]`` and is *not* truth: truthfulness is expressed separately by
  :class:`TruthStatus` and :class:`ProvenanceClass` (observed → derived → hypothesis → prediction →
  unknown). This is how the platform refuses to turn an unknown into a fact.
* Serialisation is JSON-safe by construction (``model_dump(mode="json")``) so the same payload can go
  to Kafka, PostgreSQL JSONB, Qdrant payloads and LangGraph checkpoints without custom encoders.
"""
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Dict, List, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from .errors import ErrorCode, VexerError
from .ids import new_ulid

__all__ = [
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_MAJORS",
    "AwareUTC",
    "Claim",
    "CausalLink",
    "Confidence",
    "Entity",
    "Event",
    "Evidence",
    "EvidenceType",
    "IntelligenceSignal",
    "ProvenanceClass",
    "ProvenanceRecord",
    "Relationship",
    "RelationshipType",
    "Severity",
    "SignalType",
    "TruthStatus",
    "assert_schema_supported",
]

SCHEMA_VERSION = "1.0.0"
SUPPORTED_SCHEMA_MAJORS = {1}


def _require_aware(value: datetime) -> datetime:
    """Reject naïve timestamps; normalise everything to UTC."""
    if not isinstance(value, datetime):
        raise ValueError("expected a datetime")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("datetime must be timezone-aware (UTC) — naïve timestamps are rejected")
    return value.astimezone(timezone.utc)


AwareUTC = Annotated[datetime, AfterValidator(_require_aware)]
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
NonEmptyStr = Annotated[str, Field(min_length=1)]


class ProvenanceClass(str, Enum):
    """§39 hallucination control: what kind of statement is this?"""

    OBSERVED = "OBSERVED"        # directly present in evidence
    DERIVED = "DERIVED"          # computed deterministically from observed data
    HYPOTHESIS = "HYPOTHESIS"    # inferred, not yet corroborated
    PREDICTION = "PREDICTION"    # forward-looking estimate
    UNKNOWN = "UNKNOWN"          # insufficient information — must never be presented as fact


class TruthStatus(str, Enum):
    """§13: confidence is not truth. Status is decided by evidence agreement, not by score."""

    SUPPORTED = "SUPPORTED"
    CONTESTED = "CONTESTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"


class EvidenceType(str, Enum):
    DOCUMENT = "DOCUMENT"
    NEWS = "NEWS"
    TENDER = "TENDER"
    PATENT = "PATENT"
    REGISTRY = "REGISTRY"
    SOCIAL = "SOCIAL"
    ANALYST = "ANALYST"
    API = "API"
    TELEMETRY = "TELEMETRY"
    HUMAN = "HUMAN"


class RelationshipType(str, Enum):
    """Typed edges (§16). Adding a member is a MINOR change; renaming is MAJOR."""

    CAUSES = "CAUSES"
    AFFECTS = "AFFECTS"
    FOLLOWS = "FOLLOWS"
    PRECEDES = "PRECEDES"
    CONTRADICTS = "CONTRADICTS"
    CORRELATES_WITH = "CORRELATES_WITH"
    INCREASES_RISK = "INCREASES_RISK"
    DECREASES_RISK = "DECREASES_RISK"
    CREATES_OPPORTUNITY = "CREATES_OPPORTUNITY"
    MODIFIES = "MODIFIES"
    DEPENDS_ON = "DEPENDS_ON"


class SignalType(str, Enum):
    OPPORTUNITY = "OPPORTUNITY"
    RISK = "RISK"
    REGULATORY = "REGULATORY"
    MARKET = "MARKET"
    THREAT = "THREAT"
    ANOMALY = "ANOMALY"
    TREND = "TREND"
    CONTRADICTION = "CONTRADICTION"


class Severity(str, Enum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Record(BaseModel):
    """Common base: strict validation, JSON-safe dumps, no unknown fields."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    schema_version: NonEmptyStr = SCHEMA_VERSION

    def to_json_dict(self) -> Dict[str, Any]:
        """JSON-safe representation (enums → strings, datetimes → ISO-8601 UTC)."""
        return self.model_dump(mode="json")


def assert_schema_supported(schema_version: str, *, as_of: str = "ingest") -> None:
    """Reject payloads whose MAJOR version we cannot interpret (fail loud, never guess)."""
    if not isinstance(schema_version, str) or not schema_version.strip():
        raise VexerError(ErrorCode.VALIDATION_FAILED, "payload is missing schema_version")
    try:
        major = int(schema_version.split(".", 1)[0])
    except (ValueError, IndexError) as exc:
        raise VexerError(
            ErrorCode.VALIDATION_FAILED,
            f"malformed schema_version {schema_version!r}",
            details={"as_of": as_of},
            cause=exc,
        ) from exc
    if major not in SUPPORTED_SCHEMA_MAJORS:
        raise VexerError(
            ErrorCode.SCHEMA_VERSION_UNSUPPORTED,
            f"schema major {major} is not supported (supported: {sorted(SUPPORTED_SCHEMA_MAJORS)})",
            details={"schema_version": schema_version, "as_of": as_of},
        )


class ProvenanceRecord(Record):
    """Lineage node (§12): attributions for one transformation step.

    Chain: ``SOURCE → DOCUMENT → EVENT → ENTITY → RELATIONSHIP → ANALYSIS → CONCLUSION``.
    ``derived_from`` holds the upstream identifiers, so the full lineage is a DAG that can be walked
    in either direction without extra tables.
    """

    provenance_id: NonEmptyStr = Field(default_factory=new_ulid)
    stage: NonEmptyStr                      # SOURCE | DOCUMENT | EVENT | ENTITY | RELATIONSHIP | ANALYSIS | CONCLUSION
    source_ids: List[str] = Field(default_factory=list)
    evidence_ids: List[str] = Field(default_factory=list)
    derived_from: List[str] = Field(default_factory=list)
    pipeline: NonEmptyStr = "vexer"
    pipeline_version: NonEmptyStr = "0.0.0"
    model: Optional[str] = None             # model identifier when an LLM contributed
    model_version: Optional[str] = None
    transformation: NonEmptyStr = "passthrough"
    recorded_by: NonEmptyStr = "system"
    recorded_at: AwareUTC = Field(default_factory=lambda: datetime.now(timezone.utc))
    provenance_class: ProvenanceClass = ProvenanceClass.OBSERVED


class Entity(Record):
    """A resolved real-world thing (company, person, jurisdiction, technology, product)."""

    entity_id: NonEmptyStr = Field(default_factory=new_ulid)
    entity_type: NonEmptyStr
    name: NonEmptyStr
    aliases: List[str] = Field(default_factory=list)
    attributes: Dict[str, Any] = Field(default_factory=dict)
    source_ids: List[str] = Field(default_factory=list)
    confidence: Confidence = 0.0
    created_at: AwareUTC = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: AwareUTC = Field(default_factory=lambda: datetime.now(timezone.utc))
    valid_from: AwareUTC = Field(default_factory=lambda: datetime.now(timezone.utc))
    valid_until: Optional[AwareUTC] = None

    @model_validator(mode="after")
    def _check_window(self) -> "Entity":
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be after valid_from")
        return self


class Evidence(Record):
    """A retrievable artefact that supports or contradicts a claim (§12, §18, §42).

    ``content_hash`` gives integrity and dedup even when the URI changes (link rot, mirrors).
    """

    evidence_id: NonEmptyStr = Field(default_factory=new_ulid)
    source_id: NonEmptyStr
    source_type: EvidenceType
    uri: Optional[str] = None
    content_hash: Optional[str] = None      # "sha256:<hex>" — see ids.content_hash
    evidence_type: EvidenceType = EvidenceType.DOCUMENT
    reliability: Confidence = 0.5           # source-reliability prior, owned by the source registry
    published_at: Optional[AwareUTC] = None
    retrieved_at: AwareUTC = Field(default_factory=lambda: datetime.now(timezone.utc))
    supports: List[str] = Field(default_factory=list)     # claim/record ids
    contradicts: List[str] = Field(default_factory=list)  # claim/record ids
    confidence: Confidence = 0.0
    provenance_class: ProvenanceClass = ProvenanceClass.OBSERVED
    attributes: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_hash(self) -> "Evidence":
        if self.content_hash is not None and ":" not in self.content_hash:
            raise ValueError("content_hash must be '<algorithm>:<hex>' (use ids.content_hash)")
        overlap = set(self.supports) & set(self.contradicts)
        if overlap:
            raise ValueError(f"an evidence item cannot both support and contradict the same id: {sorted(overlap)}")
        return self


class Event(Record):
    """Something that happened at a point in time, linked to entities and evidence (§16)."""

    event_id: NonEmptyStr = Field(default_factory=new_ulid)
    event_type: NonEmptyStr
    timestamp: AwareUTC
    entities: List[str] = Field(default_factory=list)       # entity ids
    source_ids: List[str] = Field(default_factory=list)
    evidence_ids: List[str] = Field(default_factory=list)
    attributes: Dict[str, Any] = Field(default_factory=dict)
    confidence: Confidence = 0.0
    provenance_id: Optional[str] = None
    provenance_class: ProvenanceClass = ProvenanceClass.OBSERVED
    geo: Optional[Dict[str, float]] = None                  # {"lat": .., "lon": ..} when known
    idempotency_key: Optional[str] = None                   # see ids.idempotency_key


class Relationship(Record):
    """A typed, *temporal* edge between two entities (§15, §17).

    Relationships are never assumed permanent: validity is an interval in time, and ``recorded_at``
    adds the bitemporal axis ("when did the platform learn it?") so history is preserved rather than
    overwritten.
    """

    relationship_id: NonEmptyStr = Field(default_factory=new_ulid)
    source_entity: NonEmptyStr
    target_entity: NonEmptyStr
    relationship_type: RelationshipType
    confidence: Confidence = 0.0
    evidence_ids: List[str] = Field(default_factory=list)
    valid_from: AwareUTC = Field(default_factory=lambda: datetime.now(timezone.utc))
    valid_until: Optional[AwareUTC] = None
    created_at: AwareUTC = Field(default_factory=lambda: datetime.now(timezone.utc))
    recorded_at: AwareUTC = Field(default_factory=lambda: datetime.now(timezone.utc))
    provenance_id: Optional[str] = None
    provenance_class: ProvenanceClass = ProvenanceClass.OBSERVED
    attributes: Dict[str, Any] = Field(default_factory=dict)
    supersedes: Optional[str] = None        # previous relationship_id this one replaces

    @model_validator(mode="after")
    def _check_edge(self) -> "Relationship":
        if self.source_entity == self.target_entity:
            raise ValueError("self-referential relationships are not allowed")
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be after valid_from")
        return self


class CausalLink(Record):
    """A directional causal assertion between two events or entities (§16)."""

    causal_id: NonEmptyStr = Field(default_factory=new_ulid)
    cause: NonEmptyStr                      # event_id or entity_id
    effect: NonEmptyStr                     # event_id or entity_id
    relationship_type: RelationshipType = RelationshipType.CAUSES
    confidence: Confidence = 0.0
    evidence_ids: List[str] = Field(default_factory=list)
    timestamp: AwareUTC = Field(default_factory=lambda: datetime.now(timezone.utc))
    mechanism: Optional[str] = None          # short explanation of *why* causation is asserted
    provenance_id: Optional[str] = None
    provenance_class: ProvenanceClass = ProvenanceClass.HYPOTHESIS

    @model_validator(mode="after")
    def _check_causal(self) -> "CausalLink":
        if self.cause == self.effect:
            raise ValueError("a causal link needs distinct cause and effect")
        allowed = {
            RelationshipType.CAUSES,
            RelationshipType.AFFECTS,
            RelationshipType.PRECEDES,
            RelationshipType.FOLLOWS,
            RelationshipType.INCREASES_RISK,
            RelationshipType.DECREASES_RISK,
            RelationshipType.CREATES_OPPORTUNITY,
            RelationshipType.CORRELATES_WITH,
        }
        if self.relationship_type not in allowed:
            raise ValueError(
                f"{self.relationship_type.value} is not a valid causal relationship type"
            )
        return self


class IntelligenceSignal(Record):
    """The platform's decision-grade output object (§11, §13, §19)."""

    signal_id: NonEmptyStr = Field(default_factory=new_ulid)
    signal_type: SignalType
    headline: NonEmptyStr
    detail: Optional[str] = None
    entities: List[str] = Field(default_factory=list)
    events: List[str] = Field(default_factory=list)
    evidence: List[str] = Field(default_factory=list)
    claims: List[str] = Field(default_factory=list)
    confidence: Confidence = 0.0
    confidence_components: Dict[str, float] = Field(default_factory=dict)
    truth_status: TruthStatus = TruthStatus.UNKNOWN
    severity: Severity = Severity.INFO
    timestamp: AwareUTC = Field(default_factory=lambda: datetime.now(timezone.utc))
    provenance_id: Optional[str] = None
    provenance_class: ProvenanceClass = ProvenanceClass.DERIVED

    @model_validator(mode="after")
    def _check_evidence(self) -> "IntelligenceSignal":
        # Hallucination control (§39): a non-UNKNOWN truth status must cite something.
        if self.truth_status is not TruthStatus.UNKNOWN and not self.evidence and not self.claims:
            raise ValueError(
                "a signal with truth_status != UNKNOWN must cite evidence or claims "
                "(unknown must never be presented as supported)"
            )
        return self


class Claim(Record):
    """An assertion that can be supported or contradicted, and can contradict another claim.

    Contradictions are **preserved** (§14): both claims stay retrievable, linked by CONTRADICTS
    edges, and resolution is expressed as ``truth_status`` + ``confidence`` instead of deleting the
    losing side.
    """

    claim_id: NonEmptyStr = Field(default_factory=new_ulid)
    subject: NonEmptyStr
    predicate: NonEmptyStr
    object: NonEmptyStr
    confidence: Confidence = 0.0
    truth_status: TruthStatus = TruthStatus.UNKNOWN
    supported_by: List[str] = Field(default_factory=list)     # evidence ids
    contradicted_by: List[str] = Field(default_factory=list)  # evidence ids
    contradicts: List[str] = Field(default_factory=list)      # other claim ids
    valid_from: AwareUTC = Field(default_factory=lambda: datetime.now(timezone.utc))
    valid_until: Optional[AwareUTC] = None
    provenance_id: Optional[str] = None
    provenance_class: ProvenanceClass = ProvenanceClass.DERIVED
    attributes: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_claim(self) -> "Claim":
        if self.truth_status is TruthStatus.SUPPORTED and not self.supported_by:
            raise ValueError("SUPPORTED requires at least one supporting evidence id")
        if self.truth_status is TruthStatus.CONTESTED and not (
            self.supported_by and (self.contradicted_by or self.contradicts)
        ):
            raise ValueError(
                "CONTESTED requires both supporting evidence and a contradicting source"
            )
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be after valid_from")
        return self



