"""Is everything that should have happened by now, happening?

The health endpoint is what an outside probe polls every thirty seconds, and
it answers 503 when any check below fails, so the probe's alarm is the one
alert that covers silence: a timer that never fired, a feed that died, a day
that never settled. Every other alarm fires on something the system said;
these fire on what it did not do.

Each check knows when it applies. "No placing round in two minutes" means
something at 11:00 on an authorized Tuesday and nothing on a Sunday, during a
pause, or on a day the operator skipped -- and a check that cannot tell the
difference pages every weekend until everybody ignores it.

Booleans only. The endpoint is open to the world, so it says which check
failed and nothing about why.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from django.utils import timezone

#: How recent the last placing or close round must be while one is due. Rounds
#: run every thirty seconds; two minutes allows a slow round and a late timer.
ROUND_GRACE = timedelta(minutes=2)
#: How recent the tick feed's pulse must be. It beats every fifteen seconds.
FEED_GRACE = timedelta(minutes=2)
#: How long a feed watching names may go without a tick in market hours.
TICK_GRACE = timedelta(minutes=5)
#: The morning check runs at 09:00; by then a session should exist.
SESSION_DUE = (9, 0)
#: How long after the configured tradesheet time a plan may take to appear.
#: Planning is a few broker reads; ten minutes is a timer that never fired.
TRADESHEET_GRACE = timedelta(minutes=10)
#: By when a person should have authorized the day (M15f). Nothing is placed
#: until they do, and from here the ladder's schedule is being eaten.
AUTHORIZE_DUE = time(10, 30)
#: Settlement runs at 15:45; allow it half an hour.
SETTLED_DUE = (16, 15)

#: A daily buy still placing, as opposed to one that stopped or finished.
PLACING = frozenset({"DRAFT", "RUNNING"})


@dataclass(frozen=True)
class Report:
    checks: dict[str, bool]

    @property
    def ok(self) -> bool:
        return all(self.checks.values())


def report(*, now: datetime | None = None) -> Report:
    now = timezone.localtime(now) if now else timezone.localtime()
    return Report(
        checks={
            "session": session_ok(now),
            "tradesheet": tradesheet_ok(now),
            "authorized": authorized_ok(now),
            "rounds": rounds_ok(now),
            "close": close_ok(now),
            "feed": feed_ok(now),
            "settled": settled_ok(now),
        }
    )


# -- when each check applies -----------------------------------------------


def _trading_now(now: datetime) -> bool:
    from configuration.pause import is_paused
    from trading_calendar.sessions import is_trading_day

    return is_trading_day(now.date()) and not is_paused(on=now.date())


def _at(now: datetime, hour: int, minute: int) -> datetime:
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _at_time(now: datetime, moment) -> datetime:
    return _at(now, moment.hour, moment.minute)


def _session_valid() -> bool:
    from broker import session

    return session.status().valid


def _buying_sheet(day):
    """Today's latest daily-buy tradesheet, if it is authorized and still placing."""
    from engine.models import TradeSheet

    sheet = TradeSheet.objects.filter(trading_date=day).order_by("-version").first()
    if sheet is None or not sheet.is_authorized or sheet.state not in PLACING:
        return None
    return sheet


def _ran_recently(task: str, now: datetime) -> bool:
    """A round of *task* finished cleanly within the grace.

    Succeeded, not merely claimed: a round refused every thirty seconds still
    claims its tick, and counting the claim would call a day that places
    nothing healthy.
    """
    from orders.models import DAILY_BUY_STREAM, TaskRun

    return TaskRun.objects.filter(
        trading_date=now.date(),
        task=task,
        stream=DAILY_BUY_STREAM,
        started_at__gte=now - ROUND_GRACE,
        succeeded=True,
    ).exists()


