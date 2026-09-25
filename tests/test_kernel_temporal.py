"""
Temporal intelligence tests (§15, §17, §42): half-open validity windows, bitemporal truth,
as-of queries, expiry, and refusal to guess when temporal fields are missing.
"""
from datetime import datetime, timedelta, timezone

import pytest

from vexer_platform.contracts import Entity
from vexer_platform.errors import VexerError
from vexer_platform.temporal import (
    BitemporalRecord,
    ValidityWindow,
    iter_history,
    select_valid_as_of,
)

T0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
T2 = datetime(2027, 1, 1, 0, 0, tzinfo=timezone.utc)


def test_window_boundaries_are_start_inclusive_and_end_exclusive():
    window = ValidityWindow(T0, T1)
    assert window.contains(T0) is True                     # start inclusive
    assert window.contains(T1) is False                    # end exclusive — no double counting
    assert window.contains(T1 - timedelta(microseconds=1)) is True
    assert window.contains(T0 - timedelta(seconds=1)) is False
    assert window.expired_at(T1) is True
    assert window.is_open() is False


def test_open_ended_window_means_still_in_force_not_permanent():
    window = ValidityWindow(T0)
    assert window.is_open() is True
    assert window.contains(datetime(2035, 1, 1, tzinfo=timezone.utc)) is True
    assert window.expired_at(datetime(2035, 1, 1, tzinfo=timezone.utc)) is False


def test_inverted_or_empty_windows_and_naive_datetimes_are_rejected():
    with pytest.raises(VexerError):
        ValidityWindow(T1, T0)

    with pytest.raises(VexerError):
        ValidityWindow(T0, T0)                              # zero-length window is meaningless

    with pytest.raises(VexerError):
        ValidityWindow(datetime(2026, 1, 1))                # naïve datetime

    with pytest.raises(VexerError):
        ValidityWindow(T0).contains(datetime(2026, 3, 1))   # naïve query time


def test_overlap_and_intersection_follow_half_open_semantics():
    jan_jun = ValidityWindow(T0, T1)
    jun_dec = ValidityWindow(T1, T2)
    mar_sep = ValidityWindow(datetime(2026, 3, 1, tzinfo=timezone.utc),
                             datetime(2026, 9, 1, tzinfo=timezone.utc))

    assert jan_jun.overlaps(jun_dec) is False        # touching endpoints do not overlap
    assert jan_jun.overlaps(mar_sep) is True
    assert jan_jun.intersect(mar_sep).describe().startswith("[2026-03-01")
    assert jan_jun.intersect(jun_dec) is None

    open_window = ValidityWindow(T0)
    assert open_window.intersect(mar_sep) == mar_sep


def test_with_duration_builds_a_bounded_window():
    window = ValidityWindow(T0).with_duration(days=30)
    assert window.valid_until == T0 + timedelta(days=30)
    assert window.contains(T0 + timedelta(days=29)) is True
    assert window.contains(T0 + timedelta(days=31)) is False

    with pytest.raises(VexerError):
        ValidityWindow(T0).with_duration(days=-1)


def test_bitemporal_record_distinguishes_truth_from_knowledge():
    record = BitemporalRecord(
        value="Company X operates in the UAE",
        valid=ValidityWindow(T0, T1),
        recorded_at=T1 + timedelta(days=1),      # learned after the fact
    )
    assert record.as_known_at(T0 + timedelta(days=10)) is None     # true then, but not yet known
    assert record.as_known_at(T1 + timedelta(days=2)) is None      # known, but no longer valid
    assert record.as_known_at(T1 - timedelta(days=1) + timedelta(days=0)) is None

    live = BitemporalRecord(
        value="in force", valid=ValidityWindow(T0), recorded_at=T0
    )
    assert live.as_known_at(T1) == "in force"


def test_select_valid_as_of_answers_what_was_true_at_time_t():
    old_edge = Entity(
        entity_type="RELATIONSHIP",
        name="CompetitorCorp operates in the UAE",
        valid_from=T0,
        valid_until=T1,
    )
    new_edge = Entity(
        entity_type="RELATIONSHIP",
        name="CompetitorCorp exited the UAE",
        valid_from=T1,
    )

    as_of_march = select_valid_as_of([old_edge, new_edge], datetime(2026, 3, 1, tzinfo=timezone.utc))
    as_of_december = select_valid_as_of([old_edge, new_edge], datetime(2026, 12, 1, tzinfo=timezone.utc))

    assert [e.name for e in as_of_march] == ["CompetitorCorp operates in the UAE"]
    assert [e.name for e in as_of_december] == ["CompetitorCorp exited the UAE"]
    # Nothing is deleted: the historical record remains queryable at any point in time.


def test_select_valid_as_of_supports_bitemporal_records_and_rejects_untimed_objects():
    record = BitemporalRecord(value="v1", valid=ValidityWindow(T0, T1), recorded_at=T0)
    assert select_valid_as_of([record], datetime(2026, 3, 1, tzinfo=timezone.utc)) == [record]

    class Untimed:
        pass

    with pytest.raises(VexerError):
        select_valid_as_of([Untimed()], T0)


def test_iter_history_orders_by_recorded_time_and_requires_a_timestamp():
    early = Entity(entity_type="COMPANY", name="A", created_at=T0, updated_at=T0)
    late = Entity(entity_type="COMPANY", name="B", created_at=T1, updated_at=T1)
    assert [e.name for e in iter_history([late, early])] == ["A", "B"]

    class NoTimestamp:
        pass

    with pytest.raises(VexerError):
        iter_history([NoTimestamp()])
