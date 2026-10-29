"""The tradesheet lock (D13).

A tradesheet may be regenerated freely until an order for that day exists in a
non-terminal state. After that it is frozen, because the stored ranking
snapshot is the permanent explanation for an order that is out in the market,
and a version that could drift away from the order it produced would explain
nothing.

Under the sliced execution model the lock effectively closes at tick 0 and
stays closed until the day settles, since orders rest until the fallback.
Regeneration is therefore a pre-placement facility; once placement has begun
the operator's lever is an override (M15a), which records a decision against
the sheet rather than rewriting it. A filled buy keeps it closed even once every
order is terminal -- see `why_locked`.
"""

from datetime import date

from engine.models import TradeSheet
from orders.models import Order
from orders.state import TERMINAL


class TradesheetLocked(Exception):
    """Regeneration refused because an order for that day is still live."""


def live_orders_on(trading_date: date):
    return Order.objects.filter(trading_date=trading_date).exclude(state__in=list(TERMINAL))


def is_locked(trading_date: date) -> bool:
    return why_locked(trading_date) is not None


def why_locked(trading_date: date) -> str | None:
    """Why the day can no longer be replanned, or None if it still can.

    Three things close it, and the second is the one that matters most. A
    live order is the original rule. But an order that has *filled* is
    terminal, and a lock that only watched live orders reopened the moment
    the day's buy completed: a new version would then start from nothing
    committed, and the ladder -- which runs the latest version -- would buy
    the day's quantity a second time. Once any of the day's buy has filled,
    it has bought, and it has been decided. An order that was cancelled or
    rejected without a fill bought nothing, so it does not close the day.
    """
    from engine.ledger import day_settled
    from orders.models import DAILY_BUY_STREAM

    live = list(live_orders_on(trading_date).values_list("tag", flat=True)[:5])
    if live:
        return (
            f"{trading_date} has live orders ({', '.join(live)}). Cancel them before "
            "regenerating, or use an override to change the day."
        )
    if Order.objects.filter(
        trading_date=trading_date, stream=DAILY_BUY_STREAM, filled_quantity__gt=0
    ).exists():
        return (
            f"{trading_date} has already bought. A new version would buy the day again; "
            "use an override to change what is left of it."
        )
    if day_settled(trading_date):
        return f"{trading_date} has settled, and a settled day is a record, not a plan."
    return None


def require_unlocked(trading_date: date) -> None:
    """Refuse regeneration once the day has committed to anything."""
    reason = why_locked(trading_date)
    if reason is not None:
        raise TradesheetLocked(reason)


def latest_sheet(trading_date: date) -> TradeSheet | None:
    return TradeSheet.objects.filter(trading_date=trading_date).order_by("-version").first()
