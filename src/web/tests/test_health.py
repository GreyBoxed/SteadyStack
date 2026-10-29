"""The health checks: each fails when something due has not happened, and
stands down when nothing is due.

The second half matters as much as the first. A check that fires on a
Sunday, during a pause or on a day the operator skipped pages until everyone
ignores it, and then it catches nothing.
"""

import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
import time_machine
from django.utils import timezone

from engine.models import CashLedger, TradeSheet, TradeSheetState
from orders.models import DAILY_BUY_STREAM, FeedHeartbeat, TaskRun
from web import health

pytestmark = pytest.mark.django_db

TUESDAY = date(2026, 9, 22)
MONDAY = date(2026, 9, 21)
SUNDAY = date(2026, 9, 20)


def at(day, hour, minute=0, second=0):
    return timezone.make_aware(
        datetime.combine(day, time(hour, minute, second)), timezone.get_current_timezone()
    )


@pytest.fixture
def session(monkeypatch):
    """A Kite session, valid unless a test says otherwise."""
    state = {"valid": True}
    monkeypatch.setattr(health, "_session_valid", lambda: state["valid"])
    return state


def a_sheet(day=TUESDAY, *, state=TradeSheetState.RUNNING, authorized=True):
    return TradeSheet.objects.create(
        trading_date=day,
        version=1,
        trace_id=f"ts-{day.isoformat()}",
        state=state,
        quantity=4,
        spendable_cash=Decimal("5000"),
        authorized_at=at(day, 9, 0) if authorized else None,
        authorized_by="operator" if authorized else "",
    )


def a_round(task, moment, tick_no, *, succeeded=True):
    """A finished round at *moment*; `started_at` is auto_now_add."""
    run = TaskRun.objects.create(
        trading_date=moment.date(),
        task=task,
        stream=DAILY_BUY_STREAM,
        tick_no=tick_no,
        succeeded=succeeded,
    )
    TaskRun.objects.filter(pk=run.pk).update(started_at=moment)


def a_pulse(moment, *, connected=True, subscribed=0, last_tick_at=None):
    FeedHeartbeat.objects.update_or_create(
        id=FeedHeartbeat.SINGLETON,
        defaults={
            "beat_at": moment,
            "connected": connected,
            "subscribed": subscribed,
            "last_tick_at": last_tick_at,
        },
    )


def a_settled_day(day):
    CashLedger.objects.create(book="paper", trading_date=day)


# -- the Kite session ------------------------------------------------------


def test_no_session_after_nine_fails(session):
    session["valid"] = False
    assert health.session_ok(at(TUESDAY, 9, 5)) is False


def test_no_session_before_nine_is_not_yet_a_problem(session):
    session["valid"] = False
    assert health.session_ok(at(TUESDAY, 8, 30)) is True


def test_no_session_at_the_weekend_is_not_a_problem(session):
    session["valid"] = False
    assert health.session_ok(at(SUNDAY, 11, 0)) is True


def test_no_session_while_paused_is_not_a_problem(session):
    from configuration.pause import pause

    session["valid"] = False
    with time_machine.travel(at(TUESDAY, 8, 0), tick=False):
        pause(actor="operator", reason="away")

    assert health.session_ok(at(TUESDAY, 11, 0)) is True


# -- placing rounds --------------------------------------------------------


def test_an_authorized_day_with_a_recent_round_is_healthy(session):
    a_sheet()
    a_round("ladder", at(TUESDAY, 11, 0), tick_no=150)

    assert health.rounds_ok(at(TUESDAY, 11, 1)) is True


def test_an_authorized_day_whose_rounds_stopped_fails(session):
    a_sheet()
    a_round("ladder", at(TUESDAY, 11, 0), tick_no=150)

    assert health.rounds_ok(at(TUESDAY, 11, 5)) is False


def test_a_round_that_failed_is_not_a_round_that_ran(session):
    """A round refused every thirty seconds all day still claims its tick. It
    is the refusal the operator needs to hear about, not the claim."""
    a_sheet()
    a_round("ladder", at(TUESDAY, 11, 0), tick_no=150, succeeded=False)

    assert health.rounds_ok(at(TUESDAY, 11, 1)) is False


def test_rounds_are_not_expected_before_the_day_is_authorized(session):
    a_sheet(state=TradeSheetState.DRAFT, authorized=False)
    assert health.rounds_ok(at(TUESDAY, 11, 0)) is True


@pytest.mark.parametrize(
    "state",
    [TradeSheetState.SKIPPED, TradeSheetState.HALTED, TradeSheetState.FILLED],
)
def test_rounds_are_not_expected_once_the_day_has_stopped(session, state):
    a_sheet(state=state)
    assert health.rounds_ok(at(TUESDAY, 11, 0)) is True


def test_rounds_are_not_expected_outside_the_window(session):
    a_sheet()
    assert health.rounds_ok(at(TUESDAY, 9, 46)) is True  # the first round's grace
    assert health.rounds_ok(at(TUESDAY, 14, 30)) is True  # the window has closed


