"""
Identifier generation for the Vexer platform kernel.

Two ID families, chosen for different jobs:

* :func:`new_ulid` — **monotonic ULID**: 48-bit millisecond timestamp + 80 bits of randomness,
  Crockford base32 (26 chars). Lexicographically sortable by creation time, collision-resistant
  without coordination, dependency-free, and strictly monotonic within a millisecond per process
  (thread-safe). Used for *new* records: events, evidence, signals, audit entries.
* :func:`stable_id` — **deterministic UUIDv5** over a fixed platform namespace and a canonical
  natural key. Used where the same logical thing must always yield the same ID (entity resolution,
  ingestion dedup, idempotency). Changing the namespace or the canonicalisation invalidates all
  previously derived IDs, so both are versioned here.

Also provides content hashing (evidence integrity) and idempotency-key derivation.
"""
import hashlib
import re
import secrets
import threading
import time
import uuid
from typing import Optional

__all__ = [
    "ID_SCHEME_VERSION",
    "canonical_name",
    "content_hash",
    "idempotency_key",
    "is_valid_ulid",
    "new_ulid",
    "stable_id",
    "ulid_timestamp_ms",
]

ID_SCHEME_VERSION = "ids-v1"
_NAMESPACE = uuid.UUID("6f2d4c1e-9b3a-5f47-8c21-0d5e7a9b1c33")  # fixed platform namespace (ids-v1)
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_TIME_CHARS = 10          # 48 bits of milliseconds
_RAND_BITS = 80           # 16 base32 chars
_RAND_MOD = 1 << _RAND_BITS
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_WS_RE = re.compile(r"\s+")

_lock = threading.Lock()
_last_ms = 0
_last_rand = 0


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_ulid(now_ms: Optional[int] = None) -> str:
    """Return a new monotonic ULID.

    Monotonicity guarantee: within a single process, successive calls never return a smaller
    value, even inside the same millisecond or if the wall clock steps backwards. Randomness is
    80-bit, so cross-process collisions require both the same millisecond and a 1-in-2^80 draw.
    """
    global _last_ms, _last_rand
    ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    with _lock:
        if ms < _last_ms:            # clock regression: keep ordering monotonic
            ms = _last_ms
        if ms == _last_ms:
            _last_rand += 1
            if _last_rand >= _RAND_MOD:   # exhausted this millisecond: roll forward
                ms += 1
                _last_rand = secrets.randbits(_RAND_BITS)
        else:
            _last_rand = secrets.randbits(_RAND_BITS)
        _last_ms = ms
        rand = _last_rand
    return _encode(ms, _TIME_CHARS) + _encode(rand, 16)


def is_valid_ulid(value: str) -> bool:
    """Structural validation (charset + length); does not verify uniqueness."""
    return isinstance(value, str) and bool(_ULID_RE.match(value))


def ulid_timestamp_ms(value: str) -> int:
    """Decode the embedded millisecond timestamp. Raises ValueError on malformed input."""
    if not is_valid_ulid(value):
        raise ValueError(f"not a valid ULID: {value!r}")
    total = 0
    for char in value[:_TIME_CHARS]:
        total = (total << 5) | _CROCKFORD.index(char)
    return total


def canonical_name(name: str) -> str:
    """Canonicalise a natural-language key for deterministic IDs.

    Version ``ids-v1``: casefold, collapse internal whitespace, strip surrounding whitespace.
    Punctuation is preserved (it can be semantically meaningful, e.g. "Aegis-Defense AI").
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("canonical_name requires a non-empty string")
    return _WS_RE.sub(" ", name.casefold()).strip()


def stable_id(namespace: str, *parts: str) -> str:
    """Deterministic UUIDv5 for a natural key: ``stable_id("entity", "company", "Vexer")``.

    Returns the canonical hyphenated UUID string. Same inputs => same ID, always, on every host.
    """
    if not namespace or not namespace.strip():
        raise ValueError("stable_id requires a non-empty namespace")
    if not parts:
        raise ValueError("stable_id requires at least one key part")
    key = "|".join([namespace.strip()] + [canonical_name(str(p)) for p in parts])
    return str(uuid.uuid5(_NAMESPACE, key))


def content_hash(data: "str | bytes", *, algorithm: str = "sha256") -> str:
    """Content digest for evidence integrity, returned as ``"<algo>:<hex>"``."""
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    digest = hashlib.new(algorithm)
    digest.update(payload)
    return f"{algorithm}:{digest.hexdigest()}"


def idempotency_key(operation: str, *parts: str) -> str:
    """Deterministic 32-char key for de-duplicating retried operations.

    Callers should pass *business* identity (ids, hashes, timestamps) — never random values —
    otherwise the key provides no protection.
    """
    if not operation or not operation.strip():
        raise ValueError("idempotency_key requires a non-empty operation")
    if not parts:
        raise ValueError("idempotency_key requires at least one identity part")
    material = "|".join([operation.strip()] + [canonical_name(str(p)) for p in parts])
    return hashlib.sha256(f"{ID_SCHEME_VERSION}|{material}".encode("utf-8")).hexdigest()[:32]
