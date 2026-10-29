"""Asking the calendar whether today is a trading day, and when it runs.

Everything scheduled reads this. A tick that fires on a closed day must place
nothing, and a tick that fires outside a special session's hours must do the
same -- so the answer is a session window, not a boolean.

Coverage is the failure mode worth guarding. A calendar loaded only to the end
of last year will happily report every January day as a normal session,
because an ordinary weekday with no row *is* a normal session. That default is
right for a maintained calendar and dangerous for a stale one, so coverage is
something the system checks and alerts on rather than assumes (M18).
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.db import transaction
from django.utils import timezone

from audit.events import record
from audit.logging import fields
from trading_calendar.models import NORMAL_CLOSE, NORMAL_OPEN, SessionDay, SessionKind

logger = logging.getLogger("steadystack.calendar")

SATURDAY = 5
SUNDAY = 6

#: How far ahead the calendar should be loaded before anybody worries. A year
#: is what M18 asks for; the warning fires well before it runs out.
DESIRED_COVERAGE = timedelta(days=365)
COVERAGE_WARNING = timedelta(days=60)
#: Inside this, running out is an error, and the `task_failed` alarm emails.
#: A month is enough to find the exchange's circular and load it.
COVERAGE_CRITICAL = timedelta(days=30)

#: Indian equity settles T+1. Counted in settling days, so a settlement
#: holiday or a weekend pushes the credit out without changing this number.
SETTLEMENT_CYCLES = 1


@dataclass(frozen=True)
class Session:
    """What the market does on one date."""

    trading_date: date
    is_open: bool
    opens_at: time | None = None
    closes_at: time | None = None
    kind: str = SessionKind.NORMAL
    note: str = ""

    def __bool__(self) -> bool:
        return self.is_open

    @property
    def is_special(self) -> bool:
        return self.kind == SessionKind.SPECIAL

    def covers(self, moment: time) -> bool:
        """Whether *moment* falls inside the session.

        A tick firing at 11:00 on a Muhurat day is outside the evening session
        and must place nothing, even though the day is a trading day.
        """
        if not self.is_open:
            return False
        return self.opens_at <= moment <= self.closes_at


def closed(day: date, *, kind: str = SessionKind.CLOSED, note: str = "") -> Session:
    return Session(trading_date=day, is_open=False, kind=kind, note=note)


def session_for(day: date) -> Session:
    """The session on *day*.

    Weekends are closed without needing a row. Anything else is looked up, and
    a weekday with no row is an ordinary session -- which is why coverage is
    checked separately.
    """
    if day.weekday() in (SATURDAY, SUNDAY):
        return closed(day, note="weekend")

    entry = SessionDay.objects.filter(trading_date=day).first()

    if entry is None:
        return Session(
            trading_date=day,
            is_open=True,
            opens_at=NORMAL_OPEN,
            closes_at=NORMAL_CLOSE,
            kind=SessionKind.NORMAL,
        )

    if entry.kind == SessionKind.CLOSED:
        return closed(day, note=entry.note)

    return Session(
        trading_date=day,
        is_open=True,
        opens_at=entry.open_time or NORMAL_OPEN,
        closes_at=entry.close_time or NORMAL_CLOSE,
        kind=entry.kind,
        note=entry.note,
    )


def is_trading_day(day: date) -> bool:
    return session_for(day).is_open


def next_trading_day(after: date) -> date:
    """The next day the market opens. Used to date carry forward.

    Bounded rather than a while-true: a calendar that somehow closed every day
    would otherwise hang a scheduled task instead of failing it.
    """
    for offset in range(1, 32):
        candidate = after + timedelta(days=offset)
        if is_trading_day(candidate):
            return candidate
    raise RuntimeError(f"No trading day found in the month after {after}.")


def previous_trading_day(before: date) -> date | None:
    for offset in range(1, 32):
        candidate = before - timedelta(days=offset)
        if is_trading_day(candidate):
            return candidate
    return None


def settles_on(day: date) -> bool:
    """Whether clearing runs on *day* (M18b).

    The second of the calendar's two questions. It is not the same as whether
    the market traded: the exchange publishes settlement holidays on which
    trading is ordinary and clearing is shut. Weekends answer no to both and
    outrank any row (D18).
    """
    if day.weekday() in (SATURDAY, SUNDAY):
        return False

    entry = SessionDay.objects.filter(trading_date=day).first()
    if entry is None:
        return True
    return entry.settles


def settlement_due(trade_date: date, *, cycles: int = SETTLEMENT_CYCLES) -> date:
    """When shares bought on *trade_date* are actually credited (M31c).

    Counted in *settling* days forward, not calendar days: a settlement
    holiday delays the credit by a day, and over a weekend by three. This is
    the difference between a reconciliation that knows the shares are not due
    yet and one that reports a divergence every evening until the operator
    stops reading them.
    """
    cursor = trade_date
    for _ in range(max(1, cycles)):
        for offset in range(1, 32):
            candidate = cursor + timedelta(days=offset)
            if settles_on(candidate):
                cursor = candidate
                break
        else:
            raise RuntimeError(f"No settling day found in the month after {cursor}.")
    return cursor


def coverage_until() -> date | None:
    """The last date the calendar has been told about."""
    return (
        SessionDay.objects.order_by("-trading_date").values_list("trading_date", flat=True).first()
    )


def check_coverage(*, today: date | None = None) -> timedelta | None:
    """Alert when the calendar is running out.

    Returns how far ahead it is loaded, or None if it holds nothing at all. A
    stale calendar does not fail loudly on its own -- every uncovered weekday
    simply reads as a normal session -- so this is the only thing standing
    between a missing row and a ladder running into a closed market.
    """
    today = today or timezone.localdate()
    last = coverage_until()

    if last is None:
        record(
            "calendar.uncovered",
            entity_type="trading_calendar.SessionDay",
            after={"loaded_until": None},
        )
        logger.error("the trading calendar is empty: every weekday reads as a normal session")
        return None

    remaining = last - today
    if remaining < COVERAGE_WARNING:
        record(
            "calendar.coverage_low",
            entity_type="trading_calendar.SessionDay",
            after={"loaded_until": last.isoformat(), "days_remaining": remaining.days},
        )
        level = logging.ERROR if remaining < COVERAGE_CRITICAL else logging.WARNING
        logger.log(
            level,
            "the trading calendar is running out",
            extra=fields(loaded_until=last.isoformat(), days_remaining=remaining.days),
        )
    return remaining


class CalendarEditRefused(Exception):
    """A date that has already traded cannot be reclassified."""


@transaction.atomic
def set_day(
    day: date,
    *,
    kind: str,
    opens_at: time | None = None,
    closes_at: time | None = None,
    settles: bool | None = None,
    note: str = "",
    actor: str = "system",
) -> SessionDay:
    """Record what the market does on *day*, and whether it clears.

    Refused once that date has a tradesheet: reclassifying a day the system
    has already traded would make its record unexplainable, and the day
    happened whatever the calendar now says.

    A weekend may only be recorded as closed (M18a). NSE occasionally runs a
    special Saturday session and this system sits it out whatever a row says,
    so a row claiming one would be a row asserting a session the engine will
    silently ignore -- which is worse than no row at all.

    `settles` defaults from the kind: a closed day clears nothing, anything
    else clears unless the exchange's settlement list says otherwise (M18b).
    """
    from engine.models import TradeSheet

    if TradeSheet.objects.filter(trading_date=day).exists():
        raise CalendarEditRefused(
            f"{day} already has a tradesheet; the calendar cannot be changed underneath it."
        )

    if day.weekday() in (SATURDAY, SUNDAY) and kind != SessionKind.CLOSED:
        raise CalendarEditRefused(
            f"{day} is a weekend, and this system never trades one. It may be recorded "
            f"as closed, but not as a {kind} session."
        )

    if kind == SessionKind.SPECIAL and not (opens_at and closes_at):
        raise CalendarEditRefused("A special session must state when it opens and closes.")

    if settles is None:
        settles = kind != SessionKind.CLOSED
    if kind == SessionKind.CLOSED and settles:
        raise CalendarEditRefused(
            f"{day} is closed, so there are no trades on it to clear; it cannot settle."
        )

    entry, created = SessionDay.objects.update_or_create(
        trading_date=day,
        defaults={
            "kind": kind,
            "open_time": opens_at,
            "close_time": closes_at,
            "settles": settles,
            "note": note[:255],
            "loaded_by": actor,
        },
    )

    record(
        "calendar.day_set",
        actor=actor,
        entity_type="trading_calendar.SessionDay",
        entity_id=day.isoformat(),
        after={
            "kind": kind,
            "opens_at": opens_at.isoformat() if opens_at else None,
            "closes_at": closes_at.isoformat() if closes_at else None,
            "settles": settles,
            "note": note,
            "created": created,
        },
    )
    return entry


def now_ist() -> datetime:
    return timezone.localtime()