def test_rounds_are_not_expected_without_a_session(session):
    """The session check says why; a second alarm for the same cause is noise."""
    a_sheet()
    session["valid"] = False
    assert health.rounds_ok(at(TUESDAY, 11, 0)) is True


def test_rounds_are_not_expected_on_a_holiday(session):
    from trading_calendar.models import SessionDay, SessionKind

    SessionDay.objects.create(trading_date=TUESDAY, kind=SessionKind.CLOSED, settles=False)
    a_sheet()
    assert health.rounds_ok(at(TUESDAY, 11, 0)) is True


# -- the close -------------------------------------------------------------


def test_the_close_with_a_recent_round_is_healthy(session):
    a_sheet()
    a_round("fallback", at(TUESDAY, 15, 10), tick_no=1019)

    assert health.close_ok(at(TUESDAY, 15, 11)) is True


def test_a_close_that_never_ran_fails(session):
    a_sheet()
    assert health.close_ok(at(TUESDAY, 15, 10)) is False


def test_a_close_round_that_failed_is_not_a_close_that_ran(session):
    a_sheet()
    a_round("fallback", at(TUESDAY, 15, 10), tick_no=1019, succeeded=False)

    assert health.close_ok(at(TUESDAY, 15, 11)) is False


def test_the_close_is_not_expected_after_its_window(session):
    a_sheet()
    assert health.close_ok(at(TUESDAY, 15, 30)) is True


# -- the morning's plan -------------------------------------------------------


def test_no_tradesheet_ten_minutes_after_the_tradesheet_time_fails(session):
    """The 09:45 timer did not fire, or fired and failed: either way nothing
    will be bought today, and nothing else notices until the close."""
    assert health.tradesheet_ok(at(TUESDAY, 9, 56)) is False


def test_a_tradesheet_is_not_due_until_ten_minutes_after_its_time(session):
    assert health.tradesheet_ok(at(TUESDAY, 9, 54)) is True


def test_a_planned_day_is_healthy(session):
    a_sheet(state=TradeSheetState.DRAFT, authorized=False)
    assert health.tradesheet_ok(at(TUESDAY, 11, 0)) is True


def test_a_tradesheet_is_not_expected_on_a_weekend_holiday_or_pause(session):
    from configuration.pause import pause
    from trading_calendar.models import SessionDay, SessionKind

    assert health.tradesheet_ok(at(SUNDAY, 11, 0)) is True

    SessionDay.objects.create(trading_date=MONDAY, kind=SessionKind.CLOSED, settles=False)
    assert health.tradesheet_ok(at(MONDAY, 11, 0)) is True

    with time_machine.travel(at(TUESDAY, 8, 0), tick=False):
        pause(actor="operator", reason="away")
    assert health.tradesheet_ok(at(TUESDAY, 11, 0)) is True


def test_a_tradesheet_is_not_expected_once_the_market_has_closed(session):
    """Nothing planned after the close could fill; the settlement check
    covers the evening."""
    assert health.tradesheet_ok(at(TUESDAY, 16, 0)) is True


def test_a_tradesheet_is_not_expected_on_an_evening_only_session(session):
    """Muhurat opens at 18:15. The 09:45 timer's plan is refused as outside
    the session, so an alarm for its absence would fire every year."""
    from trading_calendar.models import SessionDay, SessionKind

    SessionDay.objects.create(
        trading_date=TUESDAY,
        kind=SessionKind.SPECIAL,
        open_time=time(18, 15),
        close_time=time(19, 15),
    )
    assert health.tradesheet_ok(at(TUESDAY, 18, 45)) is True


def test_an_unauthorized_day_after_half_past_ten_fails(session):
    """Nothing is placed until a person authorizes the day (M15f). By 10:30
    the ladder has been waiting three quarters of an hour."""
    a_sheet(state=TradeSheetState.DRAFT, authorized=False)
    assert health.authorized_ok(at(TUESDAY, 10, 31)) is False


def test_authorization_is_not_due_before_half_past_ten(session):
    a_sheet(state=TradeSheetState.DRAFT, authorized=False)
    assert health.authorized_ok(at(TUESDAY, 10, 29)) is True


def test_an_authorized_day_is_healthy(session):
    a_sheet()
    assert health.authorized_ok(at(TUESDAY, 11, 0)) is True


def test_a_regenerated_version_needs_authorizing_again(session):
    a_sheet()
    TradeSheet.objects.create(
        trading_date=TUESDAY,
        version=2,
        trace_id=f"ts-{TUESDAY.isoformat()}",
        state=TradeSheetState.DRAFT,
        quantity=4,
        spendable_cash=Decimal("5000"),
    )
    assert health.authorized_ok(at(TUESDAY, 11, 0)) is False


def test_a_skipped_day_needs_no_authorization(session):
    a_sheet(state=TradeSheetState.SKIPPED, authorized=False)
    assert health.authorized_ok(at(TUESDAY, 11, 0)) is True


