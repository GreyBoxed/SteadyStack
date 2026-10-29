"""Trace identity.

The promise is M45's: every audit event, log line and broker call carries a
trace ID, and a day's worth of them share one. These tests hold that promise
to the format, because a trace ID that varies in shape cannot be grepped.
"""

from datetime import date

import pytest
import time_machine

from audit.context import (
    bind_trading_day,
    current_request_id,
    current_trace_id,
    new_request_id,
    reset_request_id,
    reset_trace_id,
    set_request_id,
    today_ist,
    trace_id_for,
)
from audit.events import record
from audit.models import AuditEvent


def test_the_trace_id_format_is_ts_and_an_iso_date():
    assert trace_id_for(date(2026, 9, 22)) == "ts-2026-09-22"


def test_the_trace_id_zero_pads():
    """ts-2026-1-5 and ts-2026-01-05 would not match the same grep."""
    assert trace_id_for(date(2026, 1, 5)) == "ts-2026-01-05"


def test_binding_a_trading_day_sets_the_trace_id():
    token = bind_trading_day(date(2026, 9, 22))
    try:
        assert current_trace_id() == "ts-2026-09-22"
    finally:
        reset_trace_id(token)


def test_the_previous_trace_id_is_restored_on_reset():
    outer = bind_trading_day(date(2026, 9, 21))
    inner = bind_trading_day(date(2026, 9, 22))
    reset_trace_id(inner)
    try:
        assert current_trace_id() == "ts-2026-09-21"
    finally:
        reset_trace_id(outer)


@time_machine.travel("2026-09-22 10:15:00+05:30")
def test_an_unbound_context_falls_back_to_todays_trace_id():
    """A shell, a data migration or a task that forgot to bind still traces."""
    assert current_trace_id() == "ts-2026-09-22"


@time_machine.travel("2026-09-22 23:45:00+05:30")
def test_the_trading_day_is_an_ist_date_not_a_utc_one():
    """23:45 IST is the 22nd here and the 22nd's evening in UTC terms only
    because TIME_ZONE says so -- in UTC it is already the 22nd at 18:15, but
    at 00:30 IST it would be the previous UTC day. The date must follow IST."""
    assert today_ist() == date(2026, 9, 22)


@time_machine.travel("2026-09-23 00:30:00+05:30")
def test_just_after_midnight_ist_is_the_new_trading_date():
    """In UTC this instant is still 2026-09-22. The IST date is what counts."""
    assert today_ist() == date(2026, 9, 23)


def test_request_ids_are_unique():
    assert new_request_id() != new_request_id()


def test_a_request_id_is_empty_until_one_is_bound():
    assert current_request_id() == ""


def test_binding_and_resetting_a_request_id():
    token = set_request_id("abc123")
    try:
        assert current_request_id() == "abc123"
    finally:
        reset_request_id(token)
    assert current_request_id() == ""


@pytest.mark.django_db
def test_a_recorded_event_is_stamped_with_the_bound_trace_id():
    token = bind_trading_day(date(2026, 9, 22))
    try:
        event = record("tradesheet.generated", actor="system")
    finally:
        reset_trace_id(token)

    assert event.trace_id == "ts-2026-09-22"


@pytest.mark.django_db
def test_events_from_one_day_share_a_trace_id():
    token = bind_trading_day(date(2026, 9, 22))
    try:
        record("tradesheet.generated")
        record("order.placed")
        record("tradesheet.settled")
    finally:
        reset_trace_id(token)

    assert AuditEvent.objects.values_list("trace_id", flat=True).distinct().count() == 1


@pytest.mark.django_db
@time_machine.travel("2026-09-20 11:00:00+05:30")
def test_an_event_outside_a_trading_day_carries_its_own_dates_id():
    """A lumpsum credited on a Saturday belongs to no session, but must still
    be traceable. It takes the trace ID of its own calendar date (M45)."""
    event = record("cash.lumpsum_credited", actor="operator")
    assert event.trace_id == "ts-2026-09-20"


@pytest.mark.django_db
def test_recording_against_an_entity_captures_its_type_and_id():
    subject = record("first.event")
    event = record("second.event", entity=subject)

    assert event.entity_type == "audit.AuditEvent"
    assert event.entity_id == str(subject.pk)


@pytest.mark.django_db
def test_the_actor_defaults_to_system_not_to_blank():
    """An event with no actor at all cannot answer who caused it."""
    assert record("task.ran").actor == "system"


@pytest.mark.django_db
def test_before_and_after_are_stored_as_given():
    event = record(
        "config.updated",
        actor="operator",
        before={"daily_amount": "1000.00"},
        after={"daily_amount": "1500.00"},
    )
    assert event.before == {"daily_amount": "1000.00"}
    assert event.after == {"daily_amount": "1500.00"}
