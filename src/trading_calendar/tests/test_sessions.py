"""The session calendar.

The case that justifies the whole design is Muhurat: a real trading day with a
one-hour evening session. A holiday deny-list calls it "not a holiday,
therefore normal" and ladders into a closed market all afternoon, once a year,
reliably. A calendar that stores the session cannot make that mistake.
"""

from datetime import date, time

import pytest

from audit.models import AuditEvent
from trading_calendar.models import SessionDay, SessionKind
from trading_calendar.sessions import (
    CalendarEditRefused,
    check_coverage,
    coverage_until,
    is_trading_day,
    next_trading_day,
    previous_trading_day,
    session_for,
    set_day,
    settlement_due,
    settles_on,
)

# 2026-09-21 is a Monday; the 26th and 27th are the weekend.
MONDAY = date(2026, 9, 21)
FRIDAY = date(2026, 9, 25)
SATURDAY = date(2026, 9, 26)
SUNDAY = date(2026, 9, 27)

pytestmark = pytest.mark.django_db


# -- the ordinary week -----------------------------------------------------


def test_a_weekday_with_no_row_is_a_normal_session():
    """The default that keeps the table small -- and the one that makes a
    stale calendar dangerous, which is why coverage is checked."""
    session = session_for(MONDAY)

    assert session.is_open is True
    assert session.kind == SessionKind.NORMAL
    assert session.opens_at == time(9, 15)


def test_saturday_is_closed_without_a_row():
    assert session_for(SATURDAY).is_open is False


def test_sunday_is_closed_without_a_row():
    assert session_for(SUNDAY).is_open is False


def test_a_weekend_needs_no_calendar_entry_at_all():
    """Requiring one would mean the calendar runs out twice a week."""
    assert SessionDay.objects.count() == 0
    assert is_trading_day(SATURDAY) is False


# -- holidays --------------------------------------------------------------


def test_a_holiday_is_closed():
    set_day(MONDAY, kind=SessionKind.CLOSED, note="Exchange holiday")

    assert is_trading_day(MONDAY) is False
    assert session_for(MONDAY).note == "Exchange holiday"


def test_a_holiday_is_audited():
    set_day(MONDAY, kind=SessionKind.CLOSED, note="Exchange holiday", actor="operator")

    event = AuditEvent.objects.get(action="calendar.day_set")
    assert event.after["kind"] == SessionKind.CLOSED
    assert event.actor == "operator"


# -- special sessions ------------------------------------------------------


def test_a_special_session_is_a_trading_day():
    """Muhurat. The whole reason this is a calendar and not a deny-list."""
    set_day(
        MONDAY,
        kind=SessionKind.SPECIAL,
        opens_at=time(18, 15),
        closes_at=time(19, 15),
        note="Muhurat trading",
    )

    session = session_for(MONDAY)

    assert session.is_open is True
    assert session.is_special is True


def test_a_special_session_states_its_own_hours():
    set_day(MONDAY, kind=SessionKind.SPECIAL, opens_at=time(18, 15), closes_at=time(19, 15))

    session = session_for(MONDAY)

    assert session.opens_at == time(18, 15)
    assert session.closes_at == time(19, 15)


def test_a_tick_outside_a_special_session_is_outside_the_market():
    """11:00 on a Muhurat day is a closed market, even though the day trades."""
    set_day(MONDAY, kind=SessionKind.SPECIAL, opens_at=time(18, 15), closes_at=time(19, 15))
    session = session_for(MONDAY)

    assert session.covers(time(11, 0)) is False
    assert session.covers(time(18, 30)) is True


def test_a_normal_session_covers_the_ordinary_hours():
    session = session_for(MONDAY)

    assert session.covers(time(9, 45)) is True
    assert session.covers(time(15, 0)) is True
    assert session.covers(time(16, 0)) is False


def test_a_closed_day_covers_no_moment():
    assert session_for(SATURDAY).covers(time(11, 0)) is False


def test_a_special_session_without_hours_is_refused():
    with pytest.raises(CalendarEditRefused, match="when it opens"):
        set_day(MONDAY, kind=SessionKind.SPECIAL)


# -- walking the calendar --------------------------------------------------


def test_the_next_trading_day_after_friday_is_monday():
    assert next_trading_day(FRIDAY) == date(2026, 9, 28)


def test_the_next_trading_day_skips_a_holiday():
    set_day(date(2026, 9, 28), kind=SessionKind.CLOSED, note="holiday")
    assert next_trading_day(FRIDAY) == date(2026, 9, 29)


