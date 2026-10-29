"""The 09:00 session check, and the line its alarm matches.

The `no_session` alarm in infra/terraform/observability.tf is a metric filter
on the structured log: `$.message = "*no valid Kite session*"`. The command
used to tell only stderr and the audit table, so the filter had nothing to
match and the alarm could never fire. This pins the line it needs.
"""

import json
import logging
from datetime import date

import pytest
from django.core.management import call_command

from audit.models import AuditEvent

TUESDAY = date(2026, 9, 22)


@pytest.mark.django_db
def test_no_session_logs_the_line_the_alarm_matches(on, caplog):
    on(TUESDAY)
    with caplog.at_level(logging.ERROR, logger="steadystack.broker"):
        call_command("check_kite_session", "--offline")

    [line] = [r for r in caplog.records if r.name == "steadystack.broker"]
    assert line.levelname == "ERROR"
    assert "no valid Kite session" in line.getMessage()
    assert AuditEvent.objects.filter(action="kite_session.missing").exists()


@pytest.mark.django_db
def test_the_line_is_json_with_the_message_the_filter_reads(on, caplog):
    """The filter reads `$.message` out of JSON, so the formatter has to put it there."""
    from audit.logging import JSONFormatter

    on(TUESDAY)
    with caplog.at_level(logging.ERROR, logger="steadystack.broker"):
        call_command("check_kite_session", "--offline")

    [line] = [r for r in caplog.records if r.name == "steadystack.broker"]
    body = json.loads(JSONFormatter().format(line))
    assert body["message"] == "no valid Kite session"
    assert body["level"] == "ERROR"


# -- days the market is shut, and a calendar running out ----------------------


@pytest.fixture
def on(request):
    """Run the check at 09:00 IST on a given date."""
    import time_machine

    def _travel(day):
        traveller = time_machine.travel(f"{day.isoformat()} 09:00:00 +05:30", tick=False)
        traveller.start()
        request.addfinalizer(traveller.stop)

    return _travel


def loaded_until(day):
    from trading_calendar.models import SessionDay, SessionKind

    SessionDay.objects.create(trading_date=day, kind=SessionKind.CLOSED, settles=False)


@pytest.mark.django_db
def test_a_holiday_is_not_a_missing_session(on, caplog):
    """session-check.service fires every weekday. On an exchange holiday
    nobody has logged in, and an alarm saying so is one to learn to ignore."""
    from trading_calendar.models import SessionDay, SessionKind

    holiday = date(2026, 10, 2)
    SessionDay.objects.create(trading_date=holiday, kind=SessionKind.CLOSED, settles=False)
    loaded_until(date(2027, 12, 31))
    on(holiday)

    with caplog.at_level(logging.INFO):
        call_command("check_kite_session", "--offline")

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not AuditEvent.objects.filter(action="kite_session.missing").exists()


@pytest.mark.django_db
def test_a_weekend_is_not_a_missing_session(on, caplog):
    loaded_until(date(2027, 12, 31))
    on(date(2026, 9, 26))  # a Saturday

    with caplog.at_level(logging.INFO):
        call_command("check_kite_session", "--offline")

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.django_db
def test_a_calendar_under_a_month_from_running_out_is_an_error(on, caplog):
    """Nothing else scheduled on the box asks. Past its last row every weekday
    reads as a normal session, and the ladder runs into a closed market."""
    loaded_until(date(2026, 10, 15))
    on(date(2026, 9, 22))

    with caplog.at_level(logging.WARNING, logger="steadystack.calendar"):
        call_command("check_kite_session", "--offline")

    [line] = [r for r in caplog.records if r.name == "steadystack.calendar"]
    assert line.levelname == "ERROR"
    assert "running out" in line.getMessage()


@pytest.mark.django_db
def test_a_calendar_two_months_out_only_warns(on, caplog):
    loaded_until(date(2026, 11, 15))
    on(date(2026, 9, 22))

    with caplog.at_level(logging.WARNING, logger="steadystack.calendar"):
        call_command("check_kite_session", "--offline")

    [line] = [r for r in caplog.records if r.name == "steadystack.calendar"]
    assert line.levelname == "WARNING"


@pytest.mark.django_db
def test_an_empty_calendar_is_an_error_even_on_a_quiet_morning(on, caplog):
    on(date(2026, 9, 22))

    with caplog.at_level(logging.WARNING, logger="steadystack.calendar"):
        call_command("check_kite_session", "--offline")

    assert any(r.name == "steadystack.calendar" and r.levelname == "ERROR" for r in caplog.records)