def _planning_day(now: datetime) -> bool:
    """A trading day whose session is open now and was open at the tradesheet
    time. An evening-only session fails the second: its 09:45 plan is refused
    as outside the session, so there is nothing to expect of the timer."""
    from configuration.store import current
    from trading_calendar.sessions import session_for

    if not _trading_now(now):
        return False
    today = session_for(now.date())
    return today.covers(now.time()) and today.covers(current().tradesheet_time)


# -- the checks ----------------------------------------------------------------


def session_ok(now: datetime) -> bool:
    """From 09:00 on a trading day, there is a Kite session to trade with."""
    if not _trading_now(now) or now < _at(now, *SESSION_DUE):
        return True
    return _session_valid()


def tradesheet_ok(now: datetime) -> bool:
    """Ten minutes after the tradesheet time, today has a tradesheet."""
    from configuration.store import current
    from engine.models import TradeSheet

    if not _planning_day(now) or now < _at_time(now, current().tradesheet_time) + TRADESHEET_GRACE:
        return True
    return TradeSheet.objects.filter(trading_date=now.date()).exists()


def authorized_ok(now: datetime) -> bool:
    """From 10:30, today's latest tradesheet is authorized -- or skipped, and
    so has nothing to authorize."""
    from configuration.store import current
    from engine.models import TradeSheet

    due = max(
        _at_time(now, AUTHORIZE_DUE),
        _at_time(now, current().tradesheet_time) + TRADESHEET_GRACE,
    )
    if not _planning_day(now) or now < due:
        return True
    sheet = TradeSheet.objects.filter(trading_date=now.date()).order_by("-version").first()
    if sheet is None:
        return True  # the tradesheet check says so; one cause, one alarm
    return sheet.is_authorized or sheet.is_skipped


def rounds_ok(now: datetime) -> bool:
    """While an authorized day is placing, a placing round ran in the last two minutes."""
    from configuration.store import current
    from orders import idempotency

    config = current()
    opens = _at_time(now, config.tradesheet_time) + ROUND_GRACE
    closes = _at_time(now, config.placement_window_close)
    if not _trading_now(now) or not opens <= now < closes:
        return True
    if not _session_valid() or _buying_sheet(now.date()) is None:
        return True  # the session check, or the operator, explains the quiet
    return _ran_recently(idempotency.LADDER, now)


def close_ok(now: datetime) -> bool:
    """Through the close's window, a close round ran in the last two minutes."""
    from configuration.store import current
    from orders import idempotency

    config = current()
    opens = _at_time(now, config.market_fallback_time) + ROUND_GRACE
    closes = _at_time(now, config.close_window_end)
    if not _trading_now(now) or not opens <= now < closes:
        return True
    if not _session_valid() or _buying_sheet(now.date()) is None:
        return True
    return _ran_recently(idempotency.FALLBACK, now)


def feed_ok(now: datetime) -> bool:
    """In market hours, the feed process is alive, connected, and -- if it is
    watching anything -- hearing ticks."""
    from orders.models import FeedHeartbeat
    from trading_calendar.sessions import session_for

    if not _trading_now(now) or not session_for(now.date()).covers(now.time()):
        return True
    if not _session_valid():
        return True  # the feed cannot connect without one; the session check says so

    beat = FeedHeartbeat.objects.filter(id=FeedHeartbeat.SINGLETON).first()
    if beat is None or now - beat.beat_at > FEED_GRACE or not beat.connected:
        return False
    if beat.subscribed and (beat.last_tick_at is None or now - beat.last_tick_at > TICK_GRACE):
        return False
    return True


def settled_ok(now: datetime) -> bool:
    """The last trading day that had a tradesheet has its cash-ledger row."""
    from engine.ledger import day_settled
    from engine.models import TradeSheet
    from trading_calendar.sessions import is_trading_day, previous_trading_day

    today = now.date()
    due = (
        today
        if is_trading_day(today) and now >= _at(now, *SETTLED_DUE)
        else previous_trading_day(today)
    )
    if due is None or not TradeSheet.objects.filter(trading_date=due).exists():
        return True  # nothing was planned that day, so nothing to settle
    return day_settled(due)
