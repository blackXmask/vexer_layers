"""
Temporal intelligence primitives (mandate §15, §17, §42).

Facts and relationships change. The platform stores **intervals**, not booleans:

* ``valid_from``  — when the fact became true in the world (inclusive)
* ``valid_until`` — when it stopped being true (exclusive); ``None`` means "still true as far as we
  know", which is different from "true forever"
* ``recorded_at`` — when the platform learned it (the bitemporal axis)

This module answers the question §15 demands — *"what was true at time T?"* — without mutating
history, and provides half-open interval algebra so windows can be intersected (e.g. a relationship
that was valid while a regulation was in force).
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Generic, Iterable, Iterator, List, Optional, Sequence, TypeVar

from .errors import ErrorCode, VexerError

__all__ = [
    "BitemporalRecord",
    "ValidityWindow",
    "iter_history",
    "select_valid_as_of",
    "window_end_or_none",
]

T = TypeVar("T")


def _aware(value: Optional[datetime], field: str) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise VexerError(ErrorCode.VALIDATION_FAILED, f"{field} must be a datetime")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise VexerError(
            ErrorCode.VALIDATION_FAILED,
            f"{field} must be timezone-aware: naïve datetimes corrupt ordering across regions",
        )
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ValidityWindow:
    """Half-open interval ``[valid_from, valid_until)``; ``valid_until=None`` = open-ended."""

    valid_from: datetime
    valid_until: Optional[datetime] = None

    def __post_init__(self) -> None:
        start = _aware(self.valid_from, "valid_from")
        end = _aware(self.valid_until, "valid_until")
        if end is not None and end <= start:
            raise VexerError(
                ErrorCode.VALIDATION_FAILED,
                "valid_until must be strictly after valid_from "
                "(empty or inverted windows are rejected)",
            )
        object.__setattr__(self, "valid_from", start)
        object.__setattr__(self, "valid_until", end)

    # -- predicates -----------------------------------------------------------------------------
    def contains(self, moment: datetime) -> bool:
        """True when ``moment`` falls inside the window (start inclusive, end exclusive)."""
        at = _aware(moment, "moment")
        if at < self.valid_from:
            return False
        return self.valid_until is None or at < self.valid_until

    def is_open(self) -> bool:
        """True when the window has no end (still in force as far as we know)."""
        return self.valid_until is None

    def expired_at(self, moment: datetime) -> bool:
        at = _aware(moment, "moment")
        return self.valid_until is not None and at >= self.valid_until

    def overlaps(self, other: "ValidityWindow") -> bool:
        """Half-open overlap test (touching endpoints do not overlap)."""
        if self.valid_until is not None and other.valid_from >= self.valid_until:
            return False
        if other.valid_until is not None and self.valid_from >= other.valid_until:
            return False
        return True

    def intersect(self, other: "ValidityWindow") -> Optional["ValidityWindow"]:
        """Intersection of two windows, or ``None`` when they do not overlap."""
        if not self.overlaps(other):
            return None
        start = max(self.valid_from, other.valid_from)
        ends = [w for w in (self.valid_until, other.valid_until) if w is not None]
        end = min(ends) if ends else None
        if end is not None and end <= start:
            return None
        return ValidityWindow(start, end)

    def with_duration(self, days: float = 0, hours: float = 0) -> "ValidityWindow":
        """Derive a window of fixed length starting at ``valid_from`` (useful for TTLs)."""
        if days < 0 or hours < 0:
            raise VexerError(ErrorCode.VALIDATION_FAILED, "duration must not be negative")
        return ValidityWindow(self.valid_from, self.valid_from + timedelta(days=days, hours=hours))

    def describe(self) -> str:
        end = self.valid_until.isoformat() if self.valid_until else "open"
        return f"[{self.valid_from.isoformat()} -> {end})"


@dataclass(frozen=True)
class BitemporalRecord(Generic[T]):
    """A value with *both* axes: when it was true, and when we recorded it."""

    value: T
    valid: ValidityWindow
    recorded_at: datetime
    record_id: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "recorded_at", _aware(self.recorded_at, "recorded_at"))

    def as_known_at(self, moment: datetime) -> Optional[T]:
        """Return the value only if it was both *true* and *known* at ``moment`` (bitemporal)."""
        at = _aware(moment, "moment")
        if self.recorded_at > at:
            return None                     # not yet learned at that time
        return self.value if self.valid.contains(at) else None


def window_end_or_none(record: object) -> Optional[datetime]:
    """Extract ``valid_until`` from any contract object exposing it (duck-typed helper)."""
    value = getattr(record, "valid_until", None)
    return _aware(value, "valid_until") if value is not None else None


def select_valid_as_of(records: Iterable[T], moment: datetime) -> List[T]:
    """Return the records valid at ``moment`` — the "what was true at T" query (§15).

    Accepts contract objects, :class:`BitemporalRecord` instances, or any object exposing
    ``valid_from``/``valid_until``. Objects missing temporal fields raise a structured error rather
    than being silently included or excluded, because either silence would be a correctness bug.
    """
    at = _aware(moment, "moment")
    selected: List[T] = []
    for record in records:
        if isinstance(record, BitemporalRecord):
            window = record.valid
        else:
            start = getattr(record, "valid_from", None)
            if start is None:
                raise VexerError(
                    ErrorCode.VALIDATION_FAILED,
                    f"{type(record).__name__} has no valid_from: temporal queries require "
                    "explicit validity",
                )
            window = ValidityWindow(start, window_end_or_none(record))
        if window.contains(at):
            selected.append(record)
    return selected


def iter_history(records: Sequence[T]) -> Iterator[T]:
    """Yield records in recorded order when available, else in the given order (stable)."""

    def key(item: T) -> datetime:
        stamp = getattr(item, "recorded_at", None) or getattr(item, "created_at", None)
        resolved = _aware(stamp, "recorded_at")
        if resolved is None:
            raise VexerError(
                ErrorCode.VALIDATION_FAILED,
                f"{type(item).__name__} has neither recorded_at nor created_at for history ordering",
            )
        return resolved

    return iter(sorted(records, key=key))