def test_the_previous_trading_day_before_monday_is_friday():
    assert previous_trading_day(date(2026, 9, 28)) == FRIDAY


def test_a_special_session_still_counts_as_the_next_trading_day():
    set_day(
        date(2026, 9, 28),
        kind=SessionKind.SPECIAL,
        opens_at=time(18, 15),
        closes_at=time(19, 15),
    )
    assert next_trading_day(FRIDAY) == date(2026, 9, 28)


# -- coverage --------------------------------------------------------------


def test_an_empty_calendar_is_reported():
    """Silence here would mean every uncovered weekday trades by default."""
    assert check_coverage(today=MONDAY) is None
    assert AuditEvent.objects.filter(action="calendar.uncovered").exists()


def test_coverage_is_reported_from_the_furthest_loaded_day():
    set_day(date(2027, 3, 1), kind=SessionKind.CLOSED, note="holiday")
    assert coverage_until() == date(2027, 3, 1)


def test_a_calendar_running_out_raises_a_warning():
    set_day(date(2026, 10, 2), kind=SessionKind.CLOSED, note="Gandhi Jayanti")

    remaining = check_coverage(today=MONDAY)

    assert remaining.days == 11
    assert AuditEvent.objects.filter(action="calendar.coverage_low").exists()


def test_a_well_loaded_calendar_says_nothing():
    set_day(date(2027, 9, 21), kind=SessionKind.CLOSED, note="a year out")

    check_coverage(today=MONDAY)

    assert not AuditEvent.objects.filter(action="calendar.coverage_low").exists()


# -- editing a day that has already traded ---------------------------------


def test_a_date_with_a_tradesheet_cannot_be_reclassified():
    """The day happened, whatever the calendar now says."""
    from engine.models import TradeSheet

    TradeSheet.objects.create(trading_date=MONDAY, version=1, trace_id="ts-2026-09-21")

    with pytest.raises(CalendarEditRefused, match="already has a tradesheet"):
        set_day(MONDAY, kind=SessionKind.CLOSED, note="too late")


# -- a weekend is only ever closed (M18a) ----------------------------------


def test_recording_a_weekend_as_a_normal_session_is_refused():
    """The engine sits a special Saturday out whatever a row says, so a row
    claiming one is a row that lies about what will happen."""
    with pytest.raises(CalendarEditRefused, match="weekend"):
        set_day(SATURDAY, kind=SessionKind.NORMAL, note="a special session")


def test_recording_a_weekend_as_a_special_session_is_refused():
    with pytest.raises(CalendarEditRefused, match="weekend"):
        set_day(
            SUNDAY,
            kind=SessionKind.SPECIAL,
            opens_at=time(18, 0),
            closes_at=time(19, 0),
            note="muhurat",
        )


def test_a_weekend_may_be_recorded_as_closed():
    """Pointless but harmless, and refusing it would be a rule about nothing."""
    entry = set_day(SATURDAY, kind=SessionKind.CLOSED, note="exchange holiday too")

    assert entry.kind == SessionKind.CLOSED


# -- settlement, beside trading on the same row (M18b) ---------------------


def test_an_ordinary_weekday_settles():
    assert settles_on(MONDAY) is True


def test_a_weekend_settles_nothing():
    assert settles_on(SATURDAY) is False
    assert settles_on(SUNDAY) is False


def test_a_closed_day_settles_nothing():
    set_day(FRIDAY, kind=SessionKind.CLOSED, note="exchange holiday")

    assert settles_on(FRIDAY) is False


def test_a_settlement_holiday_trades_but_does_not_settle():
    """The case a fourth session kind could not express: the ladder runs, and
    the shares do not arrive."""
    set_day(FRIDAY, kind=SessionKind.NORMAL, settles=False, note="settlement holiday")

    assert is_trading_day(FRIDAY) is True
    assert settles_on(FRIDAY) is False


def test_shares_bought_today_are_due_on_the_next_settling_day():
    assert settlement_due(MONDAY) == date(2026, 9, 22)


def test_a_weekend_pushes_the_due_date_to_monday():
    assert settlement_due(FRIDAY) == date(2026, 9, 28)


def test_a_settlement_holiday_delays_the_credit_by_a_day():
    """M31c's whole reason for existing: the broker legitimately shows fewer
    shares than the ledger, and that is not a divergence."""
    set_day(date(2026, 9, 22), kind=SessionKind.NORMAL, settles=False, note="settlement holiday")

    assert settlement_due(MONDAY) == date(2026, 9, 23)