def test_authorization_is_not_asked_of_a_day_with_no_sheet(session):
    """The tradesheet check says that; one cause, one alarm."""
    assert health.authorized_ok(at(TUESDAY, 11, 0)) is True


def test_authorization_is_not_expected_on_a_weekend_or_after_the_close(session):
    a_sheet(SUNDAY, state=TradeSheetState.DRAFT, authorized=False)
    assert health.authorized_ok(at(SUNDAY, 11, 0)) is True

    a_sheet(state=TradeSheetState.DRAFT, authorized=False)
    assert health.authorized_ok(at(TUESDAY, 16, 0)) is True


# -- the tick feed -----------------------------------------------------------


def test_a_feed_beating_and_connected_is_healthy(session):
    a_pulse(at(TUESDAY, 11, 0))
    assert health.feed_ok(at(TUESDAY, 11, 1)) is True


def test_a_feed_that_stopped_beating_fails(session):
    a_pulse(at(TUESDAY, 11, 0))
    assert health.feed_ok(at(TUESDAY, 11, 5)) is False


def test_a_feed_that_never_beat_fails(session):
    assert health.feed_ok(at(TUESDAY, 11, 0)) is False


def test_a_feed_alive_but_disconnected_fails(session):
    a_pulse(at(TUESDAY, 11, 0), connected=False)
    assert health.feed_ok(at(TUESDAY, 11, 0, 30)) is False


def test_a_feed_watching_names_but_hearing_nothing_fails(session):
    a_pulse(at(TUESDAY, 11, 0), subscribed=3, last_tick_at=at(TUESDAY, 10, 50))
    assert health.feed_ok(at(TUESDAY, 11, 0, 30)) is False


def test_a_feed_watching_nothing_hears_nothing_and_that_is_fine(session):
    """It subscribes only to names with open paper orders; with none, silence
    is correct."""
    a_pulse(at(TUESDAY, 11, 0), subscribed=0, last_tick_at=None)
    assert health.feed_ok(at(TUESDAY, 11, 0, 30)) is True


def test_the_feed_is_not_expected_outside_market_hours(session):
    assert health.feed_ok(at(TUESDAY, 8, 0)) is True
    assert health.feed_ok(at(TUESDAY, 16, 0)) is True
    assert health.feed_ok(at(SUNDAY, 11, 0)) is True


# -- settlement --------------------------------------------------------------


def test_yesterday_unsettled_fails(session):
    a_sheet(MONDAY)
    assert health.settled_ok(at(TUESDAY, 10, 0)) is False


def test_yesterday_settled_is_healthy(session):
    a_sheet(MONDAY)
    a_settled_day(MONDAY)
    assert health.settled_ok(at(TUESDAY, 10, 0)) is True


def test_today_is_due_only_after_the_evening_settlement(session):
    a_sheet(MONDAY)
    a_settled_day(MONDAY)
    a_sheet(TUESDAY)

    assert health.settled_ok(at(TUESDAY, 15, 50)) is True  # not due yet
    assert health.settled_ok(at(TUESDAY, 16, 30)) is False  # due, and missing


def test_a_day_with_no_tradesheet_has_nothing_to_settle(session):
    assert health.settled_ok(at(TUESDAY, 10, 0)) is True


def test_the_weekend_asks_about_friday(session):
    friday = date(2026, 9, 18)
    a_sheet(friday)
    assert health.settled_ok(at(SUNDAY, 11, 0)) is False
    a_settled_day(friday)
    assert health.settled_ok(at(SUNDAY, 11, 0)) is True


# -- the endpoint ------------------------------------------------------------


def test_the_endpoint_answers_503_and_names_the_failing_check(client, session):
    a_sheet()
    a_pulse(at(TUESDAY, 11, 0))

    with time_machine.travel(at(TUESDAY, 11, 0, 30), tick=False):
        response = client.get("/health/")

    body = json.loads(response.content)
    assert response.status_code == 503
    assert body["status"] == "failing"
    assert body["checks"]["rounds"] is False
    assert body["checks"]["feed"] is True
    assert body["checks"]["tradesheet"] is True
    assert body["checks"]["authorized"] is True


def test_the_endpoint_answers_200_when_everything_due_has_happened(client, session):
    a_sheet(MONDAY)
    a_settled_day(MONDAY)
    a_sheet()
    a_round("ladder", at(TUESDAY, 11, 0), tick_no=150)
    a_pulse(at(TUESDAY, 11, 0))

    with time_machine.travel(at(TUESDAY, 11, 0, 30), tick=False):
        response = client.get("/health/")

    assert response.status_code == 200
    assert json.loads(response.content)["status"] == "ok"


def test_the_grace_periods_are_minutes(session):
    assert health.ROUND_GRACE <= timedelta(minutes=5)
    assert health.FEED_GRACE <= timedelta(minutes=5)
