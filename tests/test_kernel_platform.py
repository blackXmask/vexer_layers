"""
Platform primitive tests: ULIDs, stable IDs, structured errors, request context, configuration.
These cover the §20/§21/§28/§32 requirements every other layer depends on.
"""
import asyncio
import json
import threading

import pytest

from vexer_platform.config import KernelConfig, PlatformProfile, load_kernel_config
from vexer_platform.context import (
    child_context,
    context_scope,
    current_context,
    new_request_context,
    parse_traceparent,
    require_context,
)
from vexer_platform.errors import ErrorCode, VexerError, from_exception, redact
from vexer_platform.ids import (
    canonical_name,
    content_hash,
    idempotency_key,
    is_valid_ulid,
    new_ulid,
    stable_id,
    ulid_timestamp_ms,
)

# ----------------------------------------------------------------------------- ids


def test_ulids_are_valid_unique_and_time_sortable():
    ids = [new_ulid() for _ in range(500)]
    assert len(set(ids)) == 500
    assert all(is_valid_ulid(value) for value in ids)
    assert ids == sorted(ids)                       # lexicographic order == creation order
    assert ulid_timestamp_ms(ids[0]) > 0
    assert ulid_timestamp_ms(ids[-1]) >= ulid_timestamp_ms(ids[0])


def test_ulids_are_monotonic_within_one_millisecond_and_under_clock_regression():
    same_ms = [new_ulid(now_ms=1_700_000_000_000) for _ in range(200)]
    assert len(set(same_ms)) == 200                 # randomness avoids collisions
    assert same_ms == sorted(same_ms)               # monotonic inside a single millisecond

    regression = new_ulid(now_ms=1_600_000_000_000)  # wall clock stepped backwards
    assert regression >= same_ms[-1]                 # ordering survives, it is not corrupted


def test_ulid_generation_is_thread_safe():
    results = []
    lock = threading.Lock()

    def worker():
        local = [new_ulid() for _ in range(200)]
        with lock:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 800
    assert len(set(results)) == 800                 # no duplicates under concurrency


def test_stable_ids_are_deterministic_and_canonicalisation_is_versioned():
    first = stable_id("entity", "COMPANY", "  Vexer   Corp ")
    second = stable_id("entity", "company", "vexer corp")
    assert first == second                          # deterministic across case/whitespace
    assert first != stable_id("entity", "company", "other corp")
    assert first != stable_id("relationship", "company", "vexer corp")
    assert canonical_name("  Vexer   Corp ") == "vexer corp"

    with pytest.raises(ValueError):
        canonical_name("   ")
    with pytest.raises(ValueError):
        stable_id("", "x")
    with pytest.raises(ValueError):
        stable_id("entity")


def test_content_hash_and_idempotency_key_are_stable_and_prefixed():
    assert content_hash("body") == content_hash(b"body")
    assert content_hash("body").startswith("sha256:")
    assert content_hash("body") != content_hash("other")

    key = idempotency_key("ingest", "src-1", "sha256:abc")
    assert key == idempotency_key("ingest", "src-1", "sha256:abc")   # retries deduplicate
    assert key != idempotency_key("ingest", "src-2", "sha256:abc")
    assert len(key) == 32 and key.isalnum()

    with pytest.raises(ValueError):
        idempotency_key("ingest")


# -------------------------------------------------------------------------- errors


def test_structured_errors_expose_code_retryability_and_safe_json():
    error = VexerError(
        ErrorCode.DEPENDENCY_TIMEOUT,
        "kafka publish timed out",
        service="ingestion",
        details={"topic": "vexer.events", "api_key": "super-secret-value"},
    )
    payload = error.to_dict()
    assert payload["code"] == "DEPENDENCY_TIMEOUT"
    assert payload["retryable"] is True             # explicit, never guessed by the caller
    assert payload["http_status"] == 504
    assert payload["details"]["topic"] == "vexer.events"
    assert payload["details"]["api_key"] == "***REDACTED***"    # secrets never leave the process
    json.dumps(payload)


def test_error_catalogue_maps_status_and_retryability_consistently():
    assert VexerError(ErrorCode.VALIDATION_FAILED, "bad").retryable is False
    assert VexerError(ErrorCode.VALIDATION_FAILED, "bad").http_status == 422
    assert VexerError(ErrorCode.BACKPRESSURE, "queue full").retryable is True
    assert VexerError(ErrorCode.FORBIDDEN, "nope").http_status == 403
    assert VexerError(ErrorCode.INTERNAL, "boom").retryable is False


def test_error_redaction_is_bounded_for_hostile_payloads():
    nested = {"level": 1}
    cursor = nested
    for _ in range(50):                             # pathologically deep untrusted structure
        cursor["child"] = {"level": cursor["level"] + 1}
        cursor = cursor["child"]
    assert isinstance(redact(nested), dict)         # terminates instead of blowing the stack


def test_from_exception_classifies_third_party_failures_conservatively():
    assert from_exception(TimeoutError("operation timed out")).code is ErrorCode.DEPENDENCY_TIMEOUT
    assert from_exception(ConnectionError("connection refused")).code is ErrorCode.DEPENDENCY_UNAVAILABLE
    assert from_exception(ValueError("bad payload")).code is ErrorCode.VALIDATION_FAILED
    assert from_exception(RuntimeError("mystery")).code is ErrorCode.INTERNAL
    original = VexerError(ErrorCode.NOT_FOUND, "missing")
    assert from_exception(original) is original     # translation is idempotent


# ------------------------------------------------------------------------- context


def test_request_context_propagates_correlation_through_child_hops():
    root = new_request_context("orchestrator", tenant_id="tenant-a")
    child = root.child(service_name="market-intelligence")
    assert child.correlation_id == root.correlation_id      # one investigation, one correlation id
    assert child.trace_id == root.trace_id
    assert child.request_id != root.request_id              # every hop gets its own request id
    assert root.as_headers()["X-Correlation-Id"] == root.correlation_id


def test_context_is_isolated_per_thread_and_per_async_task():
    outer = new_request_context("test")
    with context_scope(outer):
        observed = []

        def worker():
            with context_scope(new_request_context("worker")):
                observed.append(current_context().service_name)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        assert observed == ["worker"]                       # the thread did not inherit the parent
        assert require_context().service_name == "test"     # parent context untouched

        async def coroutine():
            with context_scope(child_context(service_name="async-child")):
                await asyncio.sleep(0)
                return current_context().service_name

        assert asyncio.run(coroutine()) == "async-child"
        assert require_context().service_name == "test"

    assert current_context() is None                        # scope restored on exit


def test_require_context_and_bad_traceparent_raise_structured_errors():
    assert current_context() is None
    with pytest.raises(VexerError) as excinfo:
        require_context()
    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED

    trace_id, span_id, sampled = parse_traceparent("00-" + "a" * 32 + "-" + "b" * 16 + "-01")
    assert trace_id == "a" * 32 and span_id == "b" * 16 and sampled is True

    for bad in ("", "not-a-traceparent", "00-" + "0" * 32 + "-" + "b" * 16 + "-01"):
        with pytest.raises(VexerError):
            parse_traceparent(bad)


# -------------------------------------------------------------------------- config


def test_config_profiles_validate_fail_fast_and_expose_no_secrets():
    default = load_kernel_config({})
    assert default.profile is PlatformProfile.DEV

    prod = load_kernel_config({"VEXER_PROFILE": "PROD", "VEXER_SERVICE_NAME": "gateway"})
    assert prod.profile is PlatformProfile.PROD and prod.strict is True
    assert prod.service_name == "gateway"

    with pytest.raises(VexerError):
        load_kernel_config({"VEXER_PROFILE": "PRODUCTION"})     # refuses to guess a profile

    with pytest.raises(VexerError):
        KernelConfig(profile=PlatformProfile.PROD, log_level="DEBUG")   # no verbose leaks in prod

    with pytest.raises(VexerError):
        load_kernel_config({"VEXER_DEFAULT_TIMEOUT_MS": "not-a-number"})

    with pytest.raises(VexerError):
        load_kernel_config({"VEXER_DEFAULT_TIMEOUT_MS": "-5"})

    described = prod.describe()
    assert "secret" not in json.dumps(described).lower()
    assert described["profile"] == "PROD"


def test_config_rejects_out_of_range_confidence_weights():
    with pytest.raises(VexerError):
        KernelConfig(confidence_weights={"evidence_quality": 1.5})

    with pytest.raises(VexerError):
        KernelConfig(confidence_weights={})                     # empty weights cannot score anything

    with pytest.raises(VexerError):
        KernelConfig(contradiction_penalty=1.5)

    with pytest.raises(VexerError):
        KernelConfig(freshness_half_life_days=0)

    with pytest.raises(VexerError):
        KernelConfig(log_level="VERBOSE")

